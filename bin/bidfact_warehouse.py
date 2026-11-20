#!/usr/bin/env python3
"""Command line entry point for the warehouse lineage library.

Run with::

    python3 -I bin/bidfact.py warehouse build  --package P --library L
    python3 -I bin/bidfact.py warehouse recall --library L \\
        --supplier S --batch B --sku K --cutoff 42

``build`` is idempotent for an identical package, refuses to overwrite a
different usable library without ``--force``, and reports every blocking
issue with its source file and line.  ``recall`` only reads the library.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:  # imported by bin/bidfact.py, which already fixed sys.path
    from bidfact.warehouse import (BuildBlocked, LibraryError, LibraryExists,
                                   RecallError, RecallQuery, WarehouseError,
                                   build_library, iter_recall, recall)
except ImportError:  # direct execution: python3 -I bin/bidfact_warehouse.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from bidfact.warehouse import (BuildBlocked, LibraryError, LibraryExists,
                                   RecallError, RecallQuery, WarehouseError,
                                   build_library, iter_recall, recall)


def _print(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def add_arguments(sub) -> None:
    parser = sub.add_parser(
        "warehouse", help="offline receipt-lot lineage and recall lists "
                          "(收货批次血缘 / 召回清单)")
    commands = parser.add_subparsers(dest="warehouse_cmd", required=True)

    p_build = commands.add_parser(
        "build", help="validate a package and deliver the lineage library")
    p_build.add_argument("--package", required=True, type=Path,
                         help="package directory (manifest.json)")
    p_build.add_argument("--library", required=True, type=Path,
                         help="directory holding warehouse.sqlite3")
    p_build.add_argument("--force", action="store_true",
                         help="replace an existing usable library built "
                              "from different inputs")

    p_recall = commands.add_parser(
        "recall", help="read-only recall list at a business cutoff")
    p_recall.add_argument("--library", required=True, type=Path)
    p_recall.add_argument("--supplier", required=True)
    p_recall.add_argument("--batch", required=True,
                          help="supplier batch number (原厂批号)")
    p_recall.add_argument("--sku", required=True)
    p_recall.add_argument("--cutoff", required=True, type=int,
                          help="business sequence cutoff, inclusive")
    p_recall.add_argument("--receipt", default=None,
                          help="restrict to one matching receipt")
    p_recall.add_argument("--from-pos", dest="from_pos", type=int,
                          default=None,
                          help="local half-open range start in the receipt")
    p_recall.add_argument("--to-pos", dest="to_pos", type=int, default=None,
                          help="local half-open range end in the receipt")
    p_recall.add_argument("--jsonl", type=Path, default=None,
                          help="stream segments as JSONL instead of one "
                               "JSON document")
    parser.set_defaults(func=run_warehouse)


def run_warehouse(args) -> int:
    try:
        if args.warehouse_cmd == "build":
            try:
                report = build_library(args.package, args.library,
                                       force=args.force)
            except BuildBlocked as exc:
                _print({"ok": False, "error": "build_blocked",
                        "issues": exc.issues})
                return 1
            _print(report.as_dict())
            return 0 if report.ok else 1
        if args.warehouse_cmd == "recall":
            query = RecallQuery(
                supplier_id=args.supplier, supplier_batch=args.batch,
                sku=args.sku, cutoff_seq=args.cutoff,
                receipt_id=args.receipt, from_pos=args.from_pos,
                to_pos=args.to_pos)
            if args.jsonl is not None:
                totals = None
                args.jsonl.parent.mkdir(parents=True, exist_ok=True)
                with args.jsonl.open("w", encoding="utf-8") as fh:
                    for record in iter_recall(args.library, query):
                        if record["record"] == "totals":
                            totals = record
                        fh.write(json.dumps(record, ensure_ascii=False,
                                            sort_keys=True))
                        fh.write("\n")
                _print(dict(totals or {}, jsonl=str(args.jsonl)))
                return 0
            _print(recall(args.library, query).as_dict())
            return 0
    except LibraryExists as exc:
        _print({"ok": False, "error": "library_exists",
                "path": exc.path,
                "existing_digest": exc.existing_digest,
                "new_digest": exc.new_digest})
        return 1
    except (WarehouseError, LibraryError) as exc:
        _print({"ok": False, "error": "warehouse_error",
                "message": str(exc)})
        return 1
    return 2
