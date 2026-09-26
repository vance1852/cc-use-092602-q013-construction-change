"""安置片区施工变更影响审批的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .impact import (
    CommittedDemand,
    canonical_json,
    compute_impact,
    decimal_text,
    diff_payloads,
    digest,
    quantize_amount,
)
from .models import (
    CONSTRAINTS,
    ApplicationInput,
    BuildingInput,
    ChangeInput,
    ReceiptInput,
    ServiceVersionInput,
    ZoneInput,
)
from .storage import initialize, transaction


ZERO = Decimal("0")

ROLE_PERMISSIONS = {
    "planner": {"zone.write", "service-version.write", "building.write", "report.read"},
    "engineer": {"application.write", "change.write", "change.execute", "report.read"},
    "site": {"receipt.write", "report.read"},
    "approver": {"change.approve", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

# 冻结生效的变更状态：已批准到人工接管期间资源被锁定，终态释放。
FREEZE_ACTIVE_STATES = ("approved", "in_progress", "failed", "rolling_back", "manual_takeover")


class ChangeService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM change_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM change_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO change_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _idempotent(self, scope: str, key: str, request: Mapping[str, Any]) -> dict[str, Any] | None:
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM change_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != digest(request):
            raise Conflict("幂等键对应不同请求内容")
        return json.loads(stored["response_json"])

    def _store_idempotent(self, scope: str, key: str, request: Mapping[str, Any], response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO change_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, digest(request), canonical_json(response), self._now()),
        )

    def _zone(self, zone_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM zones WHERE zone_id=?", (zone_id,)).fetchone()
        if row is None:
            raise NotFound("安置片区不存在")
        return row

    def _change(self, change_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM changes WHERE change_id=?", (change_id,)).fetchone()
        if row is None:
            raise NotFound("施工变更不存在")
        return row

    def _revision(self, change_id: str, revision: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM change_revisions WHERE change_id=? AND revision=?", (change_id, revision)
        ).fetchone()
        if row is None:
            raise NotFound("变更修订不存在")
        return row

    def _current_version(self, zone_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM service_versions WHERE zone_id=? ORDER BY revision DESC LIMIT 1", (zone_id,)
        ).fetchone()
        if row is None:
            raise InvalidState("片区尚未发布公共服务版本")
        return row

    @staticmethod
    def _capacities(version: sqlite3.Row) -> dict[str, Decimal]:
        return {constraint: Decimal(version[constraint]) for constraint in CONSTRAINTS}

    def _committed_demands(self, zone_id: str) -> list[CommittedDemand]:
        rows = self.connection.execute(
            "SELECT * FROM applications WHERE zone_id=? AND state='approved' "
            "ORDER BY priority,created_at,application_id",
            (zone_id,),
        ).fetchall()
        return [
            CommittedDemand(
                application_id=row["application_id"],
                priority=int(row["priority"]),
                created_at=row["created_at"],
                amounts={constraint: Decimal(row[constraint]) for constraint in CONSTRAINTS},
            )
            for row in rows
        ]

    def _active_freezes(self, zone_id: str, now: str) -> dict[str, Decimal]:
        rows = self.connection.execute(
            "SELECT r.payload_json FROM changes c "
            "JOIN change_revisions r ON r.change_id=c.change_id AND r.revision=c.approved_revision "
            "WHERE c.zone_id=? AND c.state IN ('approved','in_progress','failed','rolling_back','manual_takeover')",
            (zone_id,),
        ).fetchall()
        totals = {constraint: ZERO for constraint in CONSTRAINTS}
        for row in rows:
            payload = json.loads(row["payload_json"])
            window = payload["window"]
            if window["starts_at"] <= now <= window["ends_at"]:
                for key, value in payload["freeze_amounts"].items():
                    totals[key] += Decimal(value)
        return totals

    def _assess(self, change: sqlite3.Row, payload: Mapping[str, Any]) -> tuple[sqlite3.Row, dict[str, Any]]:
        version = self._current_version(change["zone_id"])
        capacities = self._capacities(version)
        freeze = {key: Decimal(value) for key, value in payload["freeze_amounts"].items()}
        deltas = {key: Decimal(value) for key, value in payload["capacity_deltas"].items()}
        problems = []
        for constraint in CONSTRAINTS:
            if freeze.get(constraint, ZERO) > capacities[constraint]:
                problems.append(f"{constraint} 冻结量超过设施容量")
            if capacities[constraint] + deltas.get(constraint, ZERO) < ZERO:
                problems.append(f"{constraint} 变更后容量不能为负")
        if problems:
            raise Conflict("；".join(sorted(set(problems))))
        impact = compute_impact(
            capacities=capacities,
            freeze=freeze,
            deltas=deltas,
            committed=self._committed_demands(change["zone_id"]),
        )
        return version, impact

    # ------------------------------------------------------------------
    # 用户与目录
    # ------------------------------------------------------------------
    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO change_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def create_zone(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "zone.write")
        zone = ZoneInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO zones(zone_id,name,timezone,created_at) VALUES(?,?,?,?)",
                    (zone.zone_id, zone.name, zone.timezone, self._now()),
                )
                self._audit("zone", zone.zone_id, "zone.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("安置片区编号已经存在") from exc
        return {"zone_id": zone.zone_id, "name": zone.name, "timezone": zone.timezone}

    def publish_service_version(self, actor_id: str, zone_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "service-version.write")
        self._zone(zone_id)
        version_input = ServiceVersionInput.from_dict(raw)
        latest = self.connection.execute(
            "SELECT * FROM service_versions WHERE zone_id=? ORDER BY revision DESC LIMIT 1", (zone_id,)
        ).fetchone()
        revision = 1 if latest is None else int(latest["revision"]) + 1
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO service_versions(zone_id,revision,housing_units,water_drainage,school_seats,"
                    "road_capacity,fire_coverage,note,source,supersedes_version_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        zone_id,
                        revision,
                        *[
                            decimal_text(quantize_amount(version_input.capacities[constraint]))
                            for constraint in CONSTRAINTS
                        ],
                        version_input.note,
                        "manual",
                        None if latest is None else latest["version_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                version_id = int(cursor.lastrowid)
                self._audit(
                    "service_version",
                    str(version_id),
                    "service_version.published",
                    actor_id,
                    {"zone_id": zone_id, "revision": revision},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("公共服务版本冲突") from exc
        return {"version_id": version_id, "zone_id": zone_id, "revision": revision}

    def list_service_versions(self, actor_id: str, zone_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        self._zone(zone_id)
        rows = self.connection.execute(
            "SELECT * FROM service_versions WHERE zone_id=? ORDER BY revision", (zone_id,)
        ).fetchall()
        return {
            "zone_id": zone_id,
            "versions": [
                {
                    "version_id": row["version_id"],
                    "revision": row["revision"],
                    "capacities": {constraint: row[constraint] for constraint in CONSTRAINTS},
                    "note": row["note"],
                    "source": row["source"],
                    "supersedes_version_id": row["supersedes_version_id"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    def get_current_version(self, actor_id: str, zone_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        self._zone(zone_id)
        row = self.connection.execute(
            "SELECT * FROM service_versions WHERE zone_id=? ORDER BY revision DESC LIMIT 1", (zone_id,)
        ).fetchone()
        if row is None:
            raise NotFound("片区尚未发布公共服务版本")
        return {
            "version_id": row["version_id"],
            "zone_id": zone_id,
            "revision": row["revision"],
            "capacities": {constraint: row[constraint] for constraint in CONSTRAINTS},
            "note": row["note"],
            "source": row["source"],
            "supersedes_version_id": row["supersedes_version_id"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def create_building(self, actor_id: str, zone_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "building.write")
        self._zone(zone_id)
        building = BuildingInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO buildings(building_id,zone_id,name,households,created_at) VALUES(?,?,?,?,?)",
                    (building.building_id, zone_id, building.name, building.households, self._now()),
                )
                self._audit("building", building.building_id, "building.created", actor_id, {"zone_id": zone_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("楼栋编号已经存在") from exc
        return {"building_id": building.building_id, "zone_id": zone_id, "state": "standing"}

    def list_buildings(self, actor_id: str, zone_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        self._zone(zone_id)
        rows = self.connection.execute(
            "SELECT * FROM buildings WHERE zone_id=? ORDER BY building_id", (zone_id,)
        ).fetchall()
        return {"zone_id": zone_id, "buildings": [dict(row) for row in rows]}

    # ------------------------------------------------------------------
    # 既有申请
    # ------------------------------------------------------------------
    def register_application(self, actor_id: str, zone_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "application.write")
        parsed = ApplicationInput.from_dict(raw)
        request = {**raw, "zone_id": zone_id}
        stored = self._idempotent("application", parsed.idempotency_key, request)
        if stored is not None:
            return stored
        self._zone(zone_id)
        version = self._current_version(zone_id)
        capacities = self._capacities(version)
        committed = {constraint: ZERO for constraint in CONSTRAINTS}
        for demand in self._committed_demands(zone_id):
            for constraint in CONSTRAINTS:
                committed[constraint] += demand.amount(constraint)
        freezes = self._active_freezes(zone_id, self._now())
        shortages = []
        for constraint in CONSTRAINTS:
            remaining = capacities[constraint] - committed[constraint] - freezes[constraint]
            demanded = parsed.demands.get(constraint, ZERO)
            if demanded > remaining:
                shortages.append(
                    f"{constraint} 剩余 {decimal_text(quantize_amount(remaining))} 不足 {decimal_text(quantize_amount(demanded))}"
                )
        if shortages:
            raise Conflict("公共服务余量不足或已被施工冻结: " + "；".join(shortages))
        response = {"application_id": parsed.application_id, "zone_id": zone_id, "state": "approved", "revision": 1}
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO applications(application_id,zone_id,applicant,housing_units,water_drainage,"
                    "school_seats,road_capacity,fire_coverage,priority,state,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'approved',?,?,?)",
                    (
                        parsed.application_id,
                        zone_id,
                        parsed.applicant,
                        *[
                            decimal_text(quantize_amount(parsed.demands[constraint]))
                            for constraint in CONSTRAINTS
                        ],
                        parsed.priority,
                        parsed.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._store_idempotent("application", parsed.idempotency_key, request, response)
                self._audit(
                    "application",
                    parsed.application_id,
                    "application.registered",
                    actor_id,
                    {"zone_id": zone_id, "snapshot_version_id": version["version_id"]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("申请编号或幂等键冲突") from exc
        return response

    def get_application(self, actor_id: str, application_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM applications WHERE application_id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise NotFound("申请不存在")
        result = dict(row)
        if row["exclusion_json"]:
            result["exclusion"] = json.loads(row["exclusion_json"])
        return result

    def list_applications(self, actor_id: str, zone_id: str, state: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        self._zone(zone_id)
        if state:
            rows = self.connection.execute(
                "SELECT * FROM applications WHERE zone_id=? AND state=? ORDER BY priority,created_at,application_id",
                (zone_id, state),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM applications WHERE zone_id=? ORDER BY priority,created_at,application_id",
                (zone_id,),
            ).fetchall()
        return {"zone_id": zone_id, "applications": [dict(row) for row in rows]}

    def withdraw_application(self, actor_id: str, application_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "application.write")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE applications SET state='withdrawn',revision=revision+1 "
                "WHERE application_id=? AND state='approved' AND revision=?",
                (application_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("申请不是当前可撤回版本")
            self._audit("application", application_id, "application.withdrawn", actor_id, {})
        return {"application_id": application_id, "state": "withdrawn", "revision": expected_revision + 1}

    # ------------------------------------------------------------------
    # 施工变更：创建、修订、提交
    # ------------------------------------------------------------------
    def _check_buildings(self, zone_id: str, buildings: list[dict[str, str]]) -> None:
        for item in buildings:
            row = self.connection.execute(
                "SELECT zone_id FROM buildings WHERE building_id=?", (item["building_id"],)
            ).fetchone()
            if row is None or row["zone_id"] != zone_id:
                raise ValidationFailed(f"楼栋 {item['building_id']} 不存在或不属于该片区")

    def create_change(self, actor_id: str, zone_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "change.write")
        self._zone(zone_id)
        parsed = ChangeInput.from_dict(raw)
        request = {**raw, "zone_id": zone_id}
        stored = self._idempotent("change", parsed.idempotency_key, request)
        if stored is not None:
            return stored
        self._check_buildings(zone_id, parsed.buildings)
        payload = parsed.payload()
        payload_sha256 = digest(payload)
        diff = diff_payloads({}, payload)
        response = {
            "change_id": parsed.change_id,
            "zone_id": zone_id,
            "state": "draft",
            "revision": 1,
            "payload_sha256": payload_sha256,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO changes(change_id,zone_id,title,state,current_revision,created_by,created_at) "
                    "VALUES(?,?,?,'draft',1,?,?)",
                    (parsed.change_id, zone_id, parsed.title, actor_id, self._now()),
                )
                self.connection.execute(
                    "INSERT INTO change_revisions(change_id,revision,parent_revision,payload_json,diff_json,"
                    "payload_sha256,created_by,created_at) VALUES(?,1,NULL,?,?,?,?,?)",
                    (
                        parsed.change_id,
                        canonical_json(payload),
                        canonical_json(diff),
                        payload_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                self._store_idempotent("change", parsed.idempotency_key, request, response)
                self._audit(
                    "change",
                    parsed.change_id,
                    "change.created",
                    actor_id,
                    {"zone_id": zone_id, "payload_sha256": payload_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("变更编号或幂等键冲突") from exc
        return response

    def revise_change(
        self, actor_id: str, change_id: str, expected_revision: int, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "change.write")
        change = self._change(change_id)
        if change["state"] != "draft":
            raise InvalidState("只有草稿状态可以修订；已提交变更请先撤回再重新登记")
        if change["current_revision"] != expected_revision:
            raise InvalidState("变更不是当前修订版本")
        parsed = ChangeInput.from_dict(raw, require_idempotency_key=False)
        if parsed.change_id != change_id:
            raise ValidationFailed("修订载荷的 change_id 必须与被修改变更一致")
        self._check_buildings(change["zone_id"], parsed.buildings)
        previous = self._revision(change_id, expected_revision)
        old_payload = json.loads(previous["payload_json"])
        new_payload = parsed.payload()
        diff = diff_payloads(old_payload, new_payload)
        if not diff:
            raise ValidationFailed("修订没有实际变化")
        new_revision = expected_revision + 1
        payload_sha256 = digest(new_payload)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO change_revisions(change_id,revision,parent_revision,payload_json,diff_json,"
                "payload_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    change_id,
                    new_revision,
                    expected_revision,
                    canonical_json(new_payload),
                    canonical_json(diff),
                    payload_sha256,
                    actor_id,
                    self._now(),
                ),
            )
            cursor = self.connection.execute(
                "UPDATE changes SET current_revision=?,title=? WHERE change_id=? AND current_revision=? AND state='draft'",
                (new_revision, parsed.title, change_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更不是当前修订版本")
            self._audit(
                "change",
                change_id,
                "change.revised",
                actor_id,
                {"revision": new_revision, "diff_keys": sorted(diff), "payload_sha256": payload_sha256},
            )
        return {"change_id": change_id, "revision": new_revision, "diff": diff, "payload_sha256": payload_sha256}

    def submit_change(self, actor_id: str, change_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "change.write")
        change = self._change(change_id)
        if change["state"] not in ("draft", "submitted"):
            raise InvalidState("只有草稿或已提交状态可以提交影响评估")
        if change["current_revision"] != expected_revision:
            raise InvalidState("变更不是当前修订版本")
        revision = self._revision(change_id, expected_revision)
        payload = json.loads(revision["payload_json"])
        version, impact = self._assess(change, payload)
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE change_revisions SET impact_json=?,snapshot_version_id=? WHERE change_id=? AND revision=?",
                (canonical_json(impact), version["version_id"], change_id, expected_revision),
            )
            self.connection.execute(
                "UPDATE changes SET state='submitted',submitted_by=?,submitted_at=? WHERE change_id=?",
                (actor_id, now, change_id),
            )
            self._audit(
                "change",
                change_id,
                "change.submitted",
                actor_id,
                {
                    "revision": expected_revision,
                    "snapshot_version_id": version["version_id"],
                    "excluded_application_ids": impact["excluded_application_ids"],
                },
            )
        return {
            "change_id": change_id,
            "state": "submitted",
            "revision": expected_revision,
            "snapshot_version_id": version["version_id"],
            "impact": impact,
        }

    def cancel_change(self, actor_id: str, change_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "change.write")
        change = self._change(change_id)
        if change["state"] not in ("draft", "submitted"):
            raise InvalidState("只有草稿或已提交状态可以撤回")
        if change["current_revision"] != expected_revision:
            raise InvalidState("变更不是当前修订版本")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE changes SET state='cancelled',closed_at=? WHERE change_id=?", (self._now(), change_id)
            )
            self._audit("change", change_id, "change.cancelled", actor_id, {"revision": expected_revision})
        return {"change_id": change_id, "state": "cancelled"}

    # ------------------------------------------------------------------
    # 整体审批
    # ------------------------------------------------------------------
    def _decision_guard(self, actor_id: str, change: sqlite3.Row, expected_revision: int) -> sqlite3.Row:
        if change["state"] != "submitted":
            raise InvalidState("只有已提交变更可以审批")
        if change["current_revision"] != expected_revision:
            raise InvalidState("变更不是当前修订版本")
        if change["submitted_by"] == actor_id or change["created_by"] == actor_id:
            raise Forbidden("施工窗口、资源冻结及回退方案必须由他人整体审批")
        revision = self._revision(change["change_id"], expected_revision)
        if revision["impact_json"] is None:
            raise InvalidState("尚未提交影响评估")
        return revision

    def _fresh_impact(self, change: sqlite3.Row, revision: sqlite3.Row) -> tuple[sqlite3.Row, dict[str, Any]]:
        version, impact = self._assess(change, json.loads(revision["payload_json"]))
        if version["version_id"] != revision["snapshot_version_id"]:
            raise Conflict("设施快照已变化，请重新提交影响评估")
        stored = json.loads(revision["impact_json"])
        if canonical_json(impact["constraints"]) != canonical_json(stored["constraints"]) or (
            impact["excluded_application_ids"] != stored["excluded_application_ids"]
        ):
            raise Conflict("既有申请已变化，影响评估需重新提交")
        return version, impact

    def approve_change(self, actor_id: str, change_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "change.approve")
        change = self._change(change_id)
        revision = self._decision_guard(actor_id, change, expected_revision)
        _, impact = self._fresh_impact(change, revision)
        payload = json.loads(revision["payload_json"])
        bundle_sha256 = digest({"revision": expected_revision, "payload": payload})
        now = self._now()
        with transaction(self.connection, immediate=True):
            conflicts = {item["application_id"]: item for item in impact["conflicts"]}
            for application_id, conflict in conflicts.items():
                cursor = self.connection.execute(
                    "UPDATE applications SET state='excluded',excluded_by=?,exclusion_json=?,revision=revision+1 "
                    "WHERE application_id=? AND state='approved'",
                    (change_id, canonical_json(conflict), application_id),
                )
                if cursor.rowcount != 1:
                    raise Conflict(f"申请 {application_id} 状态已变化，影响评估需重新提交")
            for seq, step in enumerate(payload["execution_steps"], start=1):
                self.connection.execute(
                    "INSERT INTO change_steps(change_id,phase,seq,step_key,title) VALUES(?,'execution',?,?,?)",
                    (change_id, seq, step["step_key"], step["title"]),
                )
            self.connection.execute(
                "UPDATE changes SET state='approved',approved_revision=?,bundle_sha256=?,decided_by=?,decided_at=?,"
                "decision_reason=NULL WHERE change_id=?",
                (expected_revision, bundle_sha256, actor_id, now, change_id),
            )
            self._audit(
                "change",
                change_id,
                "change.approved",
                actor_id,
                {
                    "revision": expected_revision,
                    "bundle_sha256": bundle_sha256,
                    "excluded_application_ids": impact["excluded_application_ids"],
                },
            )
        return {
            "change_id": change_id,
            "state": "approved",
            "approved_revision": expected_revision,
            "bundle_sha256": bundle_sha256,
            "excluded_application_ids": impact["excluded_application_ids"],
        }

    def reject_change(
        self, actor_id: str, change_id: str, expected_revision: int, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "change.approve")
        change = self._change(change_id)
        self._decision_guard(actor_id, change, expected_revision)
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("驳回原因不能为空")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE changes SET state='rejected',decided_by=?,decided_at=?,decision_reason=?,closed_at=? "
                "WHERE change_id=?",
                (actor_id, self._now(), self._now(), reason.strip(), change_id),
            )
            self._audit(
                "change", change_id, "change.rejected", actor_id, {"revision": expected_revision, "reason": reason.strip()}
            )
        return {"change_id": change_id, "state": "rejected", "reason": reason.strip()}

    # ------------------------------------------------------------------
    # 现场执行与回执
    # ------------------------------------------------------------------
    def start_execution(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "change.execute")
        change = self._change(change_id)
        if change["state"] != "approved":
            raise InvalidState("只有已批准变更可以开工")
        payload = json.loads(self._revision(change_id, change["approved_revision"])["payload_json"])
        now = self._now()
        if not payload["window"]["starts_at"] <= now <= payload["window"]["ends_at"]:
            raise InvalidState("当前时间不在施工窗口内")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE changes SET state='in_progress' WHERE change_id=? AND state='approved'", (change_id,)
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有已批准变更可以开工")
            self._audit("change", change_id, "change.started", actor_id, {"window": payload["window"]})
        return {"change_id": change_id, "state": "in_progress"}

    def _complete_change(
        self, change: sqlite3.Row, payload: Mapping[str, Any], actor_id: str, now: str
    ) -> int:
        zone_id = change["zone_id"]
        version = self._current_version(zone_id)
        capacities = self._capacities(version)
        deltas = {key: Decimal(value) for key, value in payload["capacity_deltas"].items()}
        new_capacities = {
            constraint: quantize_amount(capacities[constraint] + deltas.get(constraint, ZERO))
            for constraint in CONSTRAINTS
        }
        cursor = self.connection.execute(
            "INSERT INTO service_versions(zone_id,revision,housing_units,water_drainage,school_seats,"
            "road_capacity,fire_coverage,note,source,supersedes_version_id,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                zone_id,
                int(version["revision"]) + 1,
                *[decimal_text(new_capacities[constraint]) for constraint in CONSTRAINTS],
                f"施工变更 {change['change_id']} 完成自动发布",
                f"change:{change['change_id']}",
                version["version_id"],
                actor_id,
                now,
            ),
        )
        result_version_id = int(cursor.lastrowid)
        for item in payload["buildings"]:
            action = item["action"]
            if action == "demolish":
                self.connection.execute(
                    "UPDATE buildings SET state='demolished' WHERE building_id=?", (item["building_id"],)
                )
            elif action in ("renovate", "extend"):
                self.connection.execute(
                    "UPDATE buildings SET state='modified' WHERE building_id=? AND state='standing'",
                    (item["building_id"],),
                )
        self.connection.execute(
            "UPDATE changes SET state='completed',result_version_id=?,closed_at=? WHERE change_id=?",
            (result_version_id, now, change["change_id"]),
        )
        self._audit(
            "change",
            change["change_id"],
            "change.completed",
            actor_id,
            {"result_version_id": result_version_id},
        )
        return result_version_id

    def _restore_excluded(self, change_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT application_id FROM applications WHERE excluded_by=? AND state='excluded'", (change_id,)
        ).fetchall()
        restored = [row["application_id"] for row in rows]
        self.connection.execute(
            "UPDATE applications SET state='approved',excluded_by=NULL,exclusion_json=NULL,revision=revision+1 "
            "WHERE excluded_by=? AND state='excluded'",
            (change_id,),
        )
        return restored

    def post_receipt(self, actor_id: str, change_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        receipt = ReceiptInput.from_dict(raw)
        request = {**raw, "change_id": change_id}
        stored = self._idempotent("receipt", receipt.idempotency_key, request)
        if stored is not None:
            return stored
        change = self._change(change_id)
        if change["state"] == "in_progress":
            phase = "execution"
        elif change["state"] == "rolling_back":
            phase = "rollback"
        else:
            raise InvalidState("变更当前不能接收现场回执")
        steps = self.connection.execute(
            "SELECT * FROM change_steps WHERE change_id=? AND phase=? ORDER BY seq", (change_id, phase)
        ).fetchall()
        pending = [step for step in steps if step["state"] == "pending"]
        if not pending:
            raise InvalidState("当前阶段没有待回执步骤")
        current = pending[0]
        if current["step_key"] != receipt.step_key:
            raise InvalidState(f"下一步应回执步骤 {current['step_key']}")
        now = self._now()
        response: dict[str, Any] = {
            "change_id": change_id,
            "phase": phase,
            "step_key": receipt.step_key,
            "outcome": receipt.outcome,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE change_steps SET state=?,receipt_note=?,receipt_by=?,receipt_at=? "
                "WHERE change_id=? AND phase=? AND seq=? AND state='pending'",
                (
                    "done" if receipt.outcome == "done" else "failed",
                    receipt.note,
                    actor_id,
                    now,
                    change_id,
                    phase,
                    current["seq"],
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidState("步骤状态已变化")
            if receipt.outcome == "failed":
                self.connection.execute(
                    "UPDATE changes SET state='failed',failed_phase=?,failed_step=?,failure_note=? WHERE change_id=?",
                    (phase, receipt.step_key, receipt.note, change_id),
                )
                self._audit(
                    "change",
                    change_id,
                    "change.step_failed",
                    actor_id,
                    {"phase": phase, "step_key": receipt.step_key, "note": receipt.note},
                )
                response["state"] = "failed"
                response["next"] = "rollback 或 takeover"
            else:
                remaining = len(pending) - 1
                if remaining > 0:
                    response["state"] = change["state"]
                    response["next_step_key"] = pending[1]["step_key"]
                elif phase == "execution":
                    payload = json.loads(self._revision(change_id, change["approved_revision"])["payload_json"])
                    result_version_id = self._complete_change(change, payload, actor_id, now)
                    response["state"] = "completed"
                    response["result_version_id"] = result_version_id
                else:
                    restored = self._restore_excluded(change_id)
                    self.connection.execute(
                        "UPDATE changes SET state='rolled_back',closed_at=? WHERE change_id=?", (now, change_id)
                    )
                    self._audit(
                        "change", change_id, "change.rolled_back", actor_id, {"restored_application_ids": restored}
                    )
                    response["state"] = "rolled_back"
                    response["restored_application_ids"] = restored
            self._store_idempotent("receipt", receipt.idempotency_key, request, response)
        return response

    def begin_rollback(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "change.execute")
        change = self._change(change_id)
        if change["state"] != "failed":
            raise InvalidState("只有失败变更可以转入回退")
        if change["failed_phase"] != "execution":
            raise InvalidState("回退步骤再次失败，只能人工接管")
        payload = json.loads(self._revision(change_id, change["approved_revision"])["payload_json"])
        with transaction(self.connection, immediate=True):
            for seq, step in enumerate(payload["rollback_plan"]["steps"], start=1):
                self.connection.execute(
                    "INSERT INTO change_steps(change_id,phase,seq,step_key,title) VALUES(?,'rollback',?,?,?)",
                    (change_id, seq, step["step_key"], step["title"]),
                )
            self.connection.execute(
                "UPDATE changes SET state='rolling_back' WHERE change_id=? AND state='failed'", (change_id,)
            )
            self._audit(
                "change",
                change_id,
                "change.rollback_started",
                actor_id,
                {"failed_step": change["failed_step"], "rollback_steps": len(payload["rollback_plan"]["steps"])},
            )
        return {"change_id": change_id, "state": "rolling_back"}

    def begin_takeover(self, actor_id: str, change_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "change.execute")
        change = self._change(change_id)
        if change["state"] != "failed":
            raise InvalidState("只有失败变更可以转入人工接管")
        if not isinstance(note, str) or not note.strip():
            raise ValidationFailed("接管说明不能为空")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE changes SET state='manual_takeover',takeover_by=?,takeover_note=? WHERE change_id=? AND state='failed'",
                (actor_id, note.strip(), change_id),
            )
            self._audit(
                "change",
                change_id,
                "change.takeover_started",
                actor_id,
                {"failed_step": change["failed_step"], "note": note.strip()},
            )
        return {"change_id": change_id, "state": "manual_takeover", "takeover_by": actor_id}

    def close_takeover(self, actor_id: str, change_id: str, report: str) -> dict[str, Any]:
        self._require(actor_id, "change.execute")
        change = self._change(change_id)
        if change["state"] != "manual_takeover":
            raise InvalidState("只有人工接管中的变更可以结案")
        if not isinstance(report, str) or not report.strip():
            raise ValidationFailed("结案报告不能为空")
        now = self._now()
        with transaction(self.connection, immediate=True):
            restored = self._restore_excluded(change_id)
            self.connection.execute(
                "UPDATE changes SET state='manual_closed',takeover_report=?,closed_at=? WHERE change_id=?",
                (report.strip(), now, change_id),
            )
            self._audit(
                "change",
                change_id,
                "change.manual_closed",
                actor_id,
                {"report": report.strip(), "restored_application_ids": restored},
            )
        return {"change_id": change_id, "state": "manual_closed", "restored_application_ids": restored}

    # ------------------------------------------------------------------
    # 查询与解释
    # ------------------------------------------------------------------
    def get_change(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        change = self._change(change_id)
        revision = self._revision(change_id, change["current_revision"])
        steps = self.connection.execute(
            "SELECT phase,seq,step_key,title,state,receipt_by,receipt_at FROM change_steps "
            "WHERE change_id=? ORDER BY phase,seq",
            (change_id,),
        ).fetchall()
        result = dict(change)
        result["payload"] = json.loads(revision["payload_json"])
        result["steps"] = [dict(step) for step in steps]
        return result

    def change_revisions(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        self._change(change_id)
        rows = self.connection.execute(
            "SELECT * FROM change_revisions WHERE change_id=? ORDER BY revision", (change_id,)
        ).fetchall()
        return {
            "change_id": change_id,
            "revisions": [
                {
                    "revision": row["revision"],
                    "parent_revision": row["parent_revision"],
                    "payload_sha256": row["payload_sha256"],
                    "diff": json.loads(row["diff_json"]),
                    "snapshot_version_id": row["snapshot_version_id"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    def change_impact(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        change = self._change(change_id)
        revision = self._revision(change_id, change["current_revision"])
        if revision["impact_json"] is None:
            raise NotFound("变更尚未提交影响评估")
        return {
            "change_id": change_id,
            "revision": change["current_revision"],
            "snapshot_version_id": revision["snapshot_version_id"],
            "impact": json.loads(revision["impact_json"]),
        }

    def explain_change(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        change = self._change(change_id)
        decisions: list[dict[str, Any]] = []
        revision = self._revision(change_id, change["current_revision"])
        if revision["impact_json"] is not None:
            impact = json.loads(revision["impact_json"])
            version = self.connection.execute(
                "SELECT * FROM service_versions WHERE version_id=?", (revision["snapshot_version_id"],)
            ).fetchone()
            decisions.append({
                "decision": "impact_assessment",
                "basis": (
                    f"依据公共服务版本第{version['revision']}版(version_id={version['version_id']})"
                    f"与{impact['committed_count']}份既有申请计算五项约束剩余量"
                ),
                "snapshot_version_id": version["version_id"],
                "rule": impact["rule"],
                "excluded_application_ids": impact["excluded_application_ids"],
            })
            for conflict in impact["conflicts"]:
                decisions.append({
                    "decision": "application_excluded",
                    "application_id": conflict["application_id"],
                    "basis": conflict["reason"],
                    "constraints": conflict["constraints"],
                    "amounts": conflict["amounts"],
                })
        if change["state"] in ("approved", "in_progress", "completed", "failed", "rolling_back", "rolled_back", "manual_takeover", "manual_closed"):
            decisions.append({
                "decision": "bundle_approved",
                "basis": "施工窗口、资源冻结及回退方案作为整体由他人批准",
                "approved_revision": change["approved_revision"],
                "bundle_sha256": change["bundle_sha256"],
                "decided_by": change["decided_by"],
                "decided_at": change["decided_at"],
            })
        if change["state"] == "rejected":
            decisions.append({
                "decision": "bundle_rejected",
                "basis": change["decision_reason"],
                "decided_by": change["decided_by"],
                "decided_at": change["decided_at"],
            })
        steps = self.connection.execute(
            "SELECT * FROM change_steps WHERE change_id=? AND state<>'pending' ORDER BY phase,seq", (change_id,)
        ).fetchall()
        for step in steps:
            decisions.append({
                "decision": f"receipt_{step['state']}",
                "basis": f"{step['phase']}阶段第{step['seq']}步 {step['step_key']} 现场回执{step['state']}",
                "receipt_by": step["receipt_by"],
                "receipt_at": step["receipt_at"],
                "note": step["receipt_note"],
            })
        if change["failed_step"]:
            decisions.append({
                "decision": "step_failed",
                "basis": f"{change['failed_phase']}阶段步骤 {change['failed_step']} 失败: {change['failure_note']}",
            })
        if change["takeover_by"]:
            decisions.append({
                "decision": "manual_takeover",
                "basis": change["takeover_note"],
                "takeover_by": change["takeover_by"],
            })
        if change["state"] == "completed":
            decisions.append({
                "decision": "completed",
                "basis": f"全部执行步骤回执确认，容量变更发布为公共服务版本 version_id={change['result_version_id']}",
                "result_version_id": change["result_version_id"],
            })
        if change["state"] == "rolled_back":
            decisions.append({
                "decision": "rolled_back",
                "basis": "回退方案全部步骤回执确认，被排斥申请已恢复",
            })
        if change["state"] == "manual_closed":
            decisions.append({
                "decision": "manual_closed",
                "basis": change["takeover_report"],
            })
        return {
            "change_id": change_id,
            "zone_id": change["zone_id"],
            "state": change["state"],
            "current_revision": change["current_revision"],
            "decisions": decisions,
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM change_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
