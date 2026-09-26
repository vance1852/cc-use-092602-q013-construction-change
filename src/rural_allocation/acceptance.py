"""贯通补偿单价、地块资源池、土地库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "village-a", "name": "北部示范村", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mu": "500000"})
    service.create_facility("plan", {"facility_id": "settlement-b", "name": "东部安置片区", "kind": "settlement", "timezone": "Asia/Shanghai", "capacity_mu": "800000"})
    service.create_route("plan", {"route_id": "pool-a-b", "origin_id": "village-a", "destination_id": "settlement-b", "product": "cultivated-land", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "village-a", "product": "cultivated-land", "grade": "PEAK_VALLEY", "quantity_mu": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pool-a-b", "shipper_id": "household-east", "service_date": "2026-09-25", "requested_mu": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pool-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "relocation-recovery", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"pool-a-b": "20"}, "demand_changes": {"village-a:cultivated-land": "-5"}})
    service.approve_scenario("risk", "relocation-recovery", 1)
    scenario = service.run_scenario("plan", "relocation-recovery", "2026-09-23")
    service.register_building("plan", {"building_id": "bld-east-1", "settlement_id": "settlement-b", "name": "东部片区1号楼", "housing_units": 120, "floors": 18})
    service.register_building("plan", {"building_id": "bld-east-2", "settlement_id": "settlement-b", "name": "东部片区2号楼", "housing_units": 80, "floors": 11})
    service.register_service("plan", {"service_id": "svc-housing", "settlement_id": "settlement-b", "kind": "housing", "capacity": "500", "unit": "套"})
    service.register_service("plan", {"service_id": "svc-water", "settlement_id": "settlement-b", "kind": "water", "capacity": "2000", "unit": "立方米/日"})
    service.register_service("plan", {"service_id": "svc-seats", "settlement_id": "settlement-b", "kind": "seats", "capacity": "600", "unit": "个"})
    service.register_service("plan", {"service_id": "svc-road", "settlement_id": "settlement-b", "kind": "road", "capacity": "3000", "unit": "标准车/日"})
    service.register_service("plan", {"service_id": "svc-fire", "settlement_id": "settlement-b", "kind": "fire", "capacity": "500", "unit": "套"})
    service.revise_service("plan", "svc-fire", {"capacity": "450", "unit": "套", "note": "消防站布局调整", "expected_revision": 1})
    service.submit_change("plan", {"change_id": "chg-water-main", "settlement_id": "settlement-b", "title": "给排水主干管检修", "window": {"starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-10T18:00:00Z"}, "demands": {"water": "1500"}, "building_ids": ["bld-east-2"], "steps": ["停水作业", "更换管段", "恢复供水"], "rollback_plan": {"summary": "恢复旧管段供水", "steps": ["关闭新管段", "恢复旧管段"]}, "priority": 20})
    service.decide_change("risk", "chg-water-main", 1, "approved", "窗口避开入住高峰，资源冻结合理")
    beta = service.submit_change("plan", {"change_id": "chg-beta", "settlement_id": "settlement-b", "title": "1号楼外立面改造", "window": {"starts_at": "2026-10-03T08:00:00Z", "ends_at": "2026-10-06T18:00:00Z"}, "demands": {"water": "700", "housing": "40"}, "building_ids": ["bld-east-1"], "steps": ["搭设脚手架", "拆除旧外立面", "恢复立面与清理"], "rollback_plan": {"summary": "恢复临时围挡并撤离", "steps": ["拆除脚手架", "恢复围挡"]}, "priority": 50})
    service.decide_change("risk", "chg-beta", 1, "approved", "已评估对主干管检修的挤占并通知双方调整")
    service.record_receipt("dispatch", "chg-beta", 0, "done", "脚手架验收合格")
    service.record_receipt("dispatch", "chg-beta", 1, "done", "旧外立面拆除完成")
    beta_done = service.record_receipt("dispatch", "chg-beta", 2, "done", "立面恢复并清场")
    service.submit_change("plan", {"change_id": "chg-gamma", "settlement_id": "settlement-b", "title": "内部道路拓宽", "window": {"starts_at": "2026-10-04T08:00:00Z", "ends_at": "2026-10-05T18:00:00Z"}, "demands": {"road": "800"}, "building_ids": ["bld-east-2"], "steps": ["封闭半幅道路", "摊铺沥青"], "rollback_plan": {"summary": "恢复原有路面", "steps": ["清除新铺材料", "恢复标线"]}, "priority": 60})
    service.decide_change("risk", "chg-gamma", 1, "approved", "道路承载充足")
    service.record_receipt("dispatch", "chg-gamma", 0, "failed", "发现未探明燃气管道")
    service.begin_rollback("dispatch", "chg-gamma", "按回退方案恢复原状")
    service.record_receipt("dispatch", "chg-gamma", 0, "done", "新铺材料已清除", step_kind="rollback")
    gamma = service.record_receipt("dispatch", "chg-gamma", 1, "done", "标线恢复并撤场", step_kind="rollback")
    construction = {
        "beta_conflicts": beta["assessment"]["conflicting_applications"],
        "beta_state": beta_done["state"],
        "gamma_state": gamma["state"],
        "explain_beta": service.explain_change("chg-beta"),
        "fire_service_versions": service.service_versions("svc-fire")["versions"],
    }
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "construction": construction, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行乡镇片区调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
