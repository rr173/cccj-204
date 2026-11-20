"""Section allocation of payment vouchers, exactly and idempotently.

Rules implemented here (from the monthly close specification):

* a payment voucher references one allocation plan approved for that
  month; each plan line gives ``(project_id, section_id)``, a positive
  integer weight, and an announcement ``result_unit`` assertion that
  was already verified by :mod:`bidfact.fund.evidence`;
* within one plan a section key may appear only once;
* the **whole voucher** amount (never one bank debit) is split by the
  plan's weights: integer quotient first, leftover minor units handed
  out by descending remainder, ties by ``(project_id, section_id)``
  ascending, case-sensitive Unicode code point order;
* some rows may legitimately receive zero; the split must sum exactly
  to the voucher amount (asserted, never silently rounded).

A voucher can be cleared by several bank flows.  Allocation is a
property of the voucher, so it runs exactly once per voucher and each
bank flow simply *selects* the voucher's allocated lines in proportion
to how much of the voucher it cleared.  That is what makes the wiring
example come out right: two 1分 debits clearing one 2分 equal-weight
voucher give A 1 / B 1, not A 2.
"""
from __future__ import annotations

from dataclasses import dataclass

from .money import split_by_weights


@dataclass(frozen=True)
class PlanRow:
    project_id: str
    section_id: str
    weight: int
    result_id: str
    assertion_id: str
    source_file: str
    source_line: int
    plan_id: str = ""

    @property
    def result_unit_assertion_id(self) -> str:
        return self.assertion_id

    @property
    def key(self) -> tuple[str, str]:
        return (self.project_id, self.section_id)


@dataclass(frozen=True)
class AllocatedLine:
    project_id: str
    section_id: str
    weight: int
    amount_minor: int          # share of the whole voucher
    remainder: int             # total*weight % sum(weights) at split time
    sum_weights: int
    result_id: str
    assertion_id: str
    source_file: str
    source_line: int
    plan_id: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.project_id, self.section_id)


def split_voucher(amount_minor: int,
                  plan_rows: list[PlanRow]) -> list[AllocatedLine]:
    """Split one voucher amount across plan rows exactly once."""
    keys = [row.key for row in plan_rows]
    weights = [row.weight for row in plan_rows]
    amounts, remainders = split_by_weights(amount_minor, weights, keys)
    if sum(amounts) != amount_minor:
        raise AssertionError("integer allocation did not sum to the voucher")
    total_weight = sum(weights)
    return [AllocatedLine(
        project_id=row.project_id, section_id=row.section_id,
        weight=row.weight, amount_minor=amount, remainder=remainder,
        sum_weights=total_weight, result_id=row.result_id,
        assertion_id=row.assertion_id, source_file=row.source_file,
        source_line=row.source_line, plan_id=row.plan_id)
        for row, amount, remainder in zip(plan_rows, amounts, remainders)]


def select_per_clearing(lines: list[tuple[int, int, str, str]],
                        clearings: list[tuple[int, str, str]],
                        voucher_minor: int
                        ) -> list[tuple[int, str, str, int]]:
    """Attribute each allocated line to the bank flows that paid it.

    ``lines`` are ``(rank, amount, project_id, section_id)``; ``clearings``
    are ``(amount, account_id, bank_flow_id)``, in ascending
    ``(account_id, bank_flow_id)`` order.  Both margins are exact: every
    flow gives out its whole clearing amount and every line receives its
    whole allocation (both ends are already reconciled to equality).

    Method: walk the flows in ascending key order; each flow's amount is
    handed to still-unfilled lines by largest remainder (weights = the
    lines' full allocations), never exceeding a line's remaining
    capacity; if a line fills up, its quotient is redistributed to the
    remaining lines.  This is a deterministic, capacity-aware largest
    remainder and needs no floating point: all comparisons are integer
    products.  Ties are broken by ``(project_id, section_id)`` ascending.

    Because the caller only ever passes a voucher that its clearings pay
    in full, the totals match exactly and no unit is invented or lost.
    """
    if voucher_minor <= 0:
        raise ValueError("voucher amount must be positive")
    if sum(c[0] for c in clearings) != voucher_minor:
        raise ValueError("clearings must pay the voucher in full")
    remaining = {rank: amount for rank, amount, _, _ in lines}
    keys = {rank: (proj, sec) for rank, _, proj, sec in lines}
    result: list[tuple[int, str, str, int]] = []
    for cleared, account_id, bank_flow_id in clearings:
        to_place = cleared
        while to_place > 0:
            open_ranks = [rank for rank, _, _, _ in lines
                          if remaining[rank] > 0]
            if not open_ranks:
                raise AssertionError("no capacity left for a clearing")
            weight_total = sum(remaining[r] for r in open_ranks)
            bases: dict[int, int] = {}
            remainders: dict[int, int] = {}
            for rank in open_ranks:
                product = to_place * remaining[rank]
                bases[rank] = product // weight_total
                remainders[rank] = product % weight_total
            assigned = sum(bases.values())
            # Hand out the leftover one unit at a time, best remainder
            # first, never exceeding a line's remaining capacity.
            order = sorted(
                open_ranks,
                key=lambda r: (-remainders[r], keys[r], r))
            for rank in order:
                if assigned >= to_place:
                    break
                if bases[rank] < remaining[rank]:
                    bases[rank] += 1
                    assigned += 1
            # Capacity-aware: if a line is capped below its quotient the
            # leftover is recomputed on the next round with that line out.
            progress = 0
            for rank in open_ranks:
                take = min(bases[rank], remaining[rank])
                if take:
                    result.append((rank, account_id, bank_flow_id, take))
                    remaining[rank] -= take
                    progress += take
            if progress == 0:
                raise AssertionError("clearing attribution made no progress")
            to_place -= progress
    for rank, amount, _, _ in lines:
        if remaining[rank] != 0:
            raise AssertionError("line not fully attributed to flows")
    # Stable presentation order: flow key, then line rank.
    result.sort(key=lambda item: (item[1], item[2], item[0]))
    return result
