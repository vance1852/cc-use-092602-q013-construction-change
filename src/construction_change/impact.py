"""施工变更影响与修订差异的确定性计算。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .models import CONSTRAINTS


ZERO = Decimal("0")


def quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def diff_payloads(old: Mapping[str, Any], new: Mapping[str, Any], path: str = "") -> dict[str, Any]:
    """计算两个规范化载荷的字段级差异。

    返回 {点分路径: {"from": 旧值, "to": 新值}}；新增字段 from 为 None，
    删除字段 to 为 None，列表作为整体比较。结果按键排序，保证确定性。
    """
    changes: dict[str, Any] = {}
    for key in sorted(set(old) | set(new)):
        location = f"{path}.{key}" if path else str(key)
        in_old = key in old
        in_new = key in new
        if not in_old:
            changes[location] = {"from": None, "to": new[key]}
        elif not in_new:
            changes[location] = {"from": old[key], "to": None}
        else:
            before, after = old[key], new[key]
            if isinstance(before, Mapping) and isinstance(after, Mapping):
                changes.update(diff_payloads(before, after, location))
            elif before != after:
                changes[location] = {"from": before, "to": after}
    return changes


@dataclass(frozen=True, slots=True)
class CommittedDemand:
    """一份既有申请对五项约束的占用。"""

    application_id: str
    priority: int
    created_at: str
    amounts: Mapping[str, Decimal]

    def amount(self, constraint: str) -> Decimal:
        return self.amounts.get(constraint, ZERO)


def exclusion_order(demands: Sequence[CommittedDemand]) -> list[CommittedDemand]:
    """排斥顺序：优先级数值大、提交时间晚、编号靠后的申请先被排除。"""
    return sorted(demands, key=lambda item: (item.priority, item.created_at, item.application_id), reverse=True)


def compute_impact(
    *,
    capacities: Mapping[str, Decimal],
    freeze: Mapping[str, Decimal],
    deltas: Mapping[str, Decimal],
    committed: Sequence[CommittedDemand],
) -> dict[str, Any]:
    """依据设施快照计算每项约束的剩余量与被排斥的冲突申请。

    对每项约束给出：容量、已占用、冻结量、竣工变更量、变更前剩余、
    施工窗口内剩余、竣工后剩余与缺口；再按排斥顺序贪心选择冲突申请，
    直到所有约束缺口被覆盖。输入必须满足 freeze <= capacity 且
    capacity + delta >= 0，因此排除全部申请后缺口必然收敛。
    """
    committed_totals = {constraint: ZERO for constraint in CONSTRAINTS}
    for demand in committed:
        for constraint in CONSTRAINTS:
            committed_totals[constraint] += demand.amount(constraint)

    constraints: dict[str, dict[str, str]] = {}
    shortfalls: dict[str, Decimal] = {}
    for constraint in CONSTRAINTS:
        capacity = capacities[constraint]
        frozen = freeze.get(constraint, ZERO)
        delta = deltas.get(constraint, ZERO)
        used = committed_totals[constraint]
        remaining_before = capacity - used
        remaining_during = capacity - frozen - used
        remaining_after = capacity + delta - used
        worst_available = min(capacity - frozen, capacity + delta)
        shortfall = max(ZERO, used - worst_available)
        shortfalls[constraint] = shortfall
        constraints[constraint] = {
            "capacity": decimal_text(quantize_amount(capacity)),
            "committed": decimal_text(quantize_amount(used)),
            "frozen": decimal_text(quantize_amount(frozen)),
            "delta": decimal_text(quantize_amount(delta)),
            "remaining_before": decimal_text(quantize_amount(remaining_before)),
            "remaining_during": decimal_text(quantize_amount(remaining_during)),
            "remaining_after": decimal_text(quantize_amount(remaining_after)),
            "shortfall": decimal_text(quantize_amount(shortfall)),
        }

    remaining_shortfall = dict(shortfalls)
    excluded_totals = {constraint: ZERO for constraint in CONSTRAINTS}
    conflicts: list[dict[str, Any]] = []
    if any(amount > ZERO for amount in remaining_shortfall.values()):
        for demand in exclusion_order(committed):
            if not any(amount > ZERO for amount in remaining_shortfall.values()):
                break
            blocking = [
                constraint
                for constraint in CONSTRAINTS
                if remaining_shortfall[constraint] > ZERO and demand.amount(constraint) > ZERO
            ]
            if not blocking:
                continue
            for constraint in CONSTRAINTS:
                freed = demand.amount(constraint)
                excluded_totals[constraint] += freed
                remaining_shortfall[constraint] = max(ZERO, remaining_shortfall[constraint] - freed)
            conflicts.append({
                "application_id": demand.application_id,
                "priority": demand.priority,
                "created_at": demand.created_at,
                "amounts": {
                    constraint: decimal_text(quantize_amount(demand.amount(constraint)))
                    for constraint in CONSTRAINTS
                    if demand.amount(constraint) > ZERO
                },
                "constraints": blocking,
                "reason": "约束 " + ",".join(blocking) + " 容量不足，按优先级逆序排除",
            })

    for constraint in CONSTRAINTS:
        entry = constraints[constraint]
        excluded = excluded_totals[constraint]
        capacity = capacities[constraint]
        frozen = freeze.get(constraint, ZERO)
        delta = deltas.get(constraint, ZERO)
        resolved_used = committed_totals[constraint] - excluded
        entry["excluded"] = decimal_text(quantize_amount(excluded))
        entry["remaining_during_resolved"] = decimal_text(quantize_amount(capacity - frozen - resolved_used))
        entry["remaining_after_resolved"] = decimal_text(quantize_amount(capacity + delta - resolved_used))

    return {
        "constraints": constraints,
        "conflicts": conflicts,
        "excluded_application_ids": [item["application_id"] for item in conflicts],
        "committed_count": len(committed),
        "feasible": not any(amount > ZERO for amount in remaining_shortfall.values()),
        "rule": "剩余量=容量-已占用-冻结(窗口内)或容量+变更量-已占用(竣工后)；冲突按优先级数值大、提交晚、编号靠后的顺序排除",
    }
