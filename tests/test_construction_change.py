from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from construction_change.api import JsonApplication
from construction_change.clock import FrozenClock
from construction_change.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from construction_change.impact import CommittedDemand, compute_impact, diff_payloads, exclusion_order
from construction_change.service import ChangeService


CAPACITIES = {
    "housing_units": "200",
    "water_drainage": "1000",
    "school_seats": "300",
    "road_capacity": "500",
    "fire_coverage": "200",
}

DEMANDS = {
    "app-1": (10, {"housing_units": "80", "water_drainage": "400", "school_seats": "120", "road_capacity": "200", "fire_coverage": "80"}),
    "app-2": (20, {"housing_units": "60", "water_drainage": "300", "school_seats": "90", "road_capacity": "150", "fire_coverage": "60"}),
    "app-3": (30, {"housing_units": "40", "water_drainage": "200", "school_seats": "60", "road_capacity": "100", "fire_coverage": "40"}),
}


def change_payload(change_id: str = "chg-1", *, freeze_road: str = "100") -> dict[str, object]:
    return {
        "change_id": change_id,
        "title": "二标段扩建施工",
        "reason": "新增安置楼栋与配套",
        "window": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-12-31T23:59:59Z"},
        "buildings": [{"building_id": "b1", "action": "renovate"}, {"building_id": "b2", "action": "demolish"}],
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
            {"step_key": "acceptance-check", "title": "竣工验收"},
        ],
        "rollback_plan": {
            "summary": "停工清理并恢复通行",
            "steps": [
                {"step_key": "stop-work", "title": "停工清理"},
                {"step_key": "restore-road", "title": "恢复通行"},
            ],
        },
    }


class ImpactTests(unittest.TestCase):
    def test_remaining_and_conflict_selection(self) -> None:
        committed = [
            CommittedDemand("app-1", 10, "2026-09-26T08:00:00Z", {"road_capacity": Decimal("200"), "housing_units": Decimal("80")}),
            CommittedDemand("app-2", 20, "2026-09-26T08:01:00Z", {"road_capacity": Decimal("150")}),
            CommittedDemand("app-3", 30, "2026-09-26T08:02:00Z", {"road_capacity": Decimal("100")}),
        ]
        impact = compute_impact(
            capacities={"housing_units": Decimal("200"), "water_drainage": Decimal("0"), "school_seats": Decimal("0"), "road_capacity": Decimal("500"), "fire_coverage": Decimal("0")},
            freeze={"road_capacity": Decimal("100")},
            deltas={"road_capacity": Decimal("-20")},
            committed=committed,
        )
        road = impact["constraints"]["road_capacity"]
        self.assertEqual(road["committed"], "450.000")
        self.assertEqual(road["remaining_before"], "50.000")
        self.assertEqual(road["remaining_during"], "-50.000")
        self.assertEqual(road["remaining_after"], "30.000")
        self.assertEqual(road["shortfall"], "50.000")
        self.assertEqual(impact["excluded_application_ids"], ["app-3"])
        self.assertEqual(impact["conflicts"][0]["constraints"], ["road_capacity"])
        self.assertEqual(road["remaining_during_resolved"], "50.000")
        self.assertTrue(impact["feasible"])

    def test_exclusion_order_is_deterministic(self) -> None:
        demands = [
            CommittedDemand("a-low", 5, "2026-09-26T08:00:00Z", {}),
            CommittedDemand("b-new", 20, "2026-09-26T09:00:00Z", {}),
            CommittedDemand("c-old", 20, "2026-09-26T08:00:00Z", {}),
        ]
        ordered = [item.application_id for item in exclusion_order(demands)]
        self.assertEqual(ordered, ["b-new", "c-old", "a-low"])

    def test_diff_payloads_tracks_nested_changes(self) -> None:
        diff = diff_payloads(
            {"window": {"starts_at": "a", "ends_at": "b"}, "freeze": {"road": "80"}},
            {"window": {"starts_at": "a", "ends_at": "c"}, "freeze": {"road": "100"}, "note": "x"},
        )
        self.assertEqual(diff["window.ends_at"], {"from": "b", "to": "c"})
        self.assertEqual(diff["freeze.road"], {"from": "80", "to": "100"})
        self.assertEqual(diff["note"], {"from": None, "to": "x"})
        self.assertNotIn("window.starts_at", diff)


class ChangeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = ChangeService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("eng", "engineer"),
            ("eng2", "engineer"),
            ("site", "site"),
            ("appr", "approver"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_zone("plan", {"zone_id": "zone-e", "name": "东部安置片区", "timezone": "Asia/Shanghai"})
        self.service.publish_service_version("plan", "zone-e", {"capacities": CAPACITIES, "note": "初始版本"})
        for building_id in ("b1", "b2", "b3"):
            self.service.create_building("plan", "zone-e", {"building_id": building_id, "name": f"楼栋{building_id}", "households": 48})
        for application_id, (priority, demands) in DEMANDS.items():
            self.service.register_application("eng", "zone-e", {
                "application_id": application_id,
                "applicant": f"家庭-{application_id}",
                "demands": demands,
                "priority": priority,
                "idempotency_key": f"key-{application_id}",
            })

    def tearDown(self) -> None:
        self.connection.close()

    def create_and_submit(self, change_id: str = "chg-1") -> dict[str, object]:
        self.service.create_change("eng", "zone-e", {**change_payload(change_id), "idempotency_key": f"key-{change_id}"})
        return self.service.submit_change("eng", change_id, 1)

    def approve_and_start(self, change_id: str = "chg-1") -> None:
        self.service.approve_change("appr", change_id, 1)
        self.clock.current = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
        self.service.start_execution("eng", change_id)

    def test_application_registration_blocked_by_shortage(self) -> None:
        with self.assertRaises(Conflict) as ctx:
            self.service.register_application("eng", "zone-e", {
                "application_id": "app-4",
                "applicant": "家庭-app-4",
                "demands": {"housing_units": "30", "water_drainage": "0", "school_seats": "0", "road_capacity": "0", "fire_coverage": "0"},
                "priority": 40,
                "idempotency_key": "key-app-4",
            })
        self.assertIn("housing_units", str(ctx.exception))

    def test_submit_computes_impact_and_conflicts(self) -> None:
        submitted = self.create_and_submit()
        impact = submitted["impact"]
        self.assertEqual(submitted["snapshot_version_id"], 1)
        self.assertEqual(impact["constraints"]["road_capacity"]["shortfall"], "50.000")
        self.assertEqual(impact["excluded_application_ids"], ["app-3"])
        stored = self.service.change_impact("audit", "chg-1")
        self.assertEqual(stored["impact"]["committed_count"], 3)

    def test_approve_requires_other_person(self) -> None:
        self.create_and_submit()
        with self.assertRaises(Forbidden):
            self.service.approve_change("eng", "chg-1", 1)
        with self.assertRaises(Forbidden):
            self.service.reject_change("eng", "chg-1", 1, "自己驳回自己")
        approved = self.service.approve_change("appr", "chg-1", 1)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(len(approved["bundle_sha256"]), 64)
        excluded = self.service.get_application("audit", "app-3")
        self.assertEqual(excluded["state"], "excluded")
        self.assertEqual(excluded["exclusion"]["constraints"], ["road_capacity"])

    def test_approve_rejects_stale_assessment(self) -> None:
        self.create_and_submit()
        self.service.register_application("eng", "zone-e", {
            "application_id": "app-4",
            "applicant": "家庭-app-4",
            "demands": {"housing_units": "10", "water_drainage": "0", "school_seats": "0", "road_capacity": "0", "fire_coverage": "0"},
            "priority": 40,
            "idempotency_key": "key-app-4",
        })
        with self.assertRaises(Conflict):
            self.service.approve_change("appr", "chg-1", 1)
        refreshed = self.service.submit_change("eng", "chg-1", 1)
        self.assertEqual(refreshed["impact"]["committed_count"], 4)
        self.assertEqual(self.service.approve_change("appr", "chg-1", 1)["state"], "approved")

    def test_full_execution_publishes_new_service_version(self) -> None:
        self.create_and_submit()
        self.approve_and_start()
        state = None
        for index, step_key in enumerate(("demolish-old", "foundation", "acceptance-check"), start=1):
            state = self.service.post_receipt("site", "chg-1", {
                "step_key": step_key,
                "outcome": "done",
                "note": f"确认{index}",
                "idempotency_key": f"key-step-{index}",
            })
        self.assertEqual(state["state"], "completed")
        current = self.service.get_current_version("audit", "zone-e")
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["source"], "change:chg-1")
        self.assertEqual(current["capacities"]["housing_units"], "250.000")
        self.assertEqual(current["capacities"]["road_capacity"], "480.000")
        buildings = {row["building_id"]: row["state"] for row in self.service.list_buildings("audit", "zone-e")["buildings"]}
        self.assertEqual(buildings["b1"], "modified")
        self.assertEqual(buildings["b2"], "demolished")
        self.assertEqual(buildings["b3"], "standing")

    def test_receipts_must_follow_step_order(self) -> None:
        self.create_and_submit()
        self.approve_and_start()
        with self.assertRaises(InvalidState) as ctx:
            self.service.post_receipt("site", "chg-1", {"step_key": "foundation", "outcome": "done", "note": "跳步", "idempotency_key": "key-skip"})
        self.assertIn("demolish-old", str(ctx.exception))
        change = self.service.get_change("audit", "chg-1")
        self.assertEqual(change["state"], "in_progress")

    def test_partial_progress_cannot_be_success(self) -> None:
        self.create_and_submit()
        self.approve_and_start()
        state = self.service.post_receipt("site", "chg-1", {"step_key": "demolish-old", "outcome": "done", "note": "完成", "idempotency_key": "key-step-1"})
        self.assertEqual(state["state"], "in_progress")
        self.assertEqual(state["next_step_key"], "foundation")
        change = self.service.get_change("audit", "chg-1")
        self.assertEqual(change["state"], "in_progress")
        self.assertIsNone(change["result_version_id"])
        versions = self.service.list_service_versions("audit", "zone-e")["versions"]
        self.assertEqual(len(versions), 1)

    def test_receipt_replay_is_idempotent(self) -> None:
        self.create_and_submit()
        self.approve_and_start()
        payload = {"step_key": "demolish-old", "outcome": "done", "note": "完成", "idempotency_key": "key-step-1"}
        first = self.service.post_receipt("site", "chg-1", payload)
        second = self.service.post_receipt("site", "chg-1", payload)
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.post_receipt("site", "chg-1", {**payload, "note": "不同内容"})

    def test_failure_rolls_back_and_restores_applications(self) -> None:
        self.create_and_submit()
        self.approve_and_start()
        self.service.post_receipt("site", "chg-1", {"step_key": "demolish-old", "outcome": "done", "note": "完成", "idempotency_key": "key-step-1"})
        failed = self.service.post_receipt("site", "chg-1", {"step_key": "foundation", "outcome": "failed", "note": "基坑涌水", "idempotency_key": "key-step-2"})
        self.assertEqual(failed["state"], "failed")
        with self.assertRaises(InvalidState):
            self.service.post_receipt("site", "chg-1", {"step_key": "acceptance-check", "outcome": "done", "note": "x", "idempotency_key": "key-step-3"})
        self.assertEqual(self.service.begin_rollback("eng", "chg-1")["state"], "rolling_back")
        self.service.post_receipt("site", "chg-1", {"step_key": "stop-work", "outcome": "done", "note": "已清理", "idempotency_key": "key-rb-1"})
        rolled_back = self.service.post_receipt("site", "chg-1", {"step_key": "restore-road", "outcome": "done", "note": "已恢复", "idempotency_key": "key-rb-2"})
        self.assertEqual(rolled_back["state"], "rolled_back")
        self.assertEqual(rolled_back["restored_application_ids"], ["app-3"])
        self.assertEqual(self.service.get_application("audit", "app-3")["state"], "approved")
        versions = self.service.list_service_versions("audit", "zone-e")["versions"]
        self.assertEqual(len(versions), 1)

    def test_failure_takeover_closes_manual_not_success(self) -> None:
        self.create_and_submit()
        self.approve_and_start()
        self.service.post_receipt("site", "chg-1", {"step_key": "demolish-old", "outcome": "failed", "note": "发现文物", "idempotency_key": "key-step-1"})
        takeover = self.service.begin_takeover("eng", "chg-1", "文物部门介入，现场人工处置")
        self.assertEqual(takeover["state"], "manual_takeover")
        closed = self.service.close_takeover("eng", "chg-1", "文物勘察完成，现场已人工恢复，未动容量")
        self.assertEqual(closed["state"], "manual_closed")
        self.assertEqual(closed["restored_application_ids"], ["app-3"])
        change = self.service.get_change("audit", "chg-1")
        self.assertNotEqual(change["state"], "completed")
        self.assertIsNone(change["result_version_id"])

    def test_rollback_failure_forces_manual_takeover(self) -> None:
        self.create_and_submit()
        self.approve_and_start()
        self.service.post_receipt("site", "chg-1", {"step_key": "demolish-old", "outcome": "failed", "note": "机械故障", "idempotency_key": "key-step-1"})
        self.service.begin_rollback("eng", "chg-1")
        self.service.post_receipt("site", "chg-1", {"step_key": "stop-work", "outcome": "failed", "note": "清理受阻", "idempotency_key": "key-rb-1"})
        with self.assertRaises(InvalidState):
            self.service.begin_rollback("eng", "chg-1")
        self.assertEqual(self.service.begin_takeover("eng", "chg-1", "现场会商")["state"], "manual_takeover")

    def test_start_execution_requires_window(self) -> None:
        self.create_and_submit()
        self.service.approve_change("appr", "chg-1", 1)
        with self.assertRaises(InvalidState):
            self.service.start_execution("eng", "chg-1")  # 时钟仍在 2026-09-26，窗口未开始
        self.clock.current = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(self.service.start_execution("eng", "chg-1")["state"], "in_progress")

    def test_freeze_blocks_new_applications_inside_window(self) -> None:
        self.create_and_submit()
        self.service.approve_change("appr", "chg-1", 1)
        payload = {
            "application_id": "app-4",
            "applicant": "家庭-app-4",
            "demands": {"housing_units": "0", "water_drainage": "0", "school_seats": "0", "road_capacity": "60", "fire_coverage": "0"},
            "priority": 40,
            "idempotency_key": "key-app-4",
        }
        self.service.register_application("eng", "zone-e", payload)  # 窗口前冻结未生效
        self.service.withdraw_application("eng", "app-4", 1)
        self.clock.current = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
        with self.assertRaises(Conflict) as ctx:
            self.service.register_application("eng", "zone-e", {**payload, "application_id": "app-5", "idempotency_key": "key-app-5"})
        self.assertIn("road_capacity", str(ctx.exception))

    def test_revision_diff_chain_is_preserved(self) -> None:
        self.service.create_change("eng", "zone-e", {**change_payload("chg-1", freeze_road="80"), "idempotency_key": "key-chg-1"})
        second = self.service.revise_change("eng", "chg-1", 1, change_payload("chg-1", freeze_road="100"))
        self.assertEqual(second["diff"], {"freeze_amounts.road_capacity": {"from": "80.000", "to": "100.000"}})
        third_payload = {**change_payload("chg-1"), "title": "二标段扩建施工(调整)"}
        third = self.service.revise_change("eng", "chg-1", 2, third_payload)
        self.assertEqual(third["diff"], {"title": {"from": "二标段扩建施工", "to": "二标段扩建施工(调整)"}})
        chain = self.service.change_revisions("audit", "chg-1")["revisions"]
        self.assertEqual([row["revision"] for row in chain], [1, 2, 3])
        self.assertEqual([row["parent_revision"] for row in chain], [None, 1, 2])
        self.assertIn("window", chain[0]["diff"])
        with self.assertRaises(ValidationFailed):
            self.service.revise_change("eng", "chg-1", 3, third_payload)

    def test_explanation_covers_each_decision(self) -> None:
        self.create_and_submit()
        self.approve_and_start()
        self.service.post_receipt("site", "chg-1", {"step_key": "demolish-old", "outcome": "failed", "note": "基坑涌水", "idempotency_key": "key-step-1"})
        self.service.begin_takeover("eng", "chg-1", "专家会商处置")
        explanation = self.service.explain_change("audit", "chg-1")
        kinds = [item["decision"] for item in explanation["decisions"]]
        self.assertIn("impact_assessment", kinds)
        self.assertIn("application_excluded", kinds)
        self.assertIn("bundle_approved", kinds)
        self.assertIn("receipt_failed", kinds)
        self.assertIn("step_failed", kinds)
        self.assertIn("manual_takeover", kinds)
        excluded = next(item for item in explanation["decisions"] if item["decision"] == "application_excluded")
        self.assertEqual(excluded["application_id"], "app-3")
        self.assertIn("road_capacity", excluded["basis"])

    def test_change_id_reuse_and_unknown_change(self) -> None:
        self.service.create_change("eng", "zone-e", {**change_payload("chg-1"), "idempotency_key": "key-chg-1"})
        with self.assertRaises(Conflict):
            self.service.create_change("eng", "zone-e", {**change_payload("chg-1"), "idempotency_key": "key-chg-1b"})
        with self.assertRaises(NotFound):
            self.service.get_change("audit", "chg-x")
        replay = self.service.create_change("eng", "zone-e", {**change_payload("chg-1"), "idempotency_key": "key-chg-1"})
        self.assertEqual(replay["revision"], 1)

    def test_audit_chain_detects_tampering(self) -> None:
        self.create_and_submit()
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE change_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_smoke(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        missing_actor = app.handle("GET", "/changes/chg-1")
        self.assertEqual(missing_actor.status, 422)
        unknown = app.handle("GET", "/nope", {"X-Actor-Id": "audit"})
        self.assertEqual(unknown.status, 404)
        self.create_and_submit()
        impact = app.handle("GET", "/changes/chg-1/impact", {"X-Actor-Id": "audit"})
        self.assertEqual(impact.status, 200)
        self.assertEqual(impact.body["impact"]["excluded_application_ids"], ["app-3"])
        explanation = app.handle("GET", "/changes/chg-1/explanation", {"X-Actor-Id": "audit"})
        self.assertEqual(explanation.status, 200)


if __name__ == "__main__":
    unittest.main()
