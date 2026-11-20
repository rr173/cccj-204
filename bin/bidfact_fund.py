#!/usr/bin/env python3
"""Command line entry point for the monthly purchase-fund ledger.

Run with::

    python3 -I bin/bidfact.py fund <command> ...

Commands
--------
trial        validate one month package, write nothing durable
close        append the month to the fund ledger (idempotent, atomic)
month        read one closed month back (rows + summary)
cumulative   net payment per section up to a month, optionally filtered
months       list closed months
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:  # imported by bin/bidfact.py, which already fixed sys.path
    from bidfact.fund import (CloseBlocked, FundError, MonthAlreadyClosed,
                              close_month, months_closed, read_cumulative,
                              read_month, trial_close)
except ImportError:  # direct execution: python3 -I bin/bidfact_fund.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from bidfact.fund import (CloseBlocked, FundError, MonthAlreadyClosed,
                              close_month, months_closed, read_cumulative,
                              read_month, trial_close)


def _print(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def add_arguments(sub) -> None:
    parser = sub.add_parser(
        "fund", help="monthly purchase-fund close (资金月结) over a "
                     "separate fund ledger")
    commands = parser.add_subparsers(dest="fund_cmd", required=True)

    def with_ledger(name: str, help_text: str):
        p = commands.add_parser(name, help=help_text)
        p.add_argument("--ledger", required=True, type=Path,
                       help="directory holding fund.sqlite3")
        return p

    p_trial = with_ledger("trial", "validate one month, write nothing")
    p_trial.add_argument("--package", required=True, type=Path,
                         help="month package directory (manifest.json)")

    p_close = with_ledger("close", "atomically append the month")
    p_close.add_argument("--package", required=True, type=Path)

    p_month = with_ledger("month", "read one closed month")
    p_month.add_argument("--month", required=True)

    p_cum = with_ledger("cumulative", "net per section up to a month")
    p_cum.add_argument("--month", required=True)
    p_cum.add_argument("--project", default=None)
    p_cum.add_argument("--section", default=None)

    with_ledger("months", "list closed months")

    parser.set_defaults(func=run_fund)


def run_fund(args) -> int:
    try:
        if args.fund_cmd == "trial":
            report = trial_close(args.package, args.ledger)
            _print(report.as_dict())
            return 0 if report.ok else 1
        if args.fund_cmd == "close":
            _print(close_month(args.package, args.ledger))
            return 0
        if args.fund_cmd == "month":
            _print(read_month(args.ledger, args.month))
            return 0
        if args.fund_cmd == "cumulative":
            _print({"month": args.month,
                    "sections": read_cumulative(
                        args.ledger, args.month,
                        project_id=args.project, section_id=args.section)})
            return 0
        if args.fund_cmd == "months":
            _print({"months": months_closed(args.ledger)})
            return 0
    except CloseBlocked as exc:
        _print({"ok": False, "month": exc.month, "error": "close_blocked",
                "issues": exc.issues})
        return 1
    except MonthAlreadyClosed as exc:
        _print({"ok": False, "month": exc.month,
                "error": "month_already_closed",
                "closed_input_digest": exc.closed_input_digest,
                "submitted_input_digest": exc.submitted_input_digest})
        return 1
    except FundError as exc:
        _print({"ok": False, "error": "fund_error", "message": str(exc)})
        return 1
    return 2
