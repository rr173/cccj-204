"""Cross-ledger reconciliation over the staging database.

The clearing rows are the only link between a bank debit and a
voucher.  Every check runs against the *staging* SQLite file, so a
month is never held in memory:

1. **Originals exist.**  Each clearing references an existing bank flow
   and voucher of the same account set, month, currency and direction.
   A missing reference is located by key and is never offset against
   another balance.
2. **Pair coverage.**  At most one clearing row per ``(bank flow,
   voucher)`` pair; duplicates are reported (the first row in source
   order is kept, so the outcome never depends on read order).  A
   clearing may not exceed either original; the exact difference is
   reported.
3. **Each end independently.**  Bank-side allocated amounts are compared
   with each flow's original; voucher-side amounts with each voucher's.
   A shortage or excess is reported for that exact flow/voucher with
   the difference.  Nothing elsewhere can cancel it.
4. **Both control sides, separately.**  Bank control groups are keyed by
   ``(account, currency, direction)``; voucher control groups by
   ``(currency, direction)``.  Missing groups, extra groups, duplicate
   groups and count/amount mismatches are blocking issues on the side
   that declared them; one side's own sum never substitutes for the
   other side's control numbers.

Issues are written to ``stg_issues``; failing clearing rows get
``valid=0`` so later stages only see reconciled links.
"""
from __future__ import annotations

import json

from .inputs import MonthPackage, SourceRef



def _issue(sink, code: str, ref: SourceRef, message: str, detail=None
           ) -> None:
    sink.add(code, ref, message, detail)


def reconcile_stage(conn, package: MonthPackage, sink) -> None:
    """Run every reconciliation check and persist its issues."""
    _foreign_rows(conn, package, sink)
    _missing_references(conn, package, sink)
    _duplicate_pairs(conn, package, sink)
    _link_consistency(conn, package, sink)
    _end_balances(conn, package, sink)
    _control_totals(conn, package, sink)


# ---------------------------------------------------------------------------
def _foreign_rows(conn, package, sink) -> None:
    for row in conn.execute(
            "SELECT * FROM bank_flows WHERE ledger_id != ? OR month != ?",
            (package.ledger_id, package.month)):
        _issue(sink, "foreign_bank_flow",
               SourceRef(row["source_file"], int(row["source_line"])),
               f"bank flow {row['account_id']}/{row['bank_flow_id']} "
               f"belongs to ledger {row['ledger_id']!r} month "
               f"{row['month']!r}, not {package.ledger_id!r} "
               f"{package.month!r}",
               {"ledger_id": row["ledger_id"], "month": row["month"]})
    for row in conn.execute(
            "SELECT * FROM vouchers WHERE ledger_id != ? OR month != ?",
            (package.ledger_id, package.month)):
        _issue(sink, "foreign_voucher",
               SourceRef(row["source_file"], int(row["source_line"])),
               f"voucher {row['voucher_id']} belongs to ledger "
               f"{row['ledger_id']!r} month {row['month']!r}, not "
               f"{package.ledger_id!r} {package.month!r}",
               {"ledger_id": row["ledger_id"], "month": row["month"]})
    for row in conn.execute(
            "SELECT * FROM clearings WHERE ledger_id != ?",
            (package.ledger_id,)):
        _issue(sink, "clearing_foreign_ledger",
               SourceRef(row["source_file"], int(row["source_line"])),
               f"clearing is booked under ledger {row['ledger_id']!r}, not "
               f"{package.ledger_id!r}", {"ledger": row["ledger_id"]})
        conn.execute("UPDATE clearings SET valid=0 WHERE clearing_id=?",
                     (row["clearing_id"],))
    conn.execute(
        "UPDATE clearings SET valid=0 WHERE ledger_id != ? AND valid=1",
        (package.ledger_id,))


def _missing_references(conn, package, sink) -> None:
    for row in conn.execute(
            "SELECT c.* FROM clearings c LEFT JOIN bank_flows f ON "
            "f.ledger_id=c.ledger_id AND f.account_id=c.account_id AND "
            "f.bank_flow_id=c.bank_flow_id WHERE c.valid=1 AND "
            "f.bank_flow_id IS NULL"):
        _issue(sink, "clearing_unknown_bank_flow",
               SourceRef(row["source_file"], int(row["source_line"])),
               f"clearing references bank flow {row['account_id']}/"
               f"{row['bank_flow_id']} that is not in this month's bank "
               "statement",
               {"bank_flow": [row["account_id"], row["bank_flow_id"]]})
        conn.execute("UPDATE clearings SET valid=0 WHERE clearing_id=?",
                     (row["clearing_id"],))
    for row in conn.execute(
            "SELECT c.* FROM clearings c LEFT JOIN vouchers v ON "
            "v.ledger_id=c.ledger_id AND v.voucher_id=c.voucher_id "
            "WHERE c.valid=1 AND v.voucher_id IS NULL"):
        _issue(sink, "clearing_unknown_voucher",
               SourceRef(row["source_file"], int(row["source_line"])),
               f"clearing references voucher {row['voucher_id']} that is "
               "not in this month's voucher file",
               {"voucher": row["voucher_id"]})
        conn.execute("UPDATE clearings SET valid=0 WHERE clearing_id=?",
                     (row["clearing_id"],))


def _duplicate_pairs(conn, package, sink) -> None:
    previous: dict[tuple, dict] = {}
    for row in conn.execute(
            "SELECT * FROM clearings WHERE valid=1 ORDER BY source_file, "
            "source_line, clearing_id"):
        key = (row["ledger_id"], row["account_id"], row["bank_flow_id"],
               row["voucher_id"])
        if key in previous:
            first = previous[key]
            _issue(sink, "duplicate_clearing_pair",
                   SourceRef(row["source_file"], int(row["source_line"])),
                   f"another clearing for bank flow {row['account_id']}/"
                   f"{row['bank_flow_id']} and voucher {row['voucher_id']} "
                   f"is already at {first['source_file']}:"
                   f"{first['source_line']}; one pair may have at most one "
                   "clearing row",
                   {"bank_flow": [row["account_id"], row["bank_flow_id"]],
                    "voucher": row["voucher_id"],
                    "first_source": {
                        "file": first["source_file"],
                        "line": int(first["source_line"])}})
            conn.execute("UPDATE clearings SET valid=0 WHERE clearing_id=?",
                         (row["clearing_id"],))
        else:
            previous[key] = row


def _link_consistency(conn, package, sink) -> None:
    # Every defect on one crossing is reported, not only the first one
    # found: the row is invalidated after all its checks have run.
    checks = (
        ("clearing_cross_month", "f.month != ? OR v.month != ?",
         (package.month, package.month),
         "clearing links a bank flow/voucher that is not in month "
         f"{package.month}"),
        ("clearing_cross_currency", "f.currency != v.currency", (),
         "clearing links a bank flow and voucher in different currencies; "
         "no FX conversion is performed"),
        ("clearing_cross_direction", "f.direction != v.direction", (),
         "clearing links a bank flow and voucher of different directions"),
        ("clearing_exceeds_bank_flow", "c.amount_minor > f.amount_minor", (),
         "clearing exceeds the bank flow original"),
        ("clearing_exceeds_voucher", "c.amount_minor > v.amount_minor", (),
         "clearing exceeds the voucher original"),
    )
    bad_ids: set[int] = set()
    for code, predicate, params, text in checks:
        rows = list(conn.execute(
            f"SELECT c.*, f.currency AS flow_currency, "
            f"f.direction AS flow_direction, f.month AS flow_month, "
            f"f.amount_minor AS flow_amount, v.currency AS voucher_currency, "
            f"v.direction AS voucher_direction, v.month AS voucher_month, "
            f"v.amount_minor AS voucher_amount FROM clearings c "
            f"JOIN bank_flows f ON f.ledger_id=c.ledger_id AND "
            f"f.account_id=c.account_id AND f.bank_flow_id=c.bank_flow_id "
            f"JOIN vouchers v ON v.ledger_id=c.ledger_id AND "
            f"v.voucher_id=c.voucher_id WHERE c.valid=1 AND ({predicate})",
            params))
        for row in rows:
            detail = {
                "bank_flow": [row["account_id"], row["bank_flow_id"]],
                "voucher": row["voucher_id"],
                "bank_currency": row["flow_currency"],
                "voucher_currency": row["voucher_currency"],
                "bank_direction": row["flow_direction"],
                "voucher_direction": row["voucher_direction"],
                "bank_month": row["flow_month"],
                "voucher_month": row["voucher_month"],
                "month": package.month,
            }
            if code == "clearing_exceeds_bank_flow":
                detail["difference"] = (
                    row["amount_minor"] - row["flow_amount"])
            elif code == "clearing_exceeds_voucher":
                detail["difference"] = (
                    row["amount_minor"] - row["voucher_amount"])
            _issue(sink, code,
                   SourceRef(row["source_file"], int(row["source_line"])),
                   f"{text} (bank flow {row['account_id']}/"
                   f"{row['bank_flow_id']}, voucher {row['voucher_id']})",
                   detail)
            bad_ids.add(int(row["clearing_id"]))
    for clearing_id in bad_ids:
        conn.execute("UPDATE clearings SET valid=0 WHERE clearing_id=?",
                     (clearing_id,))


def _end_balances(conn, package, sink) -> None:
    for row in conn.execute(
            "SELECT f.*, COALESCE(SUM(c.amount_minor), 0) AS allocated "
            "FROM bank_flows f LEFT JOIN clearings c ON "
            "c.valid=1 AND c.ledger_id=f.ledger_id AND "
            "c.account_id=f.account_id AND c.bank_flow_id=f.bank_flow_id "
            "GROUP BY f.ledger_id, f.account_id, f.bank_flow_id "
            "ORDER BY f.source_file, f.source_line"):
        diff = int(row["amount_minor"]) - int(row["allocated"])
        if diff == 0:
            continue
        key = f"{row['account_id']}/{row['bank_flow_id']}"
        code = "bank_flow_short" if diff > 0 else "bank_flow_over_allocated"
        verb = "short" if diff > 0 else "over-allocated"
        _issue(sink, code,
               SourceRef(row["source_file"], int(row["source_line"])),
               f"bank flow {key} is {verb}: amount {row['amount_minor']}, "
               f"cleared {row['allocated']}, difference {abs(diff)}",
               {"key": key, "amount": int(row["amount_minor"]),
                "allocated": int(row["allocated"]),
                "difference": abs(diff)})
    for row in conn.execute(
            "SELECT v.*, COALESCE(SUM(c.amount_minor), 0) AS allocated "
            "FROM vouchers v LEFT JOIN clearings c ON "
            "c.valid=1 AND c.ledger_id=v.ledger_id AND "
            "c.voucher_id=v.voucher_id "
            "GROUP BY v.ledger_id, v.voucher_id "
            "ORDER BY v.source_file, v.source_line"):
        diff = int(row["amount_minor"]) - int(row["allocated"])
        if diff == 0:
            continue
        key = row["voucher_id"]
        code = "voucher_short" if diff > 0 else "voucher_over_allocated"
        verb = "short" if diff > 0 else "over-allocated"
        _issue(sink, code,
               SourceRef(row["source_file"], int(row["source_line"])),
               f"voucher {key} is {verb}: amount {row['amount_minor']}, "
               f"cleared {row['allocated']}, difference {abs(diff)}",
               {"key": key, "amount": int(row["amount_minor"]),
                "allocated": int(row["allocated"]),
                "difference": abs(diff)})


def _control_totals(conn, package, sink) -> None:
    for side in ("bank", "voucher"):
        control_rows = list(conn.execute(
            "SELECT * FROM stg_control WHERE side=? ORDER BY "
            "source_file, source_line", (side,)))
        control: dict[tuple, object] = {}
        for row in control_rows:
            key = (row["account_id"], row["currency"], row["direction"])
            if key in control:
                first = control[key]
                _issue(sink, "control_duplicate_group",
                       SourceRef(row["source_file"],
                                 int(row["source_line"])),
                       f"{side} control group {_group(key)} appears again "
                       f"at {first['source_file']}:{first['source_line']}; "
                       "groups are never summed",
                       {"group": _group(key),
                        "first_source": {"file": first["source_file"],
                                         "line": int(first["source_line"])}})
                continue
            control[key] = row

        actual = {}
        first_row = {}
        if side == "bank":
            sql = ("SELECT account_id, currency, direction, COUNT(*) AS n, "
                   "SUM(amount_minor) AS total, MIN(source_file) AS f, "
                   "MIN(source_line) AS l FROM bank_flows "
                   "GROUP BY account_id, currency, direction")
        else:
            sql = ("SELECT NULL AS account_id, currency, direction, "
                   "COUNT(*) AS n, SUM(amount_minor) AS total, "
                   "MIN(source_file) AS f, MIN(source_line) AS l "
                   "FROM vouchers GROUP BY currency, direction")
        for row in conn.execute(sql):
            key = (row["account_id"], row["currency"], row["direction"])
            actual[key] = (int(row["n"]), int(row["total"]))
            first_row[key] = (row["f"], int(row["l"]))

        for key in sorted(actual, key=lambda k: (str(k[0]), k[1], k[2])):
            count, amount = actual[key]
            if key not in control:
                _issue(sink, "control_group_missing",
                       SourceRef(*first_row[key]),
                       f"{side} control has no row for group {_group(key)}; "
                       "one side's own sum never substitutes for the "
                       "control numbers",
                       {"side": side, "group": _group(key),
                        "actual_count": count, "actual_amount": amount})
                continue
            c = control[key]
            if int(c["count"]) != count:
                _issue(sink, "control_count_mismatch",
                       SourceRef(c["source_file"], int(c["source_line"])),
                       f"{side} control count for {_group(key)} is "
                       f"{c['count']} but the monthly data has {count}",
                       {"side": side, "group": _group(key),
                        "control": int(c["count"]), "actual": count,
                        "difference": count - int(c["count"])})
            if int(c["amount_minor"]) != amount:
                _issue(sink, "control_amount_mismatch",
                       SourceRef(c["source_file"], int(c["source_line"])),
                       f"{side} control amount for {_group(key)} is "
                       f"{c['amount_minor']} but the monthly data sums to "
                       f"{amount}",
                       {"side": side, "group": _group(key),
                        "control": int(c["amount_minor"]), "actual": amount,
                        "difference": amount - int(c["amount_minor"])})
        for key in sorted(control, key=lambda k: (str(k[0]), k[1], k[2])):
            if key not in actual:
                c = control[key]
                _issue(sink, "control_group_without_data",
                       SourceRef(c["source_file"], int(c["source_line"])),
                       f"{side} control declares group {_group(key)} but "
                       "the monthly data has no such row",
                       {"side": side, "group": _group(key)})


def _group(key: tuple) -> list:
    return [x for x in key if x is not None]
