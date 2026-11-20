"""Point-in-time facts, per-document statements and full timeline.

The three read entrances are deliberately separate functions:

* :func:`current_facts`  — what is true at time ``t``
* :func:`document_statements` — what one document itself said
* :func:`timeline` — every assertion and closure event in order

Only confirmed assertions and confirmed relations with publish time
not later than ``t`` participate. Values are never chosen by publish
order or site order: equal open values collapse to one answer with all
provenance, differing open values are reported as ``conflict``.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from .models import (FIELDS, S_ABSENT, S_CONFLICT, S_PENDING, S_VALUE,
                     S_WITHDRAWN)


@dataclass(frozen=True)
class Fact:
    project_id: Optional[str]
    section_id: Optional[str]
    field: str
    status: str
    value: Optional[dict]
    candidates: tuple[dict, ...]
    pending: tuple[dict, ...]
    provenance: tuple[dict, ...]


def _row_span(row) -> dict:
    return json.loads(row["span_json"])


def _assertion_payload(row, store, collection_dir: Optional[str] = None
                       ) -> dict:
    out = {
        "assertion_id": row["assertion_id"],
        "document_id": row["document_id"],
        "section_id": row["section_id"],
        "field": row["field"],
        "status": row["status"],
        "pending_reason": row["pending_reason"],
        "normalized": row["normalized"],
        "raw_value": row["raw_value"],
        "currency": row["currency"],
        "header": row["header"],
        "scope_basis": row["scope_basis"],
        "published_at": row["published_at"],
        "span": _row_span(row),
    }
    if collection_dir:
        doc_row = store.get_document(row["document_id"])
        if doc_row:
            out["source_file"] = json.loads(doc_row["raw_json"])["source"]["file"]
    return out


def _eligible_assertions(store, t: Optional[str]):
    rows = list(store.iter_assertions(status="confirmed"))
    if t is not None:
        rows = [r for r in rows if r["published_at"] <= t]
    return rows


def _closed_ids_at(store, t: Optional[str]) -> set[str]:
    closed: set[str] = set()
    for c in store.iter_closures():
        if t is None or c["published_at"] <= t:
            closed.add(c["target_assertion_id"])
    return closed


def _ever_asserted_at(store, t: Optional[str]) -> set[str]:
    return {r["assertion_id"] for r in _eligible_assertions(store, t)}


def _pending_payloads(store, t: Optional[str]) -> list:
    out = []
    for r in store.iter_assertions(status="pending"):
        if t is None or r["published_at"] <= t:
            out.append(_assertion_payload(r, store))
    return out


def current_facts(store, t: Optional[str] = None,
                  section_id: Optional[str] = None,
                  include_source_paths: bool = False) -> list[dict]:
    """Answer the business query: facts visible at time ``t``."""
    eligible = _eligible_assertions(store, t)
    closed = _closed_ids_at(store, t)
    ever = _ever_asserted_at(store, t)
    pending = _pending_payloads(store, t)

    groups: dict[tuple, list] = defaultdict(list)
    all_sections: dict[tuple, Optional[str]] = {}
    for row in eligible:
        key = (row["project_id"], row["section_id"], row["field"])
        all_sections[key] = row["section_id"]
        if row["assertion_id"] in closed:
            continue
        groups[key].append(row)

    # Pending material is listed alongside, never presented as fact.
    # A pending assertion whose section could not be resolved (None) is
    # distributed to every section the publishing document's manifest
    # binds, so an analyst querying any candidate section sees it.
    doc_sections: dict[str, list[Optional[str]]] = {}
    for d in store.all_documents():
        secs = [r["section_id"] for r in store.conn.execute(
            "SELECT section_id FROM doc_sections WHERE document_id=? "
            "ORDER BY section_id", (d["document_id"],))] or [None]
        doc_sections[d["document_id"]] = secs

    pending_by_key: dict[tuple, list] = defaultdict(list)
    doc_project = {d["document_id"]: d["project_id"]
                   for d in store.all_documents()}
    for p in pending:
        # Unresolved prose assertions may store project_id None; bind to
        # the publishing document's manifest project so scope keys merge.
        project = p.get("project_id") or doc_project.get(p["document_id"])
        if p["section_id"] is not None:
            pending_by_key[(project, p["section_id"],
                            p["field"])].append(p)
            continue
        targets = doc_sections.get(p["document_id"], [None])
        for sec in targets:
            pending_by_key[(project, sec, p["field"])].append(p)
        # Also retain the unresolved key so unpaired material is visible
        # even when no concrete section scope exists.
        pending_by_key[(project, None, p["field"])].append(p)

    # Every field of every known project/section scope is addressable:
    # a field the corpus never mentions reads as absent explicitly.
    scope_keys: set[tuple] = set()
    for d in store.all_documents():
        sections = [r["section_id"] for r in store.conn.execute(
            "SELECT section_id FROM doc_sections WHERE document_id=? "
            "ORDER BY section_id", (d["document_id"],))] or [None]
        for sec in sections:
            for field in FIELDS:
                scope_keys.add((d["project_id"], sec, field))

    keys = (set(groups) | set(all_sections) | set(pending_by_key)
            | scope_keys)
    facts: list[dict] = []
    ever_by_key: dict[tuple, bool] = defaultdict(bool)
    for r in eligible:
        ever_by_key[(r["project_id"], r["section_id"], r["field"])] = True
    for key in sorted(keys, key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
        project_id, sec, field = key
        if section_id is not None and sec != section_id:
            continue
        rows = groups.get(key, [])
        pendings = pending_by_key.get(key, [])
        provenance = [_assertion_payload(r, store,
                                         collection_dir="." if include_source_paths
                                         else None)
                      for r in rows]
        if rows:
            distinct = {(r["normalized"], r["currency"]) for r in rows}
            if len(distinct) == 1:
                only = rows[0]
                facts.append({
                    "project_id": project_id, "section_id": sec,
                    "field": field, "status": S_VALUE,
                    "value": {"normalized": only["normalized"],
                              "currency": only["currency"],
                              "raw_value": only["raw_value"]},
                    "candidates": [], "pending": pendings,
                    "provenance": provenance})
            else:
                facts.append({
                    "project_id": project_id, "section_id": sec,
                    "field": field, "status": S_CONFLICT, "value": None,
                    "candidates": provenance, "pending": pendings,
                    "provenance": []})
            continue
        # No open confirmed assertion for this exact scope. Pending
        # material takes precedence over absent; withdrawn wins over both
        # when the scope previously held a confirmed assertion.
        if ever_by_key[key]:
            status = S_WITHDRAWN
        elif pendings:
            status = S_PENDING
        else:
            status = S_ABSENT
        facts.append({
            "project_id": project_id, "section_id": sec, "field": field,
            "status": status, "value": None, "candidates": [],
            "pending": pendings, "provenance": []})

    # Coalesce duplicate scope rows (the section=None unresolved key is
    # emitted separately from concrete section keys; concrete scopes must
    # appear exactly once with a single resolved status).
    concrete: dict[tuple, dict] = {}
    unresolved: list[dict] = []
    priority = {S_VALUE: 4, S_CONFLICT: 4, S_WITHDRAWN: 3, S_PENDING: 2,
                S_ABSENT: 1}
    for f in facts:
        k = (f["project_id"], f["section_id"], f["field"])
        if f["section_id"] is None and f["status"] == S_PENDING:
            unresolved.append(f)
            continue
        if k not in concrete or priority[f["status"]] > \
                priority[concrete[k]["status"]]:
            concrete[k] = f
        else:
            existing = concrete[k]
            existing["pending"] = existing.get("pending", []) + f.get(
                "pending", [])
    out = list(concrete.values())
    out.extend(unresolved)
    out.sort(key=lambda f: (str(f["project_id"]), str(f["section_id"]),
                            f["field"]))
    return out


def document_statements(store, document_id: str) -> dict:
    """Everything one document itself asserted — unaffected by relations."""
    doc = store.get_document(document_id)
    if doc is None:
        raise KeyError(document_id)
    assertions = [_assertion_payload(r, store)
                  for r in store.conn.execute(
                      "SELECT * FROM assertions WHERE document_id=? "
                      "ORDER BY field, assertion_id", (document_id,))]
    unparsed = []
    for u in store.iter_unparsed():
        if u["document_id"] == document_id:
            unparsed.append({
                "unparsed_id": u["unparsed_id"], "field": u["field"],
                "reason": u["reason"], "raw_text": u["raw_text"],
                "span": json.loads(u["span_json"])})
    return {
        "document_id": document_id,
        "notice_id": doc["notice_id"], "site": doc["site"],
        "project_id": doc["project_id"], "published_at": doc["published_at"],
        "assertions": assertions, "unparsed": unparsed,
        "same_notice_links": [dict(r) for r in store.conn.execute(
            "SELECT site, document_id FROM same_notice WHERE notice_id=? "
            "AND document_id != ? ORDER BY site",
            (doc["notice_id"], document_id))]}


def timeline(store, section_id: Optional[str] = None) -> list[dict]:
    """Chronological event stream: assertions and the closures on them."""
    events: list[dict] = []
    for r in store.iter_assertions():
        if section_id is not None and r["section_id"] != section_id:
            continue
        events.append({
            "at": r["published_at"], "kind": "assertion",
            "status": r["status"],
            "event": _assertion_payload(r, store)})
    for c in store.iter_closures():
        target = store.get_assertion(c["target_assertion_id"])
        if target is None:
            continue
        if section_id is not None and target["section_id"] != section_id:
            continue
        events.append({
            "at": c["published_at"], "kind": "closure",
            "event": {
                "closure_id": c["closure_id"], "relation_id": c["relation_id"],
                "acting_document_id": c["acting_document_id"],
                "verb": c["verb"], "field": c["field"],
                "target_assertion_id": c["target_assertion_id"],
                "target_document_id": target["document_id"],
                "target_span": _row_span(target)}})
    events.sort(key=lambda e: (e["at"], e["kind"], json.dumps(
        e["event"], ensure_ascii=False, sort_keys=True)))
    return events


def relation_audit(store) -> list[dict]:
    out = []
    for r in store.all_relations():
        out.append({
            "relation_id": r["relation_id"],
            "acting_document_id": r["acting_document_id"],
            "verb": r["verb"], "target_document_id": r["target_document_id"],
            "target_sections": json.loads(r["target_sections_json"]),
            "fields": json.loads(r["fields_json"]),
            "status": r["status"], "detail": r["detail"],
            "published_at": r["published_at"],
            "quote": json.loads(r["quote_json"]) if r["quote_json"] else []})
    return out
