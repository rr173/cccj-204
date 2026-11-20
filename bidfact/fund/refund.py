"""Refund vouchers: return money to the original sections, not this month.

A refund voucher names an earlier **closed** payment voucher of the
same account set and the same currency.  Finance also states the
returned share as an exact ratio (``rate``; the refund amount must equal
``floor(original_amount × rate)``).  Rules:

* the original must be a ``payment`` voucher in a *previously closed*
  month; same-month payments, refunds of refunds, foreign currencies and
  unknown numbers are blocking issues;
* if the refund voucher states a ``rate``, the amount must equal
  ``floor(original_amount × rate)`` exactly and the ratio must leave no
  fractional minor unit; a residue blocks the month rather than being
  folded into a monthly total.  Without a rate the stated amount is used
  as-is (the waterfall below needs no rounding);
* the refund reuses the original payment's allocation lines — this
  month's allocation plan is never consulted;
* original lines are consumed in a fixed order: the lexicographically
  **larger** ``(project_id, section_id)`` key first, then the smaller
  one; each line only up to its historical unconsumed amount, so a
  section already fully refunded in a past month is skipped;
* several refunds in one month against the same original are applied in
  ascending refund-voucher-number order (Unicode code point), never in
  file or clearing read order;
* a refund larger than the remaining refundable amount blocks the month;
  no part of it is posted;
* a fractional minor-unit residue in the exact ratio blocks the refund
  rather than being hidden in the monthly total.

Refund allocations are stored as negative amounts, ordered by ``rank``
(0 = first consumed).  Each carries the original payment voucher, the
original rank and the original month, so the reduction can be traced
back to the exact payment allocation line, and a later month continues
from the same capacity.
"""
from __future__ import annotations

from .allocate import select_per_clearing
from .inputs import SourceRef
from .money import MoneyError, floor_rate, parse_rate, rate_remainder


def _issue(sink, code: str, ref: SourceRef, message: str, detail=None
           ) -> None:
    sink.add(code, ref, message, detail)


def plan_refunds(conn, package, sink, reader) -> dict[str, int]:
    """Validate refund vouchers and stage their negative allocation lines.

    Returns ``{refund_voucher_id: amount}`` for refunds that planned
    successfully.  A refund with any blocking issue is not planned at all
    (all-or-nothing, never partial).
    """
    refunds = [row for row in conn.execute(
        "SELECT * FROM vouchers WHERE voucher_type='refund'")]
    if not refunds:
        return {}

    # Voucher-number order is the only permitted application order.
    refunds.sort(key=lambda row: row["voucher_id"])
    planned: dict[str, int] = {}
    for row in refunds:
        voucher_id = row["voucher_id"]
        ref = SourceRef(row["source_file"], int(row["source_line"]))
        original_id = row["payment_voucher_id"]
        original = reader.voucher(package.ledger_id, original_id)
        if original is None:
            in_month = conn.execute(
                "SELECT 1 FROM vouchers WHERE ledger_id=? AND voucher_id=?",
                (package.ledger_id, original_id)).fetchone()
            if in_month is not None:
                _issue(sink, "refund_original_is_current_month", ref,
                       f"refund {voucher_id} references payment "
                       f"{original_id!r} of the current month; only earlier "
                       "closed payments are refundable",
                       {"refund_voucher": voucher_id,
                        "original_voucher": original_id})
            else:
                _issue(sink, "refund_original_not_closed", ref,
                       f"refund {voucher_id} references payment "
                       f"{original_id!r}, which is not in an earlier closed "
                       "month of this ledger",
                       {"refund_voucher": voucher_id,
                        "original_voucher": original_id})
            continue
        if original["voucher_type"] != "payment":
            _issue(sink, "refund_original_is_refund", ref,
                   f"refund {voucher_id} references {original_id!r}, itself "
                   "a refund voucher; refunds of refunds are not accepted",
                   {"refund_voucher": voucher_id,
                    "original_voucher": original_id})
            continue
        if original["currency"] != row["currency"]:
            _issue(sink, "refund_cross_currency", ref,
                   f"refund {voucher_id} is in {row['currency']} but the "
                   f"original {original_id!r} is in "
                   f"{original['currency']}; no FX conversion is performed",
                   {"refund_voucher": voucher_id,
                    "original_voucher": original_id})
            continue
        if row["direction"] != "inflow" or original["direction"] != "outflow":
            _issue(sink, "refund_cross_direction", ref,
                   "a refund must be an inflow against an outflow payment",
                   {"refund_direction": row["direction"],
                    "original_direction": original["direction"]})
            continue

        rate = None
        if row["rate"] is not None:
            try:
                rate = parse_rate(row["rate"], "rate")
            except MoneyError as exc:
                _issue(sink, "refund_bad_rate", ref, str(exc),
                       {"refund_voucher": voucher_id, "rate": row["rate"]})
                continue

        lines = reader.payment_lines(package.ledger_id, original_id)
        if not lines:
            _issue(sink, "refund_original_has_no_allocation", ref,
                   f"payment {original_id!r} has no recorded section "
                   "allocation to refund",
                   {"refund_voucher": voucher_id,
                    "original_voucher": original_id})
            continue

        if rate is not None:
            expected = floor_rate(int(original["amount_minor"]), rate)
            residue = rate_remainder(int(original["amount_minor"]), rate)
            if expected != int(row["amount_minor"]):
                _issue(sink, "refund_amount_mismatch", ref,
                       f"refund amount {row['amount_minor']} is not "
                       f"floor({original['amount_minor']} x {row['rate']}) "
                       f"= {expected}",
                       {"refund_voucher": voucher_id,
                        "original_voucher": original_id, "rate": row["rate"],
                        "expected": expected,
                        "submitted": int(row["amount_minor"])})
                continue
            if residue != 0:
                _issue(sink, "refund_ratio_residue", ref,
                       f"returning {row['rate']} of "
                       f"{original['amount_minor']} leaves an exact residue "
                       f"of {residue} minor unit(s) that cannot be paid in "
                       "whole units; the refund is blocked instead of "
                       "hiding the residue in the monthly total",
                       {"refund_voucher": voucher_id,
                        "original_voucher": original_id,
                        "residue": f"{residue.numerator}/"
                                   f"{residue.denominator}"})
                continue

        # Capacity is historical: previously closed refunds plus what this
        # month has already planned for the same original line.  Prior
        # consumption looks *across the whole ledger*, not just this
        # month, so a second refund month sees the earlier deduction.
        capacity: list[tuple[dict, int]] = []
        total_remaining = 0
        for line in sorted(lines, key=lambda r: (r["project_id"],
                                                 r["section_id"])):
            prior = reader.prior_consumed(package.ledger_id, original_id,
                                          int(line["rank"]))
            staged = -int(conn.execute(
                "SELECT COALESCE(SUM(amount_minor), 0) FROM refund_allocations "
                "WHERE ledger_id=? AND original_voucher_id=? AND "
                "original_rank=?", (package.ledger_id, original_id,
                                    int(line["rank"]))).fetchone()[0])
            remaining = int(line["amount_minor"]) - prior - staged
            if remaining < 0:
                remaining = 0
            capacity.append((line, remaining, prior))
            total_remaining += remaining

        if int(row["amount_minor"]) > total_remaining:
            _issue(sink, "refund_exceeds_remaining", ref,
                   f"refund {voucher_id} of {row['amount_minor']} exceeds "
                   f"the {total_remaining} still refundable on "
                   f"{original_id!r}; the month cannot close and no part of "
                   "the refund is posted",
                   {"refund_voucher": voucher_id,
                    "original_voucher": original_id,
                    "refund": int(row["amount_minor"]),
                    "remaining": total_remaining})
            continue

        # Larger key first; skip lines already exhausted.
        to_place = int(row["amount_minor"])
        placed: list[tuple[dict, int, int]] = []
        for line, remaining, prior in sorted(
                capacity, key=lambda item: (item[0]["project_id"],
                                            item[0]["section_id"]),
                reverse=True):
            if to_place == 0:
                break
            take = min(remaining, to_place)
            if take <= 0:
                continue
            placed.append((line, take, prior))
            to_place -= take
        if to_place != 0:
            _issue(sink, "refund_capacity_unresolved", ref,
                   "refund capacity could not be fully placed; retry",
                   {"refund_voucher": voucher_id})
            continue

        for rank, (line, take, prior) in enumerate(placed):
            conn.execute(
                "INSERT INTO refund_allocations(month, ledger_id, "
                "refund_voucher_id, rank, original_voucher_id, "
                "original_month, original_rank, project_id, section_id, "
                "amount_minor, capacity, prior_consumed, source_file, "
                "source_line) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (package.month, package.ledger_id, voucher_id, rank,
                 original_id, original["month"], int(line["rank"]),
                 line["project_id"], line["section_id"], -take,
                 int(line["amount_minor"]), prior, ref.file, ref.line))
        planned[voucher_id] = int(row["amount_minor"])
    return planned


def attach_refund_flows(conn, package) -> None:
    """Attribute each refund line to the bank flows that returned the money."""
    for voucher in conn.execute(
            "SELECT * FROM vouchers WHERE voucher_type='refund' ORDER BY "
            "voucher_id"):
        lines = list(conn.execute(
            "SELECT * FROM refund_allocations WHERE ledger_id=? AND "
            "refund_voucher_id=? ORDER BY rank",
            (package.ledger_id, voucher["voucher_id"])))
        if not lines:
            continue
        clearings = [(int(c["amount_minor"]), c["account_id"],
                      c["bank_flow_id"]) for c in conn.execute(
            "SELECT * FROM clearings WHERE valid=1 AND voucher_id=? "
            "ORDER BY account_id, bank_flow_id, clearing_id",
            (voucher["voucher_id"],))]
        total = sum(c[0] for c in clearings)
        if total != int(voucher["amount_minor"]):
            continue  # already reported as a reconciliation issue
        attribution = select_per_clearing(
            [(int(line["rank"]), -int(line["amount_minor"]),
              line["project_id"], line["section_id"]) for line in lines],
            clearings, total)
        for rank, account_id, bank_flow_id, share in attribution:
            conn.execute(
                "INSERT INTO refund_flow_lines(month, ledger_id, "
                "refund_voucher_id, rank, account_id, bank_flow_id, "
                "amount_minor) VALUES(?,?,?,?,?,?,?)",
                (package.month, package.ledger_id, voucher["voucher_id"],
                 rank, account_id, bank_flow_id, share))
