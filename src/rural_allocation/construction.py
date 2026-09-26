"""安置片区施工变更的约束评估、影响分析与修订差异链计算。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import parse_utc
from .errors import ValidationFailed
from .models import decimal_value, identifier, positive_integer, required_text
from .planning import decimal_text, quantize_volume


ZERO = Decimal("0")

CONSTRAINT_KINDS = ("housing", "water", "seats", "road", "fire")
CONSTRAINT_LABELS = {
    "housing": "住房套数",
    "water": "给排水",
    "seats": "学位",
    "road": "道路承载",
    "fire": "消防覆盖",
}
CHANGE_STATES = (
    "pending_review",
    "approved",
    "rejected",
    "in_progress",
    "failed",
    "rolling_back",
    "rolled_back",
    "manual_takeover",
    "completed",
)
REVISABLE_STATES = ("pending_review", "rejected", "failed", "rolled_back", "manual_takeover")


def constraint_demands(value: object, field: str = "demands") -> dict[str, Decimal]:
    if not isinstance(value, Mapping) or not value:
        raise ValidationFailed(f"{field} 必须是非空对象")
    result: dict[str, Decimal] = {}
    for key, amount in value.items():
        kind = required_text(key, f"{field} 键", 16)
        if kind not in CONSTRAINT_KINDS:
            raise ValidationFailed(f"{field} 包含未知约束类型 {kind}")
        parsed = quantize_volume(decimal_value(amount, f"{field}.{kind}", minimum=ZERO))
        if parsed > ZERO:
            result[kind] = parsed
    if not result:
        raise ValidationFailed(f"{field} 至少需要一项大于零的约束需求")
    return result


def _text_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValidationFailed(f"{field} 必须是非空数组")
    return tuple(required_text(item, f"{field} 元素") for item in value)


@dataclass(frozen=True, slots=True)
class Building:
    building_id: str
    settlement_id: str
    name: str
    housing_units: int
    floors: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Building":
        return cls(
            building_id=identifier(raw.get("building_id"), "building_id"),
            settlement_id=identifier(raw.get("settlement_id"), "settlement_id"),
            name=required_text(raw.get("name"), "name"),
            housing_units=positive_integer(raw.get("housing_units"), "housing_units"),
            floors=positive_integer(raw.get("floors"), "floors"),
        )


@dataclass(frozen=True, slots=True)
class PublicService:
    service_id: str
    settlement_id: str
    kind: str
    capacity: Decimal
    unit: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PublicService":
        kind = required_text(raw.get("kind"), "kind", 16)
        if kind not in CONSTRAINT_KINDS:
            raise ValidationFailed("kind 必须是 housing、water、seats、road 或 fire")
        return cls(
            service_id=identifier(raw.get("service_id"), "service_id"),
            settlement_id=identifier(raw.get("settlement_id"), "settlement_id"),
            kind=kind,
            capacity=decimal_value(raw.get("capacity"), "capacity", minimum=ZERO),
            unit=required_text(raw.get("unit"), "unit", 16),
        )


@dataclass(frozen=True, slots=True)
class ChangePayload:
    """施工窗口、资源冻结与回退方案作为整体的变更内容。"""

    title: str
    window_starts_at: str
    window_ends_at: str
    demands: Mapping[str, Decimal]
    building_ids: tuple[str, ...]
    rollback_summary: str
    rollback_steps: tuple[str, ...]
    steps: tuple[str, ...]
    priority: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ChangePayload":
        window = raw.get("window")
        if not isinstance(window, Mapping):
            raise ValidationFailed("window 必须是包含 starts_at 与 ends_at 的对象")
        starts_at = required_text(window.get("starts_at"), "window.starts_at", 40)
        ends_at = required_text(window.get("ends_at"), "window.ends_at", 40)
        try:
            start = parse_utc(starts_at, "window.starts_at")
            end = parse_utc(ends_at, "window.ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("window.ends_at 必须晚于 window.starts_at")
        building_ids_raw = raw.get("building_ids")
        if not isinstance(building_ids_raw, list) or not building_ids_raw:
            raise ValidationFailed("building_ids 必须是非空数组")
        building_ids = tuple(
            dict.fromkeys(identifier(item, "building_ids 元素") for item in building_ids_raw)
        )
        rollback = raw.get("rollback_plan")
        if not isinstance(rollback, Mapping):
            raise ValidationFailed("rollback_plan 必须是包含 summary 与 steps 的对象")
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            title=required_text(raw.get("title"), "title"),
            window_starts_at=starts_at,
            window_ends_at=ends_at,
            demands=constraint_demands(raw.get("demands")),
            building_ids=building_ids,
            rollback_summary=required_text(rollback.get("summary"), "rollback_plan.summary"),
            rollback_steps=_text_list(rollback.get("steps"), "rollback_plan.steps"),
            steps=_text_list(raw.get("steps"), "steps"),
            priority=priority,
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "window": {"starts_at": self.window_starts_at, "ends_at": self.window_ends_at},
            "demands": {kind: decimal_text(self.demands[kind]) for kind in sorted(self.demands)},
            "building_ids": list(self.building_ids),
            "rollback_plan": {
                "summary": self.rollback_summary,
                "steps": list(self.rollback_steps),
            },
            "steps": list(self.steps),
            "priority": self.priority,
        }


@dataclass(frozen=True, slots=True)
class ResourceApplication:
    """已批准且窗口重叠的既有申请，对约束容量形成冻结占用。"""

    change_id: str
    priority: int
    submitted_at: str
    demands: Mapping[str, Decimal]


def assess_constraints(
    *,
    capacities: Mapping[str, Decimal],
    applications: Iterable[ResourceApplication],
    demands: Mapping[str, Decimal],
) -> dict[str, Any]:
    """按当前设施快照计算每项约束的剩余量与被挤占的既有申请。

    既有申请按 (priority, submitted_at, change_id) 排序占用容量；本次变更
    不足时按优先级逆序挤占既有申请，挤占后仍不足的约束标记为不可行。
    """

    apps = list(applications)
    constraint_rows: list[dict[str, Any]] = []
    displaced_by_change: dict[str, dict[str, Decimal]] = {}
    blocking: list[str] = []
    for kind in CONSTRAINT_KINDS:
        capacity = capacities.get(kind, ZERO)
        required = demands.get(kind, ZERO)
        consumers = sorted(
            (app for app in apps if app.demands.get(kind, ZERO) > ZERO),
            key=lambda app: (app.priority, app.submitted_at, app.change_id),
        )
        committed = sum((app.demands[kind] for app in consumers), ZERO)
        remaining = quantize_volume(capacity - committed - required)
        displaced: list[dict[str, str]] = []
        if remaining < ZERO:
            shortage = -remaining
            for app in reversed(consumers):
                if shortage <= ZERO:
                    break
                take = min(app.demands[kind], shortage)
                displaced.append({
                    "change_id": app.change_id,
                    "displaced": decimal_text(quantize_volume(take)),
                })
                bucket = displaced_by_change.setdefault(app.change_id, {})
                bucket[kind] = bucket.get(kind, ZERO) + take
                shortage -= take
        released = sum((Decimal(item["displaced"]) for item in displaced), ZERO)
        after = quantize_volume(capacity - (committed - released) - required)
        feasible = after >= ZERO
        if not feasible:
            blocking.append(kind)
        constraint_rows.append({
            "kind": kind,
            "label": CONSTRAINT_LABELS[kind],
            "capacity": decimal_text(quantize_volume(capacity)),
            "committed": decimal_text(quantize_volume(committed)),
            "required": decimal_text(quantize_volume(required)),
            "remaining": decimal_text(remaining),
            "fits": remaining >= ZERO,
            "displaced": displaced,
            "remaining_after_displacement": decimal_text(after),
            "feasible_after_displacement": feasible,
        })
    conflicts = [
        {
            "change_id": change_id,
            "kinds": {
                kind: decimal_text(quantize_volume(amount))
                for kind, amount in sorted(kinds.items())
            },
            "reason": "既有申请在"
            + "、".join(CONSTRAINT_LABELS[kind] for kind in sorted(kinds))
            + "上被本次变更挤占",
        }
        for change_id, kinds in sorted(displaced_by_change.items())
    ]
    return {
        "constraints": constraint_rows,
        "conflicting_applications": conflicts,
        "feasible": not blocking,
        "blocking_kinds": blocking,
    }


def _diff_node(old: Any, new: Any) -> Any:
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(set(old) | set(new)):
            if key not in old:
                result[key] = {"op": "added", "to": new[key]}
            elif key not in new:
                result[key] = {"op": "removed", "from": old[key]}
            else:
                child = _diff_node(old[key], new[key])
                if child is not None:
                    result[key] = child
        return result or None
    if old != new:
        return {"op": "changed", "from": old, "to": new}
    return None


def payload_diff(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """两版变更内容的字段级差异，用于修订差异链。"""

    node = _diff_node(old, new)
    return {} if node is None else node
