"""安置片区施工变更影响审批的领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from .clock import parse_utc, utc_text
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 五项公共服务约束：住房套数、给排水、学位、道路承载、消防覆盖。
CONSTRAINTS = ("housing_units", "water_drainage", "school_seats", "road_capacity", "fire_coverage")

BUILDING_ACTIONS = {"inspect", "renovate", "extend", "demolish"}
RECEIPT_OUTCOMES = {"done", "failed"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def priority_value(value: object, field: str = "priority") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 999:
        raise ValidationFailed(f"{field} 必须是 1 到 999 的整数")
    return value


def constraint_amounts(
    value: object,
    field: str,
    *,
    require_all: bool,
    allow_negative: bool = False,
) -> dict[str, Decimal]:
    """解析五项约束用量，缺省按 0 处理；require_all 时五项必须全部给出。"""
    if not isinstance(value, Mapping):
        raise ValidationFailed(f"{field} 必须是对象")
    unknown = sorted(set(value) - set(CONSTRAINTS))
    if unknown:
        raise ValidationFailed(f"{field} 包含未知约束: {','.join(unknown)}")
    if require_all and set(value) != set(CONSTRAINTS):
        raise ValidationFailed(f"{field} 必须包含全部五项约束: {','.join(CONSTRAINTS)}")
    result: dict[str, Decimal] = {}
    for key, item in value.items():
        minimum = None if allow_negative else Decimal("0")
        result[key] = decimal_value(item, f"{field}.{key}", minimum=minimum)
    return result


def amount_text(value: Decimal) -> str:
    """约束用量统一保留三位小数，保证差异链按数值比较。"""
    return format(value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP), "f")


@dataclass(frozen=True, slots=True)
class ZoneInput:
    zone_id: str
    name: str
    timezone: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ZoneInput":
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            zone_id=identifier(raw.get("zone_id"), "zone_id"),
            name=required_text(raw.get("name"), "name"),
            timezone=timezone,
        )


@dataclass(frozen=True, slots=True)
class ServiceVersionInput:
    capacities: dict[str, Decimal]
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ServiceVersionInput":
        return cls(
            capacities=constraint_amounts(raw.get("capacities"), "capacities", require_all=True),
            note=required_text(raw.get("note", "公共服务版本"), "note", 512),
        )


@dataclass(frozen=True, slots=True)
class BuildingInput:
    building_id: str
    name: str
    households: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BuildingInput":
        households = raw.get("households")
        if isinstance(households, bool) or not isinstance(households, int) or households < 0:
            raise ValidationFailed("households 必须是非负整数")
        return cls(
            building_id=identifier(raw.get("building_id"), "building_id"),
            name=required_text(raw.get("name"), "name"),
            households=households,
        )


@dataclass(frozen=True, slots=True)
class ApplicationInput:
    application_id: str
    applicant: str
    demands: dict[str, Decimal]
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApplicationInput":
        demands = constraint_amounts(raw.get("demands"), "demands", require_all=True)
        if not any(amount > 0 for amount in demands.values()):
            raise ValidationFailed("demands 至少一项约束用量大于零")
        return cls(
            application_id=identifier(raw.get("application_id"), "application_id"),
            applicant=required_text(raw.get("applicant"), "applicant"),
            demands=demands,
            priority=priority_value(raw.get("priority", 100)),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class StepInput:
    step_key: str
    title: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], field: str) -> "StepInput":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"{field} 必须是对象")
        return cls(
            step_key=identifier(raw.get("step_key"), f"{field}.step_key"),
            title=required_text(raw.get("title"), f"{field}.title"),
        )


def step_list(value: object, field: str) -> list[StepInput]:
    if not isinstance(value, list) or not value:
        raise ValidationFailed(f"{field} 必须是非空数组")
    steps = [StepInput.from_dict(item, f"{field}[{index}]") for index, item in enumerate(value)]
    keys = [step.step_key for step in steps]
    if len(set(keys)) != len(keys):
        raise ValidationFailed(f"{field} 的步骤编号不能重复")
    return steps


@dataclass(frozen=True, slots=True)
class ChangeInput:
    change_id: str
    title: str
    reason: str
    window_start: str
    window_end: str
    buildings: list[dict[str, str]]
    freeze_amounts: dict[str, Decimal]
    capacity_deltas: dict[str, Decimal]
    execution_steps: list[StepInput]
    rollback_summary: str
    rollback_steps: list[StepInput]
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, require_idempotency_key: bool = True) -> "ChangeInput":
        window = raw.get("window")
        if not isinstance(window, Mapping):
            raise ValidationFailed("window 必须是包含 starts_at 和 ends_at 的对象")
        starts_at = required_text(window.get("starts_at"), "window.starts_at", 40)
        ends_at = required_text(window.get("ends_at"), "window.ends_at", 40)
        try:
            start = parse_utc(starts_at, "window.starts_at")
            end = parse_utc(ends_at, "window.ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("window.ends_at 必须晚于 window.starts_at")
        # 规范化为 UTC 文本，保证窗口比较与字典序一致。
        window_start = utc_text(start)
        window_end = utc_text(end)
        buildings_raw = raw.get("buildings")
        if not isinstance(buildings_raw, list) or not buildings_raw:
            raise ValidationFailed("buildings 楼栋清单必须是非空数组")
        buildings: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, item in enumerate(buildings_raw):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"buildings[{index}] 必须是对象")
            building_id = identifier(item.get("building_id"), f"buildings[{index}].building_id")
            if building_id in seen:
                raise ValidationFailed("buildings 楼栋清单不能重复")
            seen.add(building_id)
            action = required_text(item.get("action", "renovate"), f"buildings[{index}].action", 16)
            if action not in BUILDING_ACTIONS:
                raise ValidationFailed(f"buildings[{index}].action 不是受支持的处置方式")
            buildings.append({"building_id": building_id, "action": action})
        rollback = raw.get("rollback_plan")
        if not isinstance(rollback, Mapping):
            raise ValidationFailed("rollback_plan 必须是包含 summary 和 steps 的对象")
        key_value = raw.get("idempotency_key")
        if require_idempotency_key:
            idempotency_key = identifier(key_value, "idempotency_key")
        else:
            idempotency_key = "" if key_value is None else identifier(key_value, "idempotency_key")
        return cls(
            change_id=identifier(raw.get("change_id"), "change_id"),
            title=required_text(raw.get("title"), "title"),
            reason=required_text(raw.get("reason"), "reason", 512),
            window_start=window_start,
            window_end=window_end,
            buildings=buildings,
            freeze_amounts=constraint_amounts(raw.get("freeze_amounts", {}), "freeze_amounts", require_all=False),
            capacity_deltas=constraint_amounts(
                raw.get("capacity_deltas", {}), "capacity_deltas", require_all=False, allow_negative=True
            ),
            execution_steps=step_list(raw.get("execution_steps"), "execution_steps"),
            rollback_summary=required_text(rollback.get("summary"), "rollback_plan.summary", 512),
            rollback_steps=step_list(rollback.get("steps"), "rollback_plan.steps"),
            idempotency_key=idempotency_key,
        )

    def payload(self) -> dict[str, Any]:
        """纳入审批捆绑与差异链的规范化载荷。"""
        return {
            "title": self.title,
            "reason": self.reason,
            "window": {"starts_at": self.window_start, "ends_at": self.window_end},
            "buildings": self.buildings,
            "freeze_amounts": {key: amount_text(value) for key, value in sorted(self.freeze_amounts.items())},
            "capacity_deltas": {key: amount_text(value) for key, value in sorted(self.capacity_deltas.items())},
            "execution_steps": [{"step_key": step.step_key, "title": step.title} for step in self.execution_steps],
            "rollback_plan": {
                "summary": self.rollback_summary,
                "steps": [{"step_key": step.step_key, "title": step.title} for step in self.rollback_steps],
            },
        }


@dataclass(frozen=True, slots=True)
class ReceiptInput:
    step_key: str
    outcome: str
    note: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReceiptInput":
        outcome = required_text(raw.get("outcome"), "outcome", 16)
        if outcome not in RECEIPT_OUTCOMES:
            raise ValidationFailed("outcome 必须是 done 或 failed")
        return cls(
            step_key=identifier(raw.get("step_key"), "step_key"),
            outcome=outcome,
            note=required_text(raw.get("note"), "note", 512),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
