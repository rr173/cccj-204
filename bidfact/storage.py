"""SQLite-backed result store.

Design constraints from the specification:

* page texts and per-evidence paths are *not* held resident; only
  compact assertions, closures and verified span references live here.
* assertion ids are deterministic primary keys, so re-running the same
  rule version against the same corpus cannot create duplicate facts.
* every result number gets its own store file; old results are never
  rewritten when extraction rules change.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .models import Assertion, Closure, Unparsed

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    name TEXT PRIMARY KEY, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY, notice_id TEXT NOT NULL, site TEXT NOT NULL,
    project_id TEXT, published_at TEXT NOT NULL, source_digest TEXT NOT NULL,
    raw_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS doc_sections (
    document_id TEXT NOT NULL, section_id TEXT NOT NULL,
    PRIMARY KEY (document_id, section_id));
CREATE TABLE IF NOT EXISTS same_notice (
    notice_id TEXT NOT NULL, document_id TEXT NOT NULL, site TEXT NOT NULL,
    PRIMARY KEY (notice_id, document_id));
CREATE TABLE IF NOT EXISTS assertions (
    assertion_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
    project_id TEXT, section_id TEXT, field TEXT NOT NULL,
    normalized TEXT, raw_value TEXT NOT NULL, currency TEXT,
    header TEXT, scope_basis TEXT NOT NULL, status TEXT NOT NULL,
    pending_reason TEXT, published_at TEXT NOT NULL, rule_version TEXT NOT NULL,
    span_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_assert_scope
    ON assertions(section_id, field, status);
CREATE INDEX IF NOT EXISTS idx_assert_doc ON assertions(document_id);
CREATE TABLE IF NOT EXISTS closures (
    closure_id TEXT PRIMARY KEY, relation_id TEXT NOT NULL,
    acting_document_id TEXT NOT NULL, target_assertion_id TEXT NOT NULL,
    verb TEXT NOT NULL, field TEXT NOT NULL, published_at TEXT NOT NULL,
    UNIQUE (relation_id, target_assertion_id));
CREATE TABLE IF NOT EXISTS relations (
    relation_id TEXT PRIMARY KEY, acting_document_id TEXT NOT NULL,
    verb TEXT NOT NULL, target_document_id TEXT,
    target_sections_json TEXT NOT NULL, fields_json TEXT NOT NULL,
    status TEXT NOT NULL, detail TEXT NOT NULL, published_at TEXT NOT NULL,
    quote_json TEXT);
CREATE TABLE IF NOT EXISTS unparsed (
    unparsed_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
    field TEXT, reason TEXT NOT NULL, raw_text TEXT NOT NULL,
    span_json TEXT NOT NULL, published_at TEXT NOT NULL,
    rule_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS material_conflicts (
    document_id TEXT NOT NULL, digest_a TEXT NOT NULL, digest_b TEXT NOT NULL,
    detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ingestion_issues (
    issue_id TEXT PRIMARY KEY, document_id TEXT, kind TEXT NOT NULL,
    detail TEXT NOT NULL, span_json TEXT);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- meta / files -------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def add_file(self, name: str, digest: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO files(name, digest) VALUES(?, ?)",
            (name, digest))

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    # ---- documents ----------------------------------------------------
    def add_document(self, doc: dict, source_digest: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO documents(document_id, notice_id, site, "
            "project_id, published_at, source_digest, raw_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (doc["document_id"], doc["notice_id"], doc["site"],
             doc.get("project_id"), doc["published_at"], source_digest,
             json.dumps(doc, ensure_ascii=False, sort_keys=True)))
        for sec in doc.get("section_ids", []):
            self.conn.execute(
                "INSERT OR IGNORE INTO doc_sections(document_id, section_id) "
                "VALUES(?, ?)", (doc["document_id"], sec))

    def add_same_notice(self, notice_id: str, document_id: str, site: str
                        ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO same_notice(notice_id, document_id, site) "
            "VALUES(?,?,?)", (notice_id, document_id, site))

    def get_document(self, document_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM documents WHERE document_id=?",
            (document_id,)).fetchone()

    def all_documents(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM documents ORDER BY published_at, document_id"))

    def add_material_conflict(self, document_id: str, a: str, b: str,
                              detail: str) -> None:
        self.conn.execute(
            "INSERT INTO material_conflicts(document_id, digest_a, digest_b, "
            "detail) VALUES(?,?,?,?)", (document_id, a, b, detail))

    def add_issue(self, issue_id: str, document_id: Optional[str], kind: str,
                  detail: str, span: Optional[dict] = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO ingestion_issues(issue_id, document_id, "
            "kind, detail, span_json) VALUES(?,?,?,?,?)",
            (issue_id, document_id, kind, detail,
             json.dumps(span, ensure_ascii=False) if span else None))

    # ---- assertions ---------------------------------------------------
    def add_assertion(self, a: Assertion) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO assertions(assertion_id, document_id, "
            "project_id, section_id, field, normalized, raw_value, currency, "
            "header, scope_basis, status, pending_reason, published_at, "
            "rule_version, span_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a.assertion_id, a.document_id, a.project_id, a.section_id,
             a.field, a.normalized, a.raw_value, a.currency, a.header,
             a.scope_basis, a.status, a.pending_reason, a.published_at,
             a.rule_version,
             json.dumps(asdict(a.span), ensure_ascii=False, sort_keys=True)))

    def iter_assertions(self, status: Optional[str] = None
                        ) -> Iterator[sqlite3.Row]:
        if status:
            yield from self.conn.execute(
                "SELECT * FROM assertions WHERE status=? "
                "ORDER BY published_at, assertion_id", (status,))
        else:
            yield from self.conn.execute(
                "SELECT * FROM assertions ORDER BY published_at, assertion_id")

    def get_assertion(self, assertion_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM assertions WHERE assertion_id=?",
            (assertion_id,)).fetchone()

    # ---- relations / closures -----------------------------------------
    def upsert_relation(self, rel: dict) -> None:
        self.conn.execute(
            "INSERT INTO relations(relation_id, acting_document_id, verb, "
            "target_document_id, target_sections_json, fields_json, status, "
            "detail, published_at, quote_json) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(relation_id) DO UPDATE SET status=excluded.status, "
            "detail=excluded.detail",
            (rel["relation_id"], rel["acting_document_id"], rel["verb"],
             rel.get("target_document_id"),
             json.dumps(rel.get("target_sections", []), ensure_ascii=False),
             json.dumps(rel.get("fields", []), ensure_ascii=False),
             rel["status"], rel["detail"], rel["published_at"],
             json.dumps(rel.get("quote"), ensure_ascii=False)
             if rel.get("quote") else None))

    def all_relations(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM relations ORDER BY published_at, relation_id"))

    def add_closure(self, c: Closure) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO closures(closure_id, relation_id, "
            "acting_document_id, target_assertion_id, verb, field, "
            "published_at) VALUES(?,?,?,?,?,?,?)",
            (c.closure_id, c.relation_id, c.acting_document_id,
             c.target_assertion_id, c.verb, c.field, c.published_at))

    def iter_closures(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute(
            "SELECT * FROM closures ORDER BY published_at, closure_id")

    # ---- unparsed -----------------------------------------------------
    def add_unparsed(self, u: Unparsed) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO unparsed(unparsed_id, document_id, field, "
            "reason, raw_text, span_json, published_at, rule_version) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (u.unparsed_id, u.document_id, u.field, u.reason, u.raw_text,
             json.dumps(asdict(u.span), ensure_ascii=False, sort_keys=True),
             u.published_at, u.rule_version))

    def iter_unparsed(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute(
            "SELECT * FROM unparsed ORDER BY document_id, unparsed_id")

    def commit(self) -> None:
        self.conn.commit()
