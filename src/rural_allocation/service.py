"""补偿单价、土地库存、地块资源池和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .construction import (
    REVISABLE_STATES,
    Building,
    ChangePayload,
    PublicService,
    ResourceApplication,
    assess_constraints,
    payload_diff,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    Route,
    SupplyScenario,
    decimal_value,
    identifier,
    required_text,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run", "construction.write"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write", "construction.receipt"},
    "risk": {"outage.write", "scenario.approve", "report.read", "construction.write", "construction.approve"},
    "auditor": {"report.read", "audit.read"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
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
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
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
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
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

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM market_index_quotes WHERE market_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.market_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO market_index_quotes(market_index,trade_date,close_cny,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.market_index,
                        quote.trade_date,
                        decimal_text(quote.close_cny),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"market_index": quote.market_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("补偿单价版本冲突") from exc
        return {"quote_id": quote_id, "market_index": quote.market_index, "trade_date": quote.trade_date}

    def price_summary(self, market_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_cny FROM market_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM market_index_quotes "
            "WHERE market_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (market_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_cny"])) for row in rows]
        if not points:
            raise NotFound("没有基准补偿单价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "market_index": market_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_cny": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_mu,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_mu),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("地块资源池编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("地块资源池不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_mu,available_mu,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_mu),
                        decimal_text(lot.quantity_mu),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("土地资源批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("土地资源批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("地块资源池当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_mu,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_mu),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("地块资源池不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_mu"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_mu"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_mu=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_mu"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可移交版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("土地资源批次不存在")
        allocated = Decimal(nomination["allocated_mu"])
        available = Decimal(lot["available_mu"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("土地资源批次与地块资源池起点或土地类型不匹配")
        if available < allocated:
            raise Conflict("土地库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_mu=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,surveyed_mu,"
                "expected_delivered_mu,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "surveyed_mu": decimal_text(allocated),
            "expected_delivered_mu": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_cny FROM market_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用补偿单价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_mu AS REAL)) available_mu "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_cny"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_cny"]),
            market_index_drop_percent=scenario.market_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
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

    # ---- 安置片区施工变更影响审批 ----

    def _settlement(self, settlement_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM facilities WHERE facility_id=?", (settlement_id,)
        ).fetchone()
        if row is None:
            raise NotFound("安置片区不存在")
        if row["kind"] != "settlement":
            raise ValidationFailed("目标设施不是安置片区")
        return row

    def register_building(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        building = Building.from_dict(raw)
        self._settlement(building.settlement_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO construction_buildings(building_id,settlement_id,name,housing_units,floors,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        building.building_id,
                        building.settlement_id,
                        building.name,
                        building.housing_units,
                        building.floors,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("construction_building", building.building_id, "building.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("楼栋编号已经存在") from exc
        return self.building(building.building_id)

    def building(self, building_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM construction_buildings WHERE building_id=?", (building_id,)
        ).fetchone()
        if row is None:
            raise NotFound("楼栋不存在")
        return dict(row)

    def list_buildings(self, settlement_id: str) -> dict[str, Any]:
        self._settlement(settlement_id)
        rows = self.connection.execute(
            "SELECT * FROM construction_buildings WHERE settlement_id=? ORDER BY building_id",
            (settlement_id,),
        ).fetchall()
        return {"settlement_id": settlement_id, "buildings": [dict(row) for row in rows]}

    def register_service(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        service = PublicService.from_dict(raw)
        self._settlement(service.settlement_id)
        note = required_text(raw.get("note", "初始登记"), "note")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO construction_services(service_id,settlement_id,kind,capacity,unit,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        service.service_id,
                        service.settlement_id,
                        service.kind,
                        decimal_text(service.capacity),
                        service.unit,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO construction_service_versions(service_id,revision,capacity,unit,note,"
                    "changed_by,changed_at) VALUES(?,?,?,?,?,?,?)",
                    (service.service_id, 1, decimal_text(service.capacity), service.unit, note, actor_id, self._now()),
                )
                self._audit("construction_service", service.service_id, "service.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("公共服务编号或该片区的约束类型已经存在") from exc
        return self.public_service(service.service_id)

    def public_service(self, service_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM construction_services WHERE service_id=?", (service_id,)
        ).fetchone()
        if row is None:
            raise NotFound("公共服务不存在")
        return dict(row)

    def list_services(self, settlement_id: str) -> dict[str, Any]:
        self._settlement(settlement_id)
        rows = self.connection.execute(
            "SELECT * FROM construction_services WHERE settlement_id=? ORDER BY kind",
            (settlement_id,),
        ).fetchall()
        return {"settlement_id": settlement_id, "services": [dict(row) for row in rows]}

    def service_versions(self, service_id: str) -> dict[str, Any]:
        self.public_service(service_id)
        rows = self.connection.execute(
            "SELECT * FROM construction_service_versions WHERE service_id=? ORDER BY revision",
            (service_id,),
        ).fetchall()
        return {"service_id": service_id, "versions": [dict(row) for row in rows]}

    def revise_service(self, actor_id: str, service_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        row = self.public_service(service_id)
        capacity = decimal_value(raw.get("capacity"), "capacity", minimum=Decimal("0"))
        unit = required_text(raw.get("unit"), "unit", 16)
        note = required_text(raw.get("note"), "note")
        expected = raw.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != row["revision"]:
            raise InvalidState("公共服务不是当前版本")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE construction_services SET capacity=?,unit=?,revision=revision+1 "
                "WHERE service_id=? AND revision=?",
                (decimal_text(capacity), unit, service_id, row["revision"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("公共服务不是当前版本")
            self.connection.execute(
                "INSERT INTO construction_service_versions(service_id,revision,capacity,unit,note,"
                "changed_by,changed_at) VALUES(?,?,?,?,?,?,?)",
                (service_id, row["revision"] + 1, decimal_text(capacity), unit, note, actor_id, self._now()),
            )
            self._audit(
                "construction_service",
                service_id,
                "service.revised",
                actor_id,
                {"revision": row["revision"] + 1, "note": note},
            )
        return self.public_service(service_id)

    def _check_buildings(self, settlement_id: str, building_ids: Iterable[str]) -> None:
        ids = tuple(building_ids)
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT building_id,settlement_id FROM construction_buildings WHERE building_id IN ({placeholders})",
            ids,
        ).fetchall()
        found = {row["building_id"]: row for row in rows}
        for building_id in ids:
            if building_id not in found:
                raise NotFound(f"楼栋 {building_id} 不存在")
            if found[building_id]["settlement_id"] != settlement_id:
                raise Conflict(f"楼栋 {building_id} 不属于该安置片区")

    def _facility_snapshot(
        self,
        settlement_id: str,
        payload: ChangePayload,
        exclude_change_id: str,
    ) -> tuple[dict[str, Decimal], list[ResourceApplication], dict[str, Any]]:
        services = self.connection.execute(
            "SELECT * FROM construction_services WHERE settlement_id=? ORDER BY kind",
            (settlement_id,),
        ).fetchall()
        buildings = self.connection.execute(
            "SELECT * FROM construction_buildings WHERE settlement_id=? ORDER BY building_id",
            (settlement_id,),
        ).fetchall()
        others = self.connection.execute(
            "SELECT change_id,payload_json,priority,submitted_at FROM construction_changes "
            "WHERE settlement_id=? AND state IN ('approved','in_progress') AND change_id<>? "
            "ORDER BY priority,submitted_at,change_id",
            (settlement_id, exclude_change_id),
        ).fetchall()
        start = parse_utc(payload.window_starts_at, "window.starts_at")
        end = parse_utc(payload.window_ends_at, "window.ends_at")
        applications: list[ResourceApplication] = []
        application_rows: list[dict[str, Any]] = []
        for row in others:
            other = json.loads(row["payload_json"])
            other_start = parse_utc(other["window"]["starts_at"], "window.starts_at")
            other_end = parse_utc(other["window"]["ends_at"], "window.ends_at")
            if other_start >= end or start >= other_end:
                continue
            demands = {kind: Decimal(value) for kind, value in other["demands"].items()}
            applications.append(
                ResourceApplication(row["change_id"], int(row["priority"]), row["submitted_at"], demands)
            )
            application_rows.append({
                "change_id": row["change_id"],
                "priority": int(row["priority"]),
                "submitted_at": row["submitted_at"],
                "window": other["window"],
                "demands": other["demands"],
            })
        capacities = {row["kind"]: Decimal(row["capacity"]) for row in services}
        snapshot = {
            "settlement_id": settlement_id,
            "taken_at": self._now(),
            "services": [
                {
                    "service_id": row["service_id"],
                    "kind": row["kind"],
                    "capacity": row["capacity"],
                    "unit": row["unit"],
                    "revision": int(row["revision"]),
                }
                for row in services
            ],
            "buildings": [
                {
                    "building_id": row["building_id"],
                    "name": row["name"],
                    "housing_units": int(row["housing_units"]),
                    "state": row["state"],
                    "revision": int(row["revision"]),
                }
                for row in buildings
            ],
            "applications": application_rows,
        }
        return capacities, applications, snapshot

    def _assessment_view(
        self,
        payload: ChangePayload,
        capacities: Mapping[str, Decimal],
        applications: list[ResourceApplication],
    ) -> dict[str, Any]:
        result = assess_constraints(capacities=capacities, applications=applications, demands=payload.demands)
        placeholders = ",".join("?" for _ in payload.building_ids)
        rows = self.connection.execute(
            f"SELECT building_id,name,housing_units FROM construction_buildings "
            f"WHERE building_id IN ({placeholders}) ORDER BY building_id",
            tuple(payload.building_ids),
        ).fetchall()
        return {
            "window": {"starts_at": payload.window_starts_at, "ends_at": payload.window_ends_at},
            "affected_buildings": [
                {
                    "building_id": row["building_id"],
                    "name": row["name"],
                    "housing_units": int(row["housing_units"]),
                }
                for row in rows
            ],
            "affected_housing_units": sum(int(row["housing_units"]) for row in rows),
            **result,
        }

    def _store_assessment(
        self,
        change_id: str,
        revision: int,
        snapshot: Mapping[str, Any],
        snapshot_sha256: str,
        assessment: Mapping[str, Any],
    ) -> None:
        self.connection.execute(
            "INSERT INTO construction_assessments(change_id,revision,snapshot_json,snapshot_sha256,"
            "result_json,created_at) VALUES(?,?,?,?,?,?)",
            (change_id, revision, canonical_json(snapshot), snapshot_sha256, canonical_json(assessment), self._now()),
        )

    def submit_change(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "construction.write")
        change_id = identifier(raw.get("change_id"), "change_id")
        settlement_id = identifier(raw.get("settlement_id"), "settlement_id")
        self._settlement(settlement_id)
        payload = ChangePayload.from_dict(raw)
        canonical = payload.canonical()
        with transaction(self.connection, immediate=True):
            self._check_buildings(settlement_id, payload.building_ids)
            capacities, applications, snapshot = self._facility_snapshot(settlement_id, payload, change_id)
            assessment = self._assessment_view(payload, capacities, applications)
            snapshot_sha256 = digest(snapshot)
            try:
                self.connection.execute(
                    "INSERT INTO construction_changes(change_id,settlement_id,title,payload_json,state,revision,"
                    "rollback_round,priority,submitted_by,revised_by,submitted_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        change_id,
                        settlement_id,
                        payload.title,
                        canonical_json(canonical),
                        "pending_review",
                        1,
                        0,
                        payload.priority,
                        actor_id,
                        actor_id,
                        self._now(),
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("施工变更编号已经存在") from exc
            self.connection.execute(
                "INSERT INTO construction_change_revisions(change_id,revision,payload_json,diff_json,"
                "supersedes_revision_id,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    change_id,
                    1,
                    canonical_json(canonical),
                    canonical_json(payload_diff({}, canonical)),
                    None,
                    actor_id,
                    self._now(),
                ),
            )
            self._store_assessment(change_id, 1, snapshot, snapshot_sha256, assessment)
            self._audit(
                "construction_change",
                change_id,
                "change.submitted",
                actor_id,
                {"revision": 1, "snapshot_sha256": snapshot_sha256},
            )
        return {
            "change_id": change_id,
            "state": "pending_review",
            "revision": 1,
            "snapshot_sha256": snapshot_sha256,
            "assessment": assessment,
        }

    def revise_change(
        self,
        actor_id: str,
        change_id: str,
        expected_revision: int,
        raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require(actor_id, "construction.write")
        row = self.connection.execute(
            "SELECT * FROM construction_changes WHERE change_id=?", (change_id,)
        ).fetchone()
        if row is None:
            raise NotFound("施工变更不存在")
        if int(row["revision"]) != expected_revision:
            raise InvalidState("变更不是当前版本")
        if row["state"] not in REVISABLE_STATES:
            raise InvalidState("当前状态不能修订施工变更")
        payload = ChangePayload.from_dict(raw)
        canonical = payload.canonical()
        diff = payload_diff(json.loads(row["payload_json"]), canonical)
        if not diff:
            raise ValidationFailed("修订必须包含与上一版的差异")
        new_revision = int(row["revision"]) + 1
        with transaction(self.connection, immediate=True):
            self._check_buildings(row["settlement_id"], payload.building_ids)
            capacities, applications, snapshot = self._facility_snapshot(row["settlement_id"], payload, change_id)
            assessment = self._assessment_view(payload, capacities, applications)
            snapshot_sha256 = digest(snapshot)
            cursor = self.connection.execute(
                "UPDATE construction_changes SET title=?,payload_json=?,state='pending_review',revision=?,"
                "failure_reason=NULL,priority=?,revised_by=?,updated_at=? WHERE change_id=? AND revision=?",
                (
                    payload.title,
                    canonical_json(canonical),
                    new_revision,
                    payload.priority,
                    actor_id,
                    self._now(),
                    change_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更不是当前版本")
            previous = self.connection.execute(
                "SELECT revision_id FROM construction_change_revisions WHERE change_id=? AND revision=?",
                (change_id, expected_revision),
            ).fetchone()
            self.connection.execute(
                "INSERT INTO construction_change_revisions(change_id,revision,payload_json,diff_json,"
                "supersedes_revision_id,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    change_id,
                    new_revision,
                    canonical_json(canonical),
                    canonical_json(diff),
                    previous["revision_id"],
                    actor_id,
                    self._now(),
                ),
            )
            self._store_assessment(change_id, new_revision, snapshot, snapshot_sha256, assessment)
            self._audit(
                "construction_change",
                change_id,
                "change.revised",
                actor_id,
                {"revision": new_revision, "diff": diff, "snapshot_sha256": snapshot_sha256},
            )
        return {
            "change_id": change_id,
            "state": "pending_review",
            "revision": new_revision,
            "diff": diff,
            "snapshot_sha256": snapshot_sha256,
            "assessment": assessment,
        }

    def decide_change(
        self,
        actor_id: str,
        change_id: str,
        expected_revision: int,
        decision: str,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "construction.approve")
        if decision not in {"approved", "rejected"}:
            raise ValidationFailed("decision 必须是 approved 或 rejected")
        reason_text = required_text(reason, "reason")
        row = self.connection.execute(
            "SELECT * FROM construction_changes WHERE change_id=?", (change_id,)
        ).fetchone()
        if row is None:
            raise NotFound("施工变更不存在")
        if actor_id == row["revised_by"]:
            raise Forbidden("施工窗口、资源冻结及回退方案必须由他人整体审批")
        if row["state"] != "pending_review" or int(row["revision"]) != expected_revision:
            raise InvalidState("变更不是当前待审版本")
        payload = json.loads(row["payload_json"])
        scope = {
            "window": payload["window"],
            "freeze": {"demands": payload["demands"], "building_ids": payload["building_ids"]},
            "rollback_plan": payload["rollback_plan"],
        }
        scope_sha256 = digest(scope)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO construction_approvals(change_id,revision,decision,scope_json,scope_sha256,"
                "reason,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    change_id,
                    expected_revision,
                    decision,
                    canonical_json(scope),
                    scope_sha256,
                    reason_text,
                    actor_id,
                    self._now(),
                ),
            )
            cursor = self.connection.execute(
                "UPDATE construction_changes SET state=?,updated_at=? "
                "WHERE change_id=? AND revision=? AND state='pending_review'",
                (decision, self._now(), change_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更不是当前待审版本")
            self._audit(
                "construction_change",
                change_id,
                "change.decided",
                actor_id,
                {"revision": expected_revision, "decision": decision, "scope_sha256": scope_sha256},
            )
        return {
            "change_id": change_id,
            "state": decision,
            "revision": expected_revision,
            "scope_sha256": scope_sha256,
        }

    def record_receipt(
        self,
        actor_id: str,
        change_id: str,
        step_index: object,
        result: str,
        note: str,
        step_kind: str = "execution",
    ) -> dict[str, Any]:
        self._require(actor_id, "construction.receipt")
        if step_kind not in {"execution", "rollback"}:
            raise ValidationFailed("step_kind 必须是 execution 或 rollback")
        if result not in {"done", "failed"}:
            raise ValidationFailed("result 必须是 done 或 failed")
        note_text = required_text(note, "note")
        row = self.connection.execute(
            "SELECT * FROM construction_changes WHERE change_id=?", (change_id,)
        ).fetchone()
        if row is None:
            raise NotFound("施工变更不存在")
        payload = json.loads(row["payload_json"])
        if step_kind == "execution":
            if row["state"] not in ("approved", "in_progress"):
                raise InvalidState("当前状态不能记录施工回执")
            steps = payload["steps"]
            attempt = 0
        else:
            if row["state"] != "rolling_back":
                raise InvalidState("只有回退中的变更可以记录回退回执")
            steps = payload["rollback_plan"]["steps"]
            attempt = int(row["rollback_round"])
        if (
            isinstance(step_index, bool)
            or not isinstance(step_index, int)
            or not 0 <= step_index < len(steps)
        ):
            raise ValidationFailed("step_index 超出步骤范围")
        revision = int(row["revision"])
        done = self.connection.execute(
            "SELECT COUNT(*) AS c FROM construction_receipts WHERE change_id=? AND revision=? "
            "AND step_kind=? AND attempt=? AND result='done'",
            (change_id, revision, step_kind, attempt),
        ).fetchone()["c"]
        if step_index != done:
            raise InvalidState(f"现场回执必须逐步推进，下一步应为第 {done} 步")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO construction_receipts(change_id,revision,attempt,step_kind,step_index,step_text,"
                "result,note,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    change_id,
                    revision,
                    attempt,
                    step_kind,
                    step_index,
                    steps[step_index],
                    result,
                    note_text,
                    actor_id,
                    self._now(),
                ),
            )
            if result == "failed":
                new_state = "failed"
                cursor = self.connection.execute(
                    "UPDATE construction_changes SET state='failed',failure_reason=?,updated_at=? "
                    "WHERE change_id=? AND revision=?",
                    (note_text, self._now(), change_id, revision),
                )
            elif step_index == len(steps) - 1:
                new_state = "completed" if step_kind == "execution" else "rolled_back"
                cursor = self.connection.execute(
                    "UPDATE construction_changes SET state=?,updated_at=? WHERE change_id=? AND revision=?",
                    (new_state, self._now(), change_id, revision),
                )
            else:
                new_state = "in_progress" if step_kind == "execution" else "rolling_back"
                cursor = self.connection.execute(
                    "UPDATE construction_changes SET state=?,updated_at=? WHERE change_id=? AND revision=?",
                    (new_state, self._now(), change_id, revision),
                )
            if cursor.rowcount != 1:
                raise InvalidState("变更状态已变化，请刷新后重试")
            self._audit(
                "construction_change",
                change_id,
                "receipt.recorded",
                actor_id,
                {
                    "revision": revision,
                    "attempt": attempt,
                    "step_kind": step_kind,
                    "step_index": step_index,
                    "result": result,
                },
            )
        return {
            "change_id": change_id,
            "state": new_state,
            "revision": revision,
            "step_kind": step_kind,
            "step_index": step_index,
            "result": result,
        }

    def begin_rollback(self, actor_id: str, change_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "construction.receipt")
        note_text = required_text(note, "note")
        row = self.connection.execute(
            "SELECT * FROM construction_changes WHERE change_id=?", (change_id,)
        ).fetchone()
        if row is None:
            raise NotFound("施工变更不存在")
        if row["state"] != "failed":
            raise InvalidState("只有失败的变更可以转入回退")
        rollback_round = int(row["rollback_round"]) + 1
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE construction_changes SET state='rolling_back',rollback_round=?,updated_at=? "
                "WHERE change_id=? AND state='failed'",
                (rollback_round, self._now(), change_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有失败的变更可以转入回退")
            self._audit(
                "construction_change",
                change_id,
                "change.rollback_started",
                actor_id,
                {"round": rollback_round, "note": note_text},
            )
        return {"change_id": change_id, "state": "rolling_back", "rollback_round": rollback_round}

    def takeover_change(self, actor_id: str, change_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "construction.receipt")
        note_text = required_text(note, "note")
        row = self.connection.execute(
            "SELECT * FROM construction_changes WHERE change_id=?", (change_id,)
        ).fetchone()
        if row is None:
            raise NotFound("施工变更不存在")
        if row["state"] not in ("failed", "rolling_back"):
            raise InvalidState("只有失败或回退中的变更可以人工接管")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE construction_changes SET state='manual_takeover',updated_at=? "
                "WHERE change_id=? AND state IN ('failed','rolling_back')",
                (self._now(), change_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有失败或回退中的变更可以人工接管")
            self._audit(
                "construction_change",
                change_id,
                "change.manual_takeover",
                actor_id,
                {"note": note_text},
            )
        return {"change_id": change_id, "state": "manual_takeover"}

    def change(self, change_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM construction_changes WHERE change_id=?", (change_id,)
        ).fetchone()
        if row is None:
            raise NotFound("施工变更不存在")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def change_assessment(self, change_id: str) -> dict[str, Any]:
        self.change(change_id)
        row = self.connection.execute(
            "SELECT * FROM construction_assessments WHERE change_id=? ORDER BY revision DESC LIMIT 1",
            (change_id,),
        ).fetchone()
        if row is None:
            raise NotFound("影响评估不存在")
        return {
            "change_id": change_id,
            "revision": int(row["revision"]),
            "snapshot_sha256": row["snapshot_sha256"],
            "snapshot": json.loads(row["snapshot_json"]),
            "result": json.loads(row["result_json"]),
            "created_at": row["created_at"],
        }

    def change_revisions(self, change_id: str) -> dict[str, Any]:
        self.change(change_id)
        rows = self.connection.execute(
            "SELECT * FROM construction_change_revisions WHERE change_id=? ORDER BY revision",
            (change_id,),
        ).fetchall()
        return {
            "change_id": change_id,
            "revisions": [
                {
                    "revision": int(row["revision"]),
                    "diff": json.loads(row["diff_json"]),
                    "payload": json.loads(row["payload_json"]),
                    "supersedes_revision_id": row["supersedes_revision_id"],
                    "actor_id": row["actor_id"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    def explain_change(self, change_id: str) -> dict[str, Any]:
        change = self.change(change_id)
        assessment = self.change_assessment(change_id)
        approvals = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM construction_approvals WHERE change_id=? ORDER BY approval_id",
                (change_id,),
            ).fetchall()
        ]
        for approval in approvals:
            approval["scope"] = json.loads(approval.pop("scope_json"))
        receipts = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM construction_receipts WHERE change_id=? ORDER BY receipt_id",
                (change_id,),
            ).fetchall()
        ]
        revisions = self.change_revisions(change_id)["revisions"]
        basis: list[str] = []
        result = assessment["result"]
        for constraint in result["constraints"]:
            if constraint["required"] == "0.000" and constraint["committed"] == "0.000":
                continue
            line = (
                f"{constraint['label']}：容量 {constraint['capacity']}，"
                f"既有申请占用 {constraint['committed']}，本次需求 {constraint['required']}，"
                f"剩余 {constraint['remaining']}"
            )
            if constraint["displaced"]:
                targets = "、".join(
                    f"{item['change_id']}({item['displaced']})" for item in constraint["displaced"]
                )
                line += f"，挤占既有申请 {targets}"
            if not constraint["feasible_after_displacement"]:
                line += "，即使挤占全部既有申请仍不可行"
            basis.append(line)
        for approval in approvals:
            decision_label = "批准" if approval["decision"] == "approved" else "驳回"
            basis.append(
                f"{approval['decided_by']} 于 {approval['decided_at']} 将第 {approval['revision']} 版的"
                f"施工窗口、资源冻结与回退方案作为整体{decision_label}，"
                f"范围哈希 {approval['scope_sha256']}，理由：{approval['reason']}"
            )
        for receipt in receipts:
            kind_label = "施工" if receipt["step_kind"] == "execution" else f"回退第{receipt['attempt']}轮"
            result_label = "完成" if receipt["result"] == "done" else "失败"
            basis.append(
                f"第 {receipt['revision']} 版{kind_label}第 {receipt['step_index']} 步"
                f"（{receipt['step_text']}）{result_label}：{receipt['note']}，"
                f"记录人 {receipt['recorded_by']}"
            )
        if change["failure_reason"]:
            basis.append(f"失败原因：{change['failure_reason']}")
        basis.append(f"当前状态：{change['state']}，当前版本：{change['revision']}")
        return {
            "change_id": change_id,
            "state": change["state"],
            "revision": change["revision"],
            "snapshot_sha256": assessment["snapshot_sha256"],
            "snapshot_taken_at": assessment["snapshot"]["taken_at"],
            "assessment": result,
            "approvals": approvals,
            "receipts": receipts,
            "revisions": revisions,
            "decision_basis": basis,
        }
