"""贯通片区登记、公共服务版本、既有申请、变更审批、现场回执与回退的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import ChangeService


CAPACITIES_V1 = {
    "housing_units": "200",
    "water_drainage": "1000",
    "school_seats": "300",
    "road_capacity": "500",
    "fire_coverage": "200",
}


def _change_payload(change_id: str, *, freeze_road: str) -> dict[str, object]:
    return {
        "change_id": change_id,
        "title": "二标段扩建施工",
        "reason": "新增安置楼栋与配套，需占用部分道路与给排水能力",
        "window": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-12-31T23:59:59Z"},
        "buildings": [
            {"building_id": "b1", "action": "renovate"},
            {"building_id": "b2", "action": "renovate"},
        ],
        "freeze_amounts": {"road_capacity": freeze_road, "water_drainage": "50"},
        "capacity_deltas": {
            "housing_units": "50",
            "water_drainage": "100",
            "school_seats": "60",
            "road_capacity": "-20",
            "fire_coverage": "50",
        },
        "execution_steps": [
            {"step_key": "demolish-old", "title": "旧房拆除"},
            {"step_key": "foundation", "title": "基础施工"},
            {"step_key": "main-structure", "title": "主体结构"},
            {"step_key": "acceptance-check", "title": "竣工验收"},
        ],
        "rollback_plan": {
            "summary": "停工清理现场并恢复道路通行与供水",
            "steps": [
                {"step_key": "stop-work", "title": "停工清理"},
                {"step_key": "restore-road", "title": "恢复通行"},
            ],
        },
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
    service = ChangeService(connection, clock)
    for user_id, role in (
        ("plan", "planner"),
        ("eng", "engineer"),
        ("site", "site"),
        ("appr", "approver"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    service.create_zone("plan", {"zone_id": "settlement-east", "name": "东部安置片区", "timezone": "Asia/Shanghai"})
    service.publish_service_version("plan", "settlement-east", {"capacities": CAPACITIES_V1, "note": "初始公共服务版本"})
    for building_id, name, households in (("b1", "一标段1栋", 48), ("b2", "一标段2栋", 48), ("b3", "二标段1栋", 72)):
        service.create_building("plan", "settlement-east", {"building_id": building_id, "name": name, "households": households})

    applications = (
        ("app-1", 10, {"housing_units": "80", "water_drainage": "400", "school_seats": "120", "road_capacity": "200", "fire_coverage": "80"}),
        ("app-2", 20, {"housing_units": "60", "water_drainage": "300", "school_seats": "90", "road_capacity": "150", "fire_coverage": "60"}),
        ("app-3", 30, {"housing_units": "40", "water_drainage": "200", "school_seats": "60", "road_capacity": "100", "fire_coverage": "40"}),
    )
    for application_id, priority, demands in applications:
        service.register_application("eng", "settlement-east", {
            "application_id": application_id,
            "applicant": f"家庭-{application_id}",
            "demands": demands,
            "priority": priority,
            "idempotency_key": f"key-{application_id}",
        })

    # 变更一：修订差异链 -> 提交影响评估 -> 他人整体审批 -> 回执逐步完成。
    service.create_change("eng", "settlement-east", {**_change_payload("chg-001", freeze_road="80"), "idempotency_key": "key-chg-001"})
    revision = service.revise_change("eng", "chg-001", 1, _change_payload("chg-001", freeze_road="100"))
    submitted = service.submit_change("eng", "chg-001", 2)
    approved = service.approve_change("appr", "chg-001", 2)
    clock.advance(days=6)  # 进入施工窗口 2026-10-02
    service.start_execution("eng", "chg-001")
    receipt_state = None
    for index, step_key in enumerate(("demolish-old", "foundation", "main-structure", "acceptance-check"), start=1):
        receipt_state = service.post_receipt("site", "chg-001", {
            "step_key": step_key,
            "outcome": "done",
            "note": f"第{index}步现场确认",
            "idempotency_key": f"key-chg-001-step-{index}",
        })

    # 变更二：执行中失败 -> 明确回退 -> 被排斥申请恢复。
    service.create_change("eng", "settlement-east", {
        "change_id": "chg-002",
        "title": "给排水管网改造",
        "reason": "老旧管网扩容，施工期需大幅压减供水能力",
        "window": {"starts_at": "2026-11-01T00:00:00Z", "ends_at": "2026-11-30T23:59:59Z"},
        "buildings": [{"building_id": "b3", "action": "inspect"}],
        "freeze_amounts": {"water_drainage": "500"},
        "capacity_deltas": {"water_drainage": "200"},
        "execution_steps": [
            {"step_key": "cut-off", "title": "停水切换"},
            {"step_key": "lay-pipe", "title": "管道敷设"},
            {"step_key": "restore", "title": "恢复供水"},
        ],
        "rollback_plan": {
            "summary": "重新开阀恢复供水并检查确认",
            "steps": [
                {"step_key": "re-open", "title": "重新开阀"},
                {"step_key": "inspect", "title": "检查确认"},
            ],
        },
        "idempotency_key": "key-chg-002",
    })
    submitted2 = service.submit_change("eng", "chg-002", 1)
    service.approve_change("appr", "chg-002", 1)
    clock.advance(days=34)  # 2026-11-05，进入变更二窗口
    service.start_execution("eng", "chg-002")
    service.post_receipt("site", "chg-002", {"step_key": "cut-off", "outcome": "done", "note": "停水完成", "idempotency_key": "key-chg-002-step-1"})
    failed = service.post_receipt("site", "chg-002", {"step_key": "lay-pipe", "outcome": "failed", "note": "管沟塌方", "idempotency_key": "key-chg-002-step-2"})
    service.begin_rollback("eng", "chg-002")
    service.post_receipt("site", "chg-002", {"step_key": "re-open", "outcome": "done", "note": "供水恢复", "idempotency_key": "key-chg-002-rb-1"})
    rolled_back = service.post_receipt("site", "chg-002", {"step_key": "inspect", "outcome": "done", "note": "检查合格", "idempotency_key": "key-chg-002-rb-2"})

    result = {
        "status": "ok",
        "workspace": workspace.name,
        "revision_diff": revision["diff"],
        "impact_conflicts": submitted["impact"]["excluded_application_ids"],
        "approved_bundle": approved["bundle_sha256"],
        "completion": receipt_state,
        "second_impact_conflicts": submitted2["impact"]["excluded_application_ids"],
        "failure": failed,
        "rolled_back": rolled_back,
        "restored_application": service.get_application("audit", "app-2")["state"],
        "versions": service.list_service_versions("audit", "settlement-east")["versions"],
        "explanation": service.explain_change("audit", "chg-001"),
        "audit": service.audit_chain("audit"),
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行施工变更影响审批离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
