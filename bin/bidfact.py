#!/usr/bin/env python3
"""Command line entry point. Run with: python3 -I bin/bidfact.py ..."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bidfact.export import export_all  # noqa: E402
from bidfact.pipeline import run_pipeline  # noqa: E402
from bidfact.storage import Store  # noqa: E402
from bidfact.timeline import (current_facts, document_statements,  # noqa: E402
                              timeline)


def _print(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bidfact",
        description="Offline tender-announcement fact processor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="ingest+extract+validate one collection")
    p_run.add_argument("--collection", required=True, type=Path)
    p_run.add_argument("--relations", type=Path)
    p_run.add_argument("--results", required=True, type=Path)
    p_run.add_argument("--force", action="store_true")
    p_run.add_argument("--export", action="store_true")

    p_facts = sub.add_parser("facts", help="point-in-time current facts")
    p_facts.add_argument("result_dir", type=Path)
    p_facts.add_argument("--at", dest="at", default=None,
                         help="ISO datetime t; only docs published <= t count")
    p_facts.add_argument("--section", default=None)

    p_stmt = sub.add_parser("statement", help="one document's own statements")
    p_stmt.add_argument("result_dir", type=Path)
    p_stmt.add_argument("--document", required=True)

    p_tl = sub.add_parser("timeline", help="full assertion/closure timeline")
    p_tl.add_argument("result_dir", type=Path)
    p_tl.add_argument("--section", default=None)

    p_exp = sub.add_parser("export", help="write all JSON exports")
    p_exp.add_argument("result_dir", type=Path)

    args = parser.parse_args(argv)

    if args.cmd == "run":
        out = run_pipeline(args.collection, args.results,
                           args.relations, force_rebuild=args.force)
        if args.export:
            out["exports"] = {k: str(v) for k, v in
                              export_all(Path(out["path"])).items()}
        _print(out)
        return 0

    if args.cmd == "facts":
        store = Store(args.result_dir / "store.sqlite3")
        try:
            _print({"result_id": store.get_meta("result_id"),
                    "at": args.at,
                    "facts": current_facts(store, t=args.at,
                                           section_id=args.section)})
        finally:
            store.close()
        return 0

    if args.cmd == "statement":
        store = Store(args.result_dir / "store.sqlite3")
        try:
            _print(document_statements(store, args.document))
        finally:
            store.close()
        return 0

    if args.cmd == "timeline":
        store = Store(args.result_dir / "store.sqlite3")
        try:
            _print({"result_id": store.get_meta("result_id"),
                    "events": timeline(store, section_id=args.section)})
        finally:
            store.close()
        return 0

    if args.cmd == "export":
        _print({k: str(v) for k, v in export_all(args.result_dir).items()})
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
