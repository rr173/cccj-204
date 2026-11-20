"""Conflict and evidence export.

Exports are written per result number and are fully self-describing:
every fact carries the source file, page, character span and the
exact original slice, so evidence can be re-opened in the read-only
snapshot without re-running extraction.
"""
from __future__ import annotations

import json
from pathlib import Path

from .storage import Store
from .timeline import (current_facts, document_statements, relation_audit,
                       timeline)


def _source_locator(store, document_id: str) -> str:
    row = store.get_document(document_id)
    if row is None:
        return ""
    return json.loads(row["raw_json"])["source"]["file"]


def _attach_locators(store, payloads: list[dict]) -> list[dict]:
    for p in payloads:
        if "document_id" in p:
            p["source_file"] = _source_locator(store, p["document_id"])
        for key in ("candidates", "provenance", "pending"):
            for item in p.get(key, []):
                if "document_id" in item:
                    item["source_file"] = _source_locator(
                        store, item["document_id"])
    return payloads


def export_all(result_dir: Path) -> dict[str, Path]:
    result_dir = result_dir.resolve()
    store = Store(result_dir / "store.sqlite3")
    written: dict[str, Path] = {}
    try:
        # 1. current facts at the latest known publish time, plus open t
        facts = current_facts(store, include_source_paths=False)
        _attach_locators(store, facts)
        conflicts = [f for f in facts if f["status"] == "conflict"]
        written["facts.json"] = _dump(result_dir / "facts.json", {
            "result_id": store.get_meta("result_id"),
            "rule_version": store.get_meta("rule_version"),
            "facts": facts})
        written["conflicts.json"] = _dump(result_dir / "conflicts.json", {
            "result_id": store.get_meta("result_id"),
            "conflicts": conflicts})

        # 2. evidence index: every assertion/unparsed span, streamed
        evidence = {"assertions": [], "unparsed": [], "issues": []}
        for r in store.iter_assertions():
            evidence["assertions"].append({
                "assertion_id": r["assertion_id"],
                "document_id": r["document_id"],
                "source_file": _source_locator(store, r["document_id"]),
                "field": r["field"], "status": r["status"],
                "pending_reason": r["pending_reason"],
                "normalized": r["normalized"], "raw_value": r["raw_value"],
                "currency": r["currency"], "header": r["header"],
                "scope_basis": r["scope_basis"],
                "published_at": r["published_at"],
                "span": json.loads(r["span_json"])})
        for u in store.iter_unparsed():
            evidence["unparsed"].append({
                "unparsed_id": u["unparsed_id"],
                "document_id": u["document_id"],
                "source_file": _source_locator(store, u["document_id"]),
                "field": u["field"], "reason": u["reason"],
                "raw_text": u["raw_text"],
                "span": json.loads(u["span_json"])})
        for i in store.conn.execute(
                "SELECT * FROM ingestion_issues ORDER BY issue_id"):
            evidence["issues"].append({
                "issue_id": i["issue_id"], "document_id": i["document_id"],
                "kind": i["kind"], "detail": i["detail"],
                "span": json.loads(i["span_json"]) if i["span_json"] else None})
        written["evidence.json"] = _dump(result_dir / "evidence.json", {
            "result_id": store.get_meta("result_id"), **evidence})

        # 3. relations audit and closures
        written["relations.json"] = _dump(result_dir / "relations.json", {
            "result_id": store.get_meta("result_id"),
            "relations": relation_audit(store),
            "closures": [dict(c) for c in store.iter_closures()]})

        # 4. per-document own statements (one reader entrance per doc)
        statements = {}
        for d in store.all_documents():
            statements[d["document_id"]] = document_statements(
                store, d["document_id"])
        written["statements.json"] = _dump(result_dir / "statements.json", {
            "result_id": store.get_meta("result_id"),
            "documents": statements})

        # 5. full timeline
        written["timeline.json"] = _dump(result_dir / "timeline.json", {
            "result_id": store.get_meta("result_id"),
            "events": timeline(store)})
    finally:
        store.close()
    return written


def _dump(path: Path, payload: dict) -> Path:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return path
