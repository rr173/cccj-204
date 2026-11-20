"""Relation validation and closure application.

The business-side relation list is the *only* authority for correction
relationships. This module never invents a replacement from publish
order or site ranking; it checks, per relation item:

1. acting and target documents exist; verb and fields are supported;
2. target sections resolve to a unique section on the target document;
3. every quote slice points back into the acting document's own words;
4. replace/supplement items carry confirmed extracted new values, and
   retract items resolve the assertions they name;
5. targets must not be future documents, and replacement edges must
   not form a cycle (structural check, independent of traversal order).

A failing item becomes ``pending`` (or ``invalid`` for structural
forbidden shapes) and closes nothing; independent items still apply.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .errors import ManifestError
from .ingestion import load_pages
from .models import (Closure, F_BUDGET, F_DEADLINE, F_RESULT_AMOUNT,
                     F_RESULT_UNIT, FIELDS, VERBS, V_REPLACE, V_RETRACT,
                     V_SUPPLEMENT)
from .util import digest_bytes, stable_id

STATUS_CONFIRMED = "confirmed"
STATUS_PENDING = "pending"
STATUS_INVALID = "invalid"


def load_relations(path: Path) -> tuple[list[dict], str]:
    with path.open("rb") as fh:
        raw = fh.read()
    payload = json.loads(raw.decode("utf-8"))
    rels = payload.get("relations")
    if not isinstance(rels, list):
        raise ManifestError(f"{path}: relations must be a list")
    for i, rel in enumerate(rels):
        for key in ("relation_id", "acting_document_id", "verb",
                    "target_document_id", "fields"):
            if key not in rel:
                raise ManifestError(f"relations[{i}] missing {key}")
        if rel["verb"] not in VERBS:
            raise ManifestError(f"relations[{i}] bad verb {rel['verb']}")
        for f in rel["fields"]:
            if f not in FIELDS:
                raise ManifestError(f"relations[{i}] bad field {f}")
    return rels, digest_bytes(raw)


def _acting_page_map(collection_dir: Path, doc_row) -> dict[int, str]:
    doc = json.loads(doc_row["raw_json"])
    pages, _ = load_pages(collection_dir, doc)
    return {p.page_no: p.text for p in pages}


def _verify_quotes(collection_dir: Path, doc_row, quotes: list[dict]
                   ) -> Optional[str]:
    """All quotes must slice the acting document's own pages."""
    if not quotes:
        return "no_quote_backreference"
    page_map = _acting_page_map(collection_dir, doc_row)
    for q in quotes:
        page = page_map.get(int(q["page_no"]))
        if page is None:
            return f"quote page {q['page_no']} absent"
        start, end = int(q["start"]), int(q["end"])
        if start < 0 or end > len(page) or page[start:end] != q.get("text"):
            return ("quote slice mismatch on page "
                    f"{q['page_no']} [{start},{end})")
    return None


def _target_sections(store, target_doc_id: str, requested: list[str]
                     ) -> tuple[list[str], Optional[str]]:
    row = store.get_document(target_doc_id)
    if row is None:
        return [], "target_document_absent"
    manifest_sections = [r["section_id"] for r in store.conn.execute(
        "SELECT section_id FROM doc_sections WHERE document_id=? "
        "ORDER BY section_id", (target_doc_id,))]
    if not requested:
        if len(manifest_sections) == 1:
            return manifest_sections, None
        return [], ("target sections not unique; manifest="
                    f"{manifest_sections}")
    bad = [s for s in requested if s not in manifest_sections]
    if bad:
        return [], f"unknown target sections {bad}"
    return requested, None


def _target_assertions(store, target_doc_id: str, sections: list[str],
                       fields: list[str]) -> list:
    placeholders = ",".join("?" for _ in sections)
    return list(store.conn.execute(
        f"SELECT * FROM assertions WHERE document_id=? AND status='confirmed' "
        f"AND section_id IN ({placeholders}) AND field IN "
        f"({','.join('?' for _ in fields)}) ORDER BY assertion_id",
        [target_doc_id, *sections, *fields]))


def _detect_invalid_shapes(rels: list[dict], docs_by_id: dict
                           ) -> dict[str, str]:
    """Return relation_id -> reason for future-point and replacement cycles."""
    reasons: dict[str, str] = {}
    for rel in rels:
        acting = docs_by_id.get(rel["acting_document_id"])
        target = docs_by_id.get(rel["target_document_id"])
        if acting is None or target is None:
            continue
        if target["published_at"] > acting["published_at"]:
            reasons[rel["relation_id"]] = (
                "invalid_future_target: target published at "
                f"{target['published_at']} after acting "
                f"{acting['published_at']}")
    # Replacement graph over structurally admissible edges; cycles are a
    # property of the graph itself, not of any traversal order.
    edges: dict[str, list[tuple[str, str]]] = {}
    for rel in rels:
        if rel["relation_id"] in reasons or rel["verb"] != V_REPLACE:
            continue
        if (rel["acting_document_id"] in docs_by_id
                and rel["target_document_id"] in docs_by_id):
            edges.setdefault(rel["acting_document_id"], []).append(
                (rel["target_document_id"], rel["relation_id"]))

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {d: WHITE for d in docs_by_id}
    stack: list[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt, rel_id in edges.get(node, ()):  # type: ignore[func-returns-value]
            if color.get(nxt) == GRAY:
                loop_start = stack.index(nxt)
                loop_nodes = set(stack[loop_start:]) | {nxt}
                for rid, rel in ((r["relation_id"], r) for r in rels
                                 if r["verb"] == V_REPLACE):
                    if (rel["acting_document_id"] in loop_nodes
                            and rel["target_document_id"] in loop_nodes):
                        reasons.setdefault(rid, "invalid_replacement_cycle")
            elif color.get(nxt) == WHITE:
                visit(nxt)
        stack.pop()
        color[node] = BLACK

    for d in docs_by_id:
        if color[d] == WHITE:
            visit(d)
    return reasons


def validate_and_apply(store, collection_dir: Path, rels: list[dict]
                       ) -> list[dict]:
    docs_by_id = {r["document_id"]: r for r in store.all_documents()}
    invalid_reasons = _detect_invalid_shapes(rels, docs_by_id)
    results: list[dict] = []

    for rel in rels:
        record = dict(rel)
        record.setdefault("target_sections", [])
        acting = docs_by_id.get(rel["acting_document_id"])
        # Relations carry no independent publish time: they act at the
        # acting document's own publication instant.
        record["published_at"] = (acting["published_at"] if acting is not None
                                  else "")
        rid = rel["relation_id"]
        if rid in invalid_reasons:
            record.update(status=STATUS_INVALID, detail=invalid_reasons[rid])
            store.upsert_relation(record)
            results.append(record)
            continue

        if acting is None:
            record.update(status=STATUS_PENDING,
                          detail="acting_document_absent")
            store.upsert_relation(record)
            results.append(record)
            continue

        qerr = _verify_quotes(collection_dir, acting, rel.get("quote", []))
        if qerr:
            record.update(status=STATUS_PENDING, detail=f"quote: {qerr}")
            store.upsert_relation(record)
            results.append(record)
            continue

        sections, serr = _target_sections(
            store, rel["target_document_id"], rel.get("target_sections", []))
        if serr:
            record.update(status=STATUS_PENDING, detail=serr)
            store.upsert_relation(record)
            results.append(record)
            continue

        targets = _target_assertions(
            store, rel["target_document_id"], sections, rel["fields"])

        # The new values claimed by replace/supplement must already exist
        # as confirmed assertions of the acting document.
        acting_assertions = list(store.conn.execute(
            "SELECT * FROM assertions WHERE document_id=? AND status='confirmed'",
            (rel["acting_document_id"],)))
        acting_fields = {a["field"] for a in acting_assertions}
        if rel["verb"] in (V_REPLACE, V_SUPPLEMENT):
            missing = [f for f in rel["fields"] if f not in acting_fields]
            if missing:
                record.update(
                    status=STATUS_PENDING,
                    detail=f"new value extraction not confirmed for {missing}")
                store.upsert_relation(record)
                results.append(record)
                continue

        if rel["verb"] in (V_REPLACE, V_RETRACT) and not targets:
            record.update(
                status=STATUS_PENDING,
                detail=("no matching confirmed target assertion in "
                        f"{rel['target_document_id']} sections {sections} "
                        f"fields {rel['fields']}"))
            store.upsert_relation(record)
            results.append(record)
            continue

        # Confirmed: replace/retract close exactly the targeted assertions.
        # retract never restores anything; nothing here reopens old rows.
        if rel["verb"] in (V_REPLACE, V_RETRACT):
            for t in targets:
                cid = stable_id("clo", rid, t["assertion_id"])
                store.add_closure(Closure(
                    closure_id=cid, relation_id=rid,
                    acting_document_id=rel["acting_document_id"],
                    target_assertion_id=t["assertion_id"], verb=rel["verb"],
                    field=t["field"], published_at=acting["published_at"]))
        detail = (f"applied {rel['verb']}; target_assertions="
                  f"{[t['assertion_id'] for t in targets]}")
        record.update(status=STATUS_CONFIRMED, detail=detail)
        store.upsert_relation(record)
        results.append(record)

    store.commit()
    return results
