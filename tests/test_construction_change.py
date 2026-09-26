from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from rural_allocation.acceptance import run as acceptance_run
from rural_allocation.api import JsonApplication
from rural_allocation.clock import FrozenClock
from rural_allocation.construction import (
    ResourceApplication,
    assess_constraints,
    payload_diff,
)
from rural_allocation.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from rural_allocation.service import SupplyService


ROOT = Path(__file__).resolve().parents[1]


class ConstraintAssessmentTests(unittest.TestCase):
    def test_remaining_and_displacement_follow_reverse_priority(self) -> None:
        result = assess_constraints(
            capacities={"water": Decimal("1000")},
            applications=[
                ResourceApplication("chg-high", 10, "2026-09-20T00:00:00Z", {"water": Decimal("400")}),
                ResourceApplication("chg-low", 90, "2026-09-21T00:00:00Z", {"water": Decimal("300")}),
            ],
            demands={"water": Decimal("500")},
        )
        water = next(row for row in result["constraints"] if row["kind"] == "water")
        self.assertEqual(water["committed"], "700.000")
        self.assertEqual(water["remaining"], "-200.000")
        self.assertFalse(water["fits"])
        self.assertEqual(water["displaced"], [{"change_id": "chg-low", "displaced": "200.000"}])
        self.assertEqual(water["remaining_after_displacement"], "0.000")
        self.assertTrue(water["feasible_after_displacement"])
        self.assertEqual([item["change_id"] for item in result["conflicting_applications"]], ["chg-low"])
        self.assertTrue(result["feasible"])

    def test_infeasible_when_displacing_everything_is_not_enough(self) -> None:
        result = assess_constraints(
            capacities={"water": Decimal("100")},
            applications=[ResourceApplication("chg-old", 10, "2026-09-20T00:00:00Z", {"water": Decimal("50")})],
            demands={"water": Decimal("120")},
        )
        water = next(row for row in result["constraints"] if row["kind"] == "water")
        self.assertEqual(water["displaced"], [{"change_id": "chg-old", "displaced": "50.000"}])
        self.assertFalse(water["feasible_after_displacement"])
        self.assertFalse(result["feasible"])
        self.assertEqual(result["blocking_kinds"], ["water"])

    def test_unregistered_constraint_blocks_the_change(self) -> None:
        result = assess_constraints(capacities={}, applications=[], demands={"housing": Decimal("1")})
        housing = next(row for row in result["constraints"] if row["kind"] == "housing")
        self.assertEqual(housing["capacity"], "0.000")
        self.assertFalse(housing["feasible_after_displacement"])
        self.assertEqual(result["blocking_kinds"], ["housing"])

    def test_comfortable_change_reports_full_remaining(self) -> None:
        result = assess_constraints(
            capacities={"road": Decimal("3000")},
            applications=[ResourceApplication("chg-old", 10, "2026-09-20T00:00:00Z", {"road": Decimal("800")})],
            demands={"road": Decimal("500")},
        )
        road = next(row for row in result["constraints"] if row["kind"] == "road")
        self.assertEqual(road["remaining"], "1700.000")
        self.assertTrue(road["fits"])
        self.assertEqual(road["displaced"], [])
        self.assertEqual(result["conflicting_applications"], [])


class PayloadDiffTests(unittest.TestCase):
    def test_nested_changes_additions_and_removals(self) -> None:
        diff = payload_diff(
            {"title": "旧", "demands": {"water": "10.000", "road": "5.000"}, "steps": ["a"]},
            {"title": "新", "demands": {"water": "10.000"}, "steps": ["a", "b"]},
        )
        self.assertEqual(diff["title"], {"op": "changed", "from": "旧", "to": "新"})
        self.assertEqual(diff["demands"]["road"], {"op": "removed", "from": "5.000"})
        self.assertEqual(diff["steps"], {"op": "changed", "from": ["a"], "to": ["a", "b"]})

    def test_equal_payloads_have_empty_diff(self) -> None:
        self.assertEqual(payload_diff({"a": {"b": [1, 2]}}, {"a": {"b": [1, 2]}}), {})

    def test_creation_diff_marks_everything_added(self) -> None:
        diff = payload_diff({}, {"title": "新变更"})
        self.assertEqual(diff["title"], {"op": "added", "to": "新变更"})


class ConstructionChangeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("dispatch", "dispatcher"),
            ("risk-a", "risk"),
            ("risk-b", "risk"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "settlement-b", "name": "东部安置片区", "kind": "settlement", "timezone": "Asia/Shanghai", "capacity_mu": "800000"})
        self.service.register_building("plan", {"building_id": "bld-1", "settlement_id": "settlement-b", "name": "1号楼", "housing_units": 120, "floors": 18})
        self.service.register_building("plan", {"building_id": "bld-2", "settlement_id": "settlement-b", "name": "2号楼", "housing_units": 80, "floors": 11})
        for service_id, kind, capacity in (
            ("svc-housing", "housing", "500"),
            ("svc-water", "water", "2000"),
            ("svc-seats", "seats", "600"),
            ("svc-road", "road", "3000"),
            ("svc-fire", "fire", "500"),
        ):
            self.service.register_service("plan", {"service_id": service_id, "settlement_id": "settlement-b", "kind": kind, "capacity": capacity, "unit": "单位"})

    def tearDown(self) -> None:
        self.connection.close()

    def change_payload(self, change_id: str, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "change_id": change_id,
            "settlement_id": "settlement-b",
            "title": "施工调整",
            "window": {"starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-05T18:00:00Z"},
            "demands": {"water": "700"},
            "building_ids": ["bld-1"],
            "steps": ["第一步", "第二步"],
            "rollback_plan": {"summary": "恢复原状", "steps": ["回退一步"]},
            "priority": 50,
        }
        payload.update(overrides)
        return payload

    def approve_existing(self, change_id: str = "chg-old", water: str = "1500") -> None:
        self.service.submit_change("plan", self.change_payload(change_id, demands={"water": water}, priority=20))
        self.service.decide_change("risk-a", change_id, 1, "approved", "同意")

    def test_assessment_lists_conflicting_applications_before_approval(self) -> None:
        self.approve_existing()
        submitted = self.service.submit_change("plan", self.change_payload("chg-new"))
        water = next(row for row in submitted["assessment"]["constraints"] if row["kind"] == "water")
        self.assertEqual(water["capacity"], "2000.000")
        self.assertEqual(water["committed"], "1500.000")
        self.assertEqual(water["remaining"], "-200.000")
        self.assertEqual(water["displaced"], [{"change_id": "chg-old", "displaced": "200.000"}])
        conflicts = submitted["assessment"]["conflicting_applications"]
        self.assertEqual([item["change_id"] for item in conflicts], ["chg-old"])
        self.assertIn("给排水", conflicts[0]["reason"])
        stored = self.service.change_assessment("chg-new")
        self.assertEqual(stored["snapshot_sha256"], submitted["snapshot_sha256"])
        self.assertEqual(stored["snapshot"]["settlement_id"], "settlement-b")

    def test_window_without_overlap_does_not_conflict(self) -> None:
        self.approve_existing()
        submitted = self.service.submit_change(
            "plan",
            self.change_payload(
                "chg-later",
                window={"starts_at": "2026-11-01T08:00:00Z", "ends_at": "2026-11-05T18:00:00Z"},
            ),
        )
        water = next(row for row in submitted["assessment"]["constraints"] if row["kind"] == "water")
        self.assertEqual(water["committed"], "0.000")
        self.assertEqual(submitted["assessment"]["conflicting_applications"], [])

    def test_approval_is_atomic_and_requires_another_person(self) -> None:
        self.service.submit_change("risk-a", self.change_payload("chg-self"))
        with self.assertRaises(Forbidden):
            self.service.decide_change("risk-a", "chg-self", 1, "approved", "自己批自己")
        with self.assertRaises(Forbidden):
            self.service.decide_change("plan", "chg-self", 1, "approved", "越权")
        decided = self.service.decide_change("risk-b", "chg-self", 1, "approved", "整体可行")
        self.assertEqual(decided["state"], "approved")
        approval = self.service.explain_change("chg-self")["approvals"][0]
        self.assertEqual(set(approval["scope"]), {"window", "freeze", "rollback_plan"})
        self.assertEqual(approval["scope_sha256"], decided["scope_sha256"])
        self.assertEqual(approval["decided_by"], "risk-b")

    def test_receipts_advance_step_by_step_and_partial_is_never_success(self) -> None:
        self.service.submit_change("plan", self.change_payload("chg-steps", steps=["一", "二", "三"]))
        with self.assertRaises(InvalidState):
            self.service.record_receipt("dispatch", "chg-steps", 0, "done", "未审批先施工")
        self.service.decide_change("risk-a", "chg-steps", 1, "approved", "同意")
        with self.assertRaises(InvalidState):
            self.service.record_receipt("dispatch", "chg-steps", 1, "done", "跳步")
        first = self.service.record_receipt("dispatch", "chg-steps", 0, "done", "第一步完成")
        self.assertEqual(first["state"], "in_progress")
        with self.assertRaises(InvalidState):
            self.service.record_receipt("dispatch", "chg-steps", 0, "done", "重复回执")
        second = self.service.record_receipt("dispatch", "chg-steps", 1, "done", "第二步完成")
        self.assertEqual(second["state"], "in_progress")
        self.assertNotEqual(self.service.change("chg-steps")["state"], "completed")
        third = self.service.record_receipt("dispatch", "chg-steps", 2, "done", "全部完成")
        self.assertEqual(third["state"], "completed")
        with self.assertRaises(InvalidState):
            self.service.record_receipt("dispatch", "chg-steps", 2, "done", "完成后补录")

    def test_failure_goes_to_explicit_rollback(self) -> None:
        self.service.submit_change("plan", self.change_payload("chg-fail"))
        self.service.decide_change("risk-a", "chg-fail", 1, "approved", "同意")
        failed = self.service.record_receipt("dispatch", "chg-fail", 0, "failed", "发现未探明管线")
        self.assertEqual(failed["state"], "failed")
        with self.assertRaises(InvalidState):
            self.service.record_receipt("dispatch", "chg-fail", 1, "done", "失败后继续施工")
        rolling = self.service.begin_rollback("dispatch", "chg-fail", "启动回退方案")
        self.assertEqual(rolling["state"], "rolling_back")
        self.assertEqual(rolling["rollback_round"], 1)
        rolled_back = self.service.record_receipt("dispatch", "chg-fail", 0, "done", "已恢复原状", step_kind="rollback")
        self.assertEqual(rolled_back["state"], "rolled_back")
        with self.assertRaises(InvalidState):
            self.service.begin_rollback("dispatch", "chg-fail", "重复回退")

    def test_failure_can_be_taken_over_manually(self) -> None:
        self.service.submit_change("plan", self.change_payload("chg-manual"))
        self.service.decide_change("risk-a", "chg-manual", 1, "approved", "同意")
        self.service.record_receipt("dispatch", "chg-manual", 0, "failed", "现场塌方")
        taken = self.service.takeover_change("dispatch", "chg-manual", "移交应急小组人工处置")
        self.assertEqual(taken["state"], "manual_takeover")
        with self.assertRaises(InvalidState):
            self.service.record_receipt("dispatch", "chg-manual", 0, "done", "接管后补录施工回执")

    def test_revision_chain_preserves_diffs_and_requires_reapproval(self) -> None:
        self.service.submit_change("plan", self.change_payload("chg-rev"))
        self.service.decide_change("risk-a", "chg-rev", 1, "rejected", "给排水占用过高")
        revised = self.service.revise_change(
            "plan",
            "chg-rev",
            1,
            self.change_payload("chg-rev", demands={"water": "300"}, title="施工调整（降占用）"),
        )
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["diff"]["title"]["to"], "施工调整（降占用）")
        self.assertEqual(revised["diff"]["demands"]["water"], {"op": "changed", "from": "700.000", "to": "300.000"})
        with self.assertRaises(ValidationFailed):
            self.service.revise_change("plan", "chg-rev", 2, self.change_payload("chg-rev", demands={"water": "300"}, title="施工调整（降占用）"))
        with self.assertRaises(InvalidState):
            self.service.decide_change("risk-a", "chg-rev", 1, "approved", "旧版本审批无效")
        with self.assertRaises(InvalidState):
            self.service.record_receipt("dispatch", "chg-rev", 0, "done", "未重新审批")
        self.service.decide_change("risk-a", "chg-rev", 2, "approved", "修订后可行")
        chain = self.service.change_revisions("chg-rev")["revisions"]
        self.assertEqual([item["revision"] for item in chain], [1, 2])
        self.assertIsNone(chain[0]["supersedes_revision_id"])
        self.assertIsNotNone(chain[1]["supersedes_revision_id"])

    def test_approved_change_cannot_be_revised(self) -> None:
        self.service.submit_change("plan", self.change_payload("chg-locked"))
        self.service.decide_change("risk-a", "chg-locked", 1, "approved", "同意")
        with self.assertRaises(InvalidState):
            self.service.revise_change("plan", "chg-locked", 1, self.change_payload("chg-locked", title="改名"))

    def test_service_versions_are_preserved(self) -> None:
        updated = self.service.revise_service(
            "plan",
            "svc-water",
            {"capacity": "1800", "unit": "立方米/日", "note": "水厂扩容前核减", "expected_revision": 1},
        )
        self.assertEqual(updated["revision"], 2)
        versions = self.service.service_versions("svc-water")["versions"]
        self.assertEqual([row["revision"] for row in versions], [1, 2])
        self.assertEqual(versions[0]["capacity"], "2000")
        self.assertEqual(versions[1]["note"], "水厂扩容前核减")
        with self.assertRaises(InvalidState):
            self.service.revise_service("plan", "svc-water", {"capacity": "1700", "unit": "立方米/日", "note": "版本错位", "expected_revision": 1})

    def test_unknown_building_and_foreign_building_are_rejected(self) -> None:
        with self.assertRaises(NotFound):
            self.service.submit_change("plan", self.change_payload("chg-missing", building_ids=["bld-404"]))
        self.service.create_facility("plan", {"facility_id": "settlement-c", "name": "西部安置片区", "kind": "settlement", "timezone": "Asia/Shanghai", "capacity_mu": "100"})
        self.service.register_building("plan", {"building_id": "bld-west", "settlement_id": "settlement-c", "name": "西1号楼", "housing_units": 10, "floors": 6})
        with self.assertRaises(Conflict):
            self.service.submit_change("plan", self.change_payload("chg-foreign", building_ids=["bld-west"]))

    def test_explain_covers_snapshot_approval_and_receipts(self) -> None:
        self.approve_existing()
        self.service.submit_change("plan", self.change_payload("chg-explain"))
        self.service.decide_change("risk-a", "chg-explain", 1, "approved", "接受挤占并通知既有申请")
        self.service.record_receipt("dispatch", "chg-explain", 0, "done", "第一步完成")
        self.service.record_receipt("dispatch", "chg-explain", 1, "done", "第二步完成")
        explain = self.service.explain_change("chg-explain")
        self.assertEqual(explain["state"], "completed")
        self.assertEqual(len(explain["snapshot_sha256"]), 64)
        self.assertEqual(explain["approvals"][0]["decision"], "approved")
        self.assertEqual(len(explain["receipts"]), 2)
        basis_text = "\n".join(explain["decision_basis"])
        self.assertIn("挤占既有申请 chg-old", basis_text)
        self.assertIn("作为整体批准", basis_text)
        self.assertIn("当前状态：completed", basis_text)

    def test_audit_chain_stays_valid_after_construction_flow(self) -> None:
        self.approve_existing()
        self.service.submit_change("plan", self.change_payload("chg-audit"))
        self.service.decide_change("risk-a", "chg-audit", 1, "approved", "同意")
        self.assertTrue(self.service.audit_chain("audit")["valid"])


class ConstructionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk")):
            self.service.create_user(user_id, user_id, role)
        self.app = JsonApplication(self.service)
        self.service.create_facility("plan", {"facility_id": "settlement-b", "name": "东部安置片区", "kind": "settlement", "timezone": "Asia/Shanghai", "capacity_mu": "800000"})
        self.service.register_building("plan", {"building_id": "bld-1", "settlement_id": "settlement-b", "name": "1号楼", "housing_units": 120, "floors": 18})
        self.service.register_service("plan", {"service_id": "svc-water", "settlement_id": "settlement-b", "kind": "water", "capacity": "2000", "unit": "立方米/日"})

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, actor: str, payload: dict[str, object]) -> object:
        return self.app.handle("POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8"))

    def get(self, path: str, actor: str = "plan") -> object:
        return self.app.handle("GET", path, {"X-Actor-Id": actor})

    def test_change_lifecycle_over_http(self) -> None:
        payload = {
            "change_id": "chg-http",
            "settlement_id": "settlement-b",
            "title": "管网改造",
            "window": {"starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-03T18:00:00Z"},
            "demands": {"water": "100"},
            "building_ids": ["bld-1"],
            "steps": ["开挖", "回填"],
            "rollback_plan": {"summary": "恢复", "steps": ["回填恢复"]},
        }
        created = self.post("/construction/changes", "plan", payload)
        self.assertEqual(created.status, 201)
        self.assertEqual(created.body["state"], "pending_review")
        decided = self.post("/construction/changes/chg-http/decide", "risk", {"expected_revision": 1, "decision": "approved", "reason": "同意"})
        self.assertEqual(decided.status, 200)
        receipt = self.post("/construction/changes/chg-http/receipts", "dispatch", {"step_index": 0, "result": "done", "note": "开挖完成"})
        self.assertEqual(receipt.status, 201)
        self.assertEqual(receipt.body["state"], "in_progress")
        out_of_order = self.post("/construction/changes/chg-http/receipts", "dispatch", {"step_index": 0, "result": "done", "note": "重复"})
        self.assertEqual(out_of_order.status, 409)
        assessment = self.get("/construction/changes/chg-http/assessment")
        self.assertEqual(assessment.status, 200)
        self.assertEqual(assessment.body["result"]["constraints"][1]["kind"], "water")
        explain = self.get("/construction/changes/chg-http/explain")
        self.assertEqual(explain.status, 200)
        self.assertTrue(explain.body["decision_basis"])
        revisions = self.get("/construction/changes/chg-http/revisions")
        self.assertEqual(len(revisions.body["revisions"]), 1)
        missing = self.get("/construction/changes/chg-404")
        self.assertEqual(missing.status, 404)

    def test_catalog_routes_over_http(self) -> None:
        buildings = self.get("/construction/buildings?settlement_id=settlement-b")
        self.assertEqual(len(buildings.body["buildings"]), 1)
        services = self.get("/construction/services?settlement_id=settlement-b")
        self.assertEqual(len(services.body["services"]), 1)
        revised = self.post("/construction/services/svc-water/revise", "plan", {"capacity": "1900", "unit": "立方米/日", "note": "核减", "expected_revision": 1})
        self.assertEqual(revised.status, 200)
        versions = self.get("/construction/services/svc-water/versions")
        self.assertEqual(len(versions.body["versions"]), 2)
        forbidden = self.post("/construction/changes", "dispatch", {})
        self.assertEqual(forbidden.status, 403)


class RuralAcceptanceConstructionTests(unittest.TestCase):
    def test_acceptance_covers_construction_change_flow(self) -> None:
        result = acceptance_run(ROOT)
        self.assertEqual(result["status"], "ok")
        construction = result["construction"]
        self.assertEqual(construction["beta_state"], "completed")
        self.assertEqual(construction["gamma_state"], "rolled_back")
        self.assertEqual(construction["beta_conflicts"][0]["change_id"], "chg-water-main")
        self.assertEqual(len(construction["fire_service_versions"]), 2)
        self.assertTrue(construction["explain_beta"]["decision_basis"])
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
