#!/usr/bin/env python3
"""Build committable fixtures for the announcement-impact analysis.

Everything here is constructed independently of the impact code under
test:

* a tiny isolated announcement corpus (two sections A/B of project
  PRJ-X) is run through the *existing* pipeline to produce a normal
  bidfact result store ``S0`` — the store the mini fund ledger closes
  against;
* the later-result stores (``S1`` … ``S5`` plus an empty store) are
  built as **independent** SQLite stores: the S0 file is copied and
  mutated with plain SQL following the documented bidfact storage
  schema and :func:`bidfact.util.stable_id` id rules, so every store
  keeps S0's result id while describing different later outcomes
  (replace, contesting supplement, retract, missing assertion, wrong
  result id);
* one isolated mini ledger (BOOK-IMP) is closed with ordinary fund
  packages: a payment split over A/B and a later partial refund.

Hand-computed expectations live in ``impact_expected.json`` and are
typed from the specification, never derived from the implementation.

Run from the repository root::

    python3 -I tests/make_impact_fixtures.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bidfact.pipeline import run_pipeline  # noqa: E402
from bidfact.util import stable_id  # noqa: E402

FIX = ROOT / "fixtures" / "impact"
RULE_VERSION = "bidfact-rules-1.0.0"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------- base corpus
def base_text() -> str:
    return (
        "某采购项目中标公告（基线）\n"
        "发布时间：2024-01-05T09:00:00\n"
        "标段编号：A\n中标单位：甲公司\n中标金额：人民币 100 元\n"
        "标段编号：B\n中标单位：乙公司\n中标金额：人民币 50 元\n"
        "标段编号：C\n中标单位：甲公司\n中标金额：人民币 7 元\n")


def correction_text(unit_a: str = "甲更正公司", unit_b: str | None = None
                    ) -> str:
    parts = [
        "某采购项目中标公告（更正）\n",
        "发布时间：2024-02-10T09:00:00\n",
        f"标段编号：A\n中标单位：{unit_a}\n中标金额：人民币 100 元\n"]
    if unit_b is not None:
        parts.append(f"标段编号：B\n中标单位：{unit_b}\n"
                     "中标金额：人民币 50 元\n")
    return "".join(parts)


def build_base_store() -> dict:
    """Run the normal pipeline; return assertion locators."""
    col = FIX / "corpus_base"
    if col.exists():
        shutil.rmtree(col)
    (col / "pages").mkdir(parents=True)
    payload = {"document_id": "docBase",
               "pages": [{"page_no": 1, "text": base_text()}]}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    (col / "pages" / "docBase.json").write_bytes(raw)
    write_json(col / "manifest.json", {
        "collection": "impact-base",
        "documents": [{
            "document_id": "docBase", "notice_id": "N-BASE", "site": "site1",
            "project_id": "PRJ-X", "section_ids": ["A", "B", "C"],
            "published_at": "2024-01-05T09:00:00",
            "source": {"file": "pages/docBase.json",
                       "sha256": sha256_bytes(raw)}}]})
    out = run_pipeline(col, FIX / "results", None, force_rebuild=True)
    result_id = out["result_id"]
    conn = sqlite3.connect(Path(out["path"]) / "store.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        ids = {}
        for r in conn.execute(
                "SELECT * FROM assertions WHERE field='result_unit'"):
            ids[r["section_id"]] = {
                "assertion_id": r["assertion_id"],
                "document_id": r["document_id"],
                "raw_value": r["raw_value"],
                "published_at": r["published_at"],
                "span_json": r["span_json"],
                "project_id": r["project_id"]}
    finally:
        conn.close()
    return {"result_id": result_id,
            "result_dir": f"results/{result_id}", "assertions": ids}


# ------------------------------------------------------------ variant stores
def _open_rw(store_file: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(store_file))
    conn.row_factory = sqlite3.Row
    return conn


def _add_document(conn, doc_id: str, notice_id: str, project_id: str,
                  sections: list[str], published_at: str) -> None:
    manifest = {"document_id": doc_id, "notice_id": notice_id,
                "site": "site1", "project_id": project_id,
                "section_ids": sections, "published_at": published_at}
    conn.execute(
        "INSERT INTO documents(document_id, notice_id, site, project_id, "
        "published_at, source_digest, raw_json) VALUES(?,?,?,?,?,?,?)",
        (doc_id, notice_id, "site1", project_id, published_at,
         hashlib.sha256(doc_id.encode()).hexdigest(),
         json.dumps(manifest, ensure_ascii=False, sort_keys=True)))
    for sec in sections:
        conn.execute(
            "INSERT OR IGNORE INTO doc_sections(document_id, section_id) "
            "VALUES(?,?)", (doc_id, sec))


def _add_unit_assertion(conn, doc_id: str, project_id: str, section: str,
                        raw: str, published_at: str, page_no: int,
                        start: int, end: int) -> str:
    aid = stable_id("ast", doc_id, "result_unit", section, page_no,
                    start, end, raw)
    span = {"page_no": page_no, "start": start, "end": end, "text": raw,
            "source_kind": "page", "table_id": None, "row": None,
            "col": None, "header": None}
    conn.execute(
        "INSERT OR IGNORE INTO assertions(assertion_id, document_id, "
        "project_id, section_id, field, normalized, raw_value, currency, "
        "header, scope_basis, status, pending_reason, published_at, "
        "rule_version, span_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, doc_id, project_id, section, "result_unit", raw, raw, None,
         None, f"section numbering {section} in same block", "confirmed",
         None, published_at, RULE_VERSION,
         json.dumps(span, ensure_ascii=False, sort_keys=True)))
    return aid


def _add_closure(conn, relation_id: str, acting_doc: str, target_aid: str,
                 verb: str, published_at: str) -> str:
    cid = stable_id("clo", relation_id, target_aid)
    conn.execute(
        "INSERT OR IGNORE INTO closures(closure_id, relation_id, "
        "acting_document_id, target_assertion_id, verb, field, "
        "published_at) VALUES(?,?,?,?,?,?,?)",
        (cid, relation_id, acting_doc, target_aid, verb, "result_unit",
         published_at))
    conn.execute(
        "INSERT INTO relations(relation_id, acting_document_id, verb, "
        "target_document_id, target_sections_json, fields_json, status, "
        "detail, published_at, quote_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (relation_id, acting_doc, verb, "docBase",
         json.dumps(["A"] if verb != "retract-b" else ["B"]),
         json.dumps(["result_unit"]), "confirmed",
         f"applied {verb}; target_assertions=[{target_aid}]",
         published_at, None))
    return cid


def _clone(base_store: Path, name: str) -> Path:
    out_dir = FIX / "results" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "store.sqlite3"
    shutil.copyfile(base_store, target)
    return target


def build_variants(base: dict) -> dict:
    base_store = FIX / base["result_dir"] / "store.sqlite3"
    aid_a = base["assertions"]["A"]["assertion_id"]
    aid_b = base["assertions"]["B"]["assertion_id"]
    locators = {"S0": str(base_store), "A": aid_a, "B": aid_b,
                "C": base["assertions"]["C"]["assertion_id"],
                "new": {}}

    # S1: docFix1 replaces A's award unit (B's assertion stays open and
    # equal).  A's relied assertion is closed; B is unchanged.
    p = _clone(base_store, "S1")
    conn = _open_rw(p)
    try:
        _add_document(conn, "docFix1", "N-FIX-1", "PRJ-X", ["A", "B"],
                      "2024-02-10T09:00:00")
        text = correction_text()
        start = text.index("甲更正公司")
        new_a = _add_unit_assertion(conn, "docFix1", "PRJ-X", "A",
                                    "甲更正公司", "2024-02-10T09:00:00", 1,
                                    start, start + len("甲更正公司"))
        _add_closure(conn, "rel-replace-a", "docFix1", aid_a, "replace",
                     "2024-02-10T09:00:00")
        conn.commit()
        locators["new"]["S1_A"] = new_a
    finally:
        conn.close()
    locators["S1"] = str(p)

    # S2: same replace, plus docFix2 supplements a *different* still-open
    # unit value for B (no closure on B): equal-published scope with two
    # distinct values -> contested, and the newer value must not win.
    p = _clone(base_store, "S2")
    conn = _open_rw(p)
    try:
        _add_document(conn, "docFix1", "N-FIX-1", "PRJ-X", ["A", "B"],
                      "2024-02-10T09:00:00")
        text1 = correction_text()
        start = text1.index("甲更正公司")
        new_a = _add_unit_assertion(conn, "docFix1", "PRJ-X", "A",
                                    "甲更正公司", "2024-02-10T09:00:00", 1,
                                    start, start + len("甲更正公司"))
        _add_closure(conn, "rel-replace-a", "docFix1", aid_a, "replace",
                     "2024-02-10T09:00:00")
        _add_document(conn, "docFix2", "N-FIX-2", "PRJ-X", ["B"],
                      "2024-03-01T09:00:00")
        text2 = ("某采购项目中标公告（补充）\n"
                 "发布时间：2024-03-01T09:00:00\n"
                 "标段编号：B\n中标单位：乙补充公司\n")
        start = text2.index("乙补充公司")
        new_b = _add_unit_assertion(conn, "docFix2", "PRJ-X", "B",
                                    "乙补充公司", "2024-03-01T09:00:00", 1,
                                    start, start + len("乙补充公司"))
        conn.commit()
        locators["new"]["S2_B_supplement"] = new_b
    finally:
        conn.close()
    locators["S2"] = str(p)

    # S3: after replacing A, docFix3 retracts the *new* A assertion.  The
    # original A assertion stays closed — retraction must not revive it.
    p = _clone(base_store, "S3")
    conn = _open_rw(p)
    try:
        _add_document(conn, "docFix1", "N-FIX-1", "PRJ-X", ["A", "B"],
                      "2024-02-10T09:00:00")
        text1 = correction_text()
        start = text1.index("甲更正公司")
        new_a = _add_unit_assertion(conn, "docFix1", "PRJ-X", "A",
                                    "甲更正公司", "2024-02-10T09:00:00", 1,
                                    start, start + len("甲更正公司"))
        _add_closure(conn, "rel-replace-a", "docFix1", aid_a, "replace",
                     "2024-02-10T09:00:00")
        _add_document(conn, "docFix3", "N-FIX-3", "PRJ-X", ["A"],
                      "2024-04-01T09:00:00")
        _add_closure(conn, "rel-retract-new-a", "docFix3", new_a, "retract",
                     "2024-04-01T09:00:00")
        conn.commit()
    finally:
        conn.close()
    locators["S3"] = str(p)

    # S4: the relied B assertion is simply absent from the later store
    # (delete it); A is present and untouched.
    p = _clone(base_store, "S4")
    conn = _open_rw(p)
    try:
        conn.execute("DELETE FROM assertions WHERE assertion_id=?",
                     (aid_b,))
        conn.commit()
    finally:
        conn.close()
    locators["S4"] = str(p)

    # S5: same data as S1 but a different result id in meta -> every line
    # is unverifiable with result_id_mismatch.
    p = _clone(base_store, "S5")
    conn = _open_rw(p)
    try:
        _add_document(conn, "docFix1", "N-FIX-1", "PRJ-X", ["A", "B"],
                      "2024-02-10T09:00:00")
        text = correction_text()
        start = text.index("甲更正公司")
        _add_unit_assertion(conn, "docFix1", "PRJ-X", "A", "甲更正公司",
                            "2024-02-10T09:00:00", 1, start,
                            start + len("甲更正公司"))
        _add_closure(conn, "rel-replace-a", "docFix1", aid_a, "replace",
                     "2024-02-10T09:00:00")
        conn.execute("UPDATE meta SET value=? WHERE key='result_id'",
                     ("Rwrong0000000000",))
        conn.commit()
    finally:
        conn.close()
    locators["S5"] = str(p)

    # S6: a genuinely empty bidfact-shaped store (schema + result id only).
    empty_dir = FIX / "results" / "S6"
    empty_dir.mkdir(parents=True, exist_ok=True)
    empty_store = empty_dir / "store.sqlite3"
    if empty_store.exists():
        empty_store.unlink()
    conn = _open_rw(empty_store)
    try:
        conn.executescript(_EMPTY_SCHEMA)
        conn.execute("INSERT INTO meta(key, value) VALUES('result_id', ?)",
                     (base["result_id"],))
        conn.execute("INSERT INTO meta(key, value) VALUES('rule_version', ?)",
                     (RULE_VERSION,))
        conn.commit()
    finally:
        conn.close()
    locators["S6"] = str(empty_store)

    # S7: B's relied assertion still exists and is confirmed, but it is
    # rebound to a different section in the later store -> scope mismatch
    # is unverifiable, never silently treated as the same evidence.
    p = _clone(base_store, "S7")
    conn = _open_rw(p)
    try:
        conn.execute(
            "UPDATE assertions SET section_id='Z' WHERE assertion_id=?",
            (aid_b,))
        conn.commit()
    finally:
        conn.close()
    locators["S7"] = str(p)

    # S8: B's relied assertion exists but is no longer confirmed (pending)
    # in the later store -> unverifiable, never treated as a live fact.
    p = _clone(base_store, "S8")
    conn = _open_rw(p)
    try:
        conn.execute(
            "UPDATE assertions SET status='pending', "
            "pending_reason='scope_not_unique' WHERE assertion_id=?",
            (aid_b,))
        conn.commit()
    finally:
        conn.close()
    locators["S8"] = str(p)
    return locators


_EMPTY_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files (name TEXT PRIMARY KEY, digest TEXT NOT NULL);
CREATE TABLE documents (
    document_id TEXT PRIMARY KEY, notice_id TEXT NOT NULL, site TEXT NOT NULL,
    project_id TEXT, published_at TEXT NOT NULL, source_digest TEXT NOT NULL,
    raw_json TEXT NOT NULL);
CREATE TABLE doc_sections (
    document_id TEXT NOT NULL, section_id TEXT NOT NULL,
    PRIMARY KEY (document_id, section_id));
CREATE TABLE same_notice (
    notice_id TEXT NOT NULL, document_id TEXT NOT NULL, site TEXT NOT NULL,
    PRIMARY KEY (notice_id, document_id));
CREATE TABLE assertions (
    assertion_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
    project_id TEXT, section_id TEXT, field TEXT NOT NULL,
    normalized TEXT, raw_value TEXT NOT NULL, currency TEXT,
    header TEXT, scope_basis TEXT NOT NULL, status TEXT NOT NULL,
    pending_reason TEXT, published_at TEXT NOT NULL, rule_version TEXT NOT NULL,
    span_json TEXT NOT NULL);
CREATE TABLE closures (
    closure_id TEXT PRIMARY KEY, relation_id TEXT NOT NULL,
    acting_document_id TEXT NOT NULL, target_assertion_id TEXT NOT NULL,
    verb TEXT NOT NULL, field TEXT NOT NULL, published_at TEXT NOT NULL,
    UNIQUE (relation_id, target_assertion_id));
CREATE TABLE relations (
    relation_id TEXT PRIMARY KEY, acting_document_id TEXT NOT NULL,
    verb TEXT NOT NULL, target_document_id TEXT,
    target_sections_json TEXT NOT NULL, fields_json TEXT NOT NULL,
    status TEXT NOT NULL, detail TEXT NOT NULL, published_at TEXT NOT NULL,
    quote_json TEXT);
CREATE TABLE unparsed (
    unparsed_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
    field TEXT, reason TEXT NOT NULL, raw_text TEXT NOT NULL,
    span_json TEXT NOT NULL, published_at TEXT NOT NULL,
    rule_version TEXT NOT NULL);
CREATE TABLE material_conflicts (
    document_id TEXT NOT NULL, digest_a TEXT NOT NULL, digest_b TEXT NOT NULL,
    detail TEXT NOT NULL);
CREATE TABLE ingestion_issues (
    issue_id TEXT PRIMARY KEY, document_id TEXT, kind TEXT NOT NULL,
    detail TEXT NOT NULL, span_json TEXT);
"""


# ------------------------------------------------------------- mini ledger
def build_ledger_packages(base: dict) -> None:
    """BOOK-IMP mini ledger.

    * PX (6分, January) split equally over A/B -> A=3, B=3.  B has the
      larger section key so the later 2分 partial refund consumes B
      first (B 3 -> 1), leaving A untouched.
    * PY (4分, January) pays section A and section C; both name unit
      甲公司 (C exists so the "same value, different section" case is
      covered).  The A line reuses A's assertion, so one assertion is
      relied on by two payments (PX-A and PY-A): every payment keeps its
      own amount while status summaries must not double-count lines.
    """
    ev_a = base["assertions"]["A"]
    ev_b = base["assertions"]["B"]
    ev_c = base["assertions"]["C"]
    rid = base["result_id"]
    ann = "../../" + base["result_dir"]

    jan = FIX / "ledger_packages" / "2024-01"
    write_jsonl(jan / "bank_flows.jsonl", [
        {"kind": "bank_flow", "ledger_id": "BOOK-IMP", "account_id": "ACC-X",
         "bank_flow_id": "FX1", "month": "2024-01", "currency": "CNY",
         "direction": "outflow", "amount_minor": 6},
        {"kind": "bank_flow", "ledger_id": "BOOK-IMP", "account_id": "ACC-X",
         "bank_flow_id": "FX3", "month": "2024-01", "currency": "CNY",
         "direction": "outflow", "amount_minor": 4}])
    write_jsonl(jan / "vouchers.jsonl", [
        {"kind": "voucher", "ledger_id": "BOOK-IMP", "voucher_id": "PX",
         "month": "2024-01", "currency": "CNY", "direction": "outflow",
         "amount_minor": 6, "voucher_type": "payment"},
        {"kind": "voucher", "ledger_id": "BOOK-IMP", "voucher_id": "PY",
         "month": "2024-01", "currency": "CNY", "direction": "outflow",
         "amount_minor": 4, "voucher_type": "payment"}])
    write_jsonl(jan / "clearing.jsonl", [
        {"kind": "clearing", "ledger_id": "BOOK-IMP", "account_id": "ACC-X",
         "bank_flow_id": "FX1", "voucher_id": "PX", "amount_minor": 6},
        {"kind": "clearing", "ledger_id": "BOOK-IMP", "account_id": "ACC-X",
         "bank_flow_id": "FX3", "voucher_id": "PY", "amount_minor": 4}])
    write_jsonl(jan / "allocation_plan.jsonl", [
        {"kind": "allocation_plan", "ledger_id": "BOOK-IMP",
         "plan_id": "PLAN-PX", "voucher_id": "PX", "project_id": "PRJ-X",
         "section_id": "A", "weight": 1, "result_id": rid,
         "result_unit_assertion_id": ev_a["assertion_id"]},
        {"kind": "allocation_plan", "ledger_id": "BOOK-IMP",
         "plan_id": "PLAN-PX", "voucher_id": "PX", "project_id": "PRJ-X",
         "section_id": "B", "weight": 1, "result_id": rid,
         "result_unit_assertion_id": ev_b["assertion_id"]},
        {"kind": "allocation_plan", "ledger_id": "BOOK-IMP",
         "plan_id": "PLAN-PY", "voucher_id": "PY", "project_id": "PRJ-X",
         "section_id": "A", "weight": 1, "result_id": rid,
         "result_unit_assertion_id": ev_a["assertion_id"]},
        {"kind": "allocation_plan", "ledger_id": "BOOK-IMP",
         "plan_id": "PLAN-PY", "voucher_id": "PY", "project_id": "PRJ-X",
         "section_id": "C", "weight": 1, "result_id": rid,
         "result_unit_assertion_id": ev_c["assertion_id"]}])
    write_jsonl(jan / "control_bank.jsonl", [
        {"kind": "control", "side": "bank", "account_id": "ACC-X",
         "currency": "CNY", "direction": "outflow", "count": 2,
         "amount_minor": 10}])
    write_jsonl(jan / "control_voucher.jsonl", [
        {"kind": "control", "side": "voucher", "currency": "CNY",
         "direction": "outflow", "count": 2, "amount_minor": 10}])
    write_json(jan / "manifest.json", {
        "ledger_id": "BOOK-IMP", "month": "2024-01",
        "announcement_result_dir": ann,
        "files": {"bank_flow": "bank_flows.jsonl",
                  "voucher": "vouchers.jsonl",
                  "clearing": "clearing.jsonl",
                  "allocation_plan": "allocation_plan.jsonl"},
        "control_files": {"bank": "control_bank.jsonl",
                          "voucher": "control_voucher.jsonl"}})

    # February: a 2分 partial refund of PX.  B has the larger section key,
    # so the refund waterfall consumes B first: B 3 -> 1, A untouched.
    feb = FIX / "ledger_packages" / "2024-02"
    write_jsonl(feb / "bank_flows.jsonl", [
        {"kind": "bank_flow", "ledger_id": "BOOK-IMP", "account_id": "ACC-X",
         "bank_flow_id": "FX2", "month": "2024-02", "currency": "CNY",
         "direction": "inflow", "amount_minor": 2}])
    write_jsonl(feb / "vouchers.jsonl", [
        {"kind": "voucher", "ledger_id": "BOOK-IMP", "voucher_id": "RX1",
         "month": "2024-02", "currency": "CNY", "direction": "inflow",
         "amount_minor": 2, "voucher_type": "refund",
         "payment_voucher_id": "PX", "rate": "1/3"}])
    write_jsonl(feb / "clearing.jsonl", [
        {"kind": "clearing", "ledger_id": "BOOK-IMP", "account_id": "ACC-X",
         "bank_flow_id": "FX2", "voucher_id": "RX1", "amount_minor": 2}])
    write_jsonl(feb / "allocation_plan.jsonl", [])
    write_jsonl(feb / "control_bank.jsonl", [
        {"kind": "control", "side": "bank", "account_id": "ACC-X",
         "currency": "CNY", "direction": "inflow", "count": 1,
         "amount_minor": 2}])
    write_jsonl(feb / "control_voucher.jsonl", [
        {"kind": "control", "side": "voucher", "currency": "CNY",
         "direction": "inflow", "count": 1, "amount_minor": 2}])
    write_json(feb / "manifest.json", {
        "ledger_id": "BOOK-IMP", "month": "2024-02",
        "announcement_result_dir": ann,
        "files": {"bank_flow": "bank_flows.jsonl",
                  "voucher": "vouchers.jsonl",
                  "clearing": "clearing.jsonl",
                  "allocation_plan": "allocation_plan.jsonl"},
        "control_files": {"bank": "control_bank.jsonl",
                          "voucher": "control_voucher.jsonl"}})


def write_expected(base: dict, locators: dict) -> None:
    """Hand-computed expectations (typed, not produced by the code)."""
    write_json(FIX / "impact_expected.json", {
        "_note": "Hand-computed from the impact specification. PX=6 split "
                 "equally A=3/B=3; the 2分 refund hits B first (larger key), "
                 "so after 2024-02: PX-A refunded 0 outstanding 3; PX-B "
                 "refunded 2 outstanding 1. PY=4 split equally A=2/C=2 with "
                 "no refund; PY-A reuses A's assertion (one assertion, two "
                 "payments), and C carries the same unit text 甲公司 as A "
                 "but a different section.",
        "ledger_id": "BOOK-IMP",
        "result_id": base["result_id"],
        "cutoff_late": "2024-05-01T00:00:00",
        "cutoff_early": "2024-01-20T00:00:00",
        "replace_published_at": "2024-02-10T09:00:00",
        "supplement_published_at": "2024-03-01T09:00:00",
        "retract_published_at": "2024-04-01T09:00:00",
        "payment_lines": [
            {"voucher_id": "PX", "section_id": "A", "rank": 0,
             "amount_minor": 3, "refunded_minor": 0, "outstanding_minor": 3,
             "assertion_id": locators["A"]},
            {"voucher_id": "PX", "section_id": "B", "rank": 1,
             "amount_minor": 3, "refunded_minor": 2, "outstanding_minor": 1,
             "assertion_id": locators["B"]},
            {"voucher_id": "PY", "section_id": "A", "rank": 0,
             "amount_minor": 2, "refunded_minor": 0, "outstanding_minor": 2,
             "assertion_id": locators["A"]},
            {"voucher_id": "PY", "section_id": "C", "rank": 1,
             "amount_minor": 2, "refunded_minor": 0, "outstanding_minor": 2,
             "assertion_id": locators["C"]}],
        "expected_status": {
            "S1": {"PX": {"A": "closed", "B": "unchanged"},
                   "PY": {"A": "closed", "C": "unchanged"}},
            "S2": {"PX": {"A": "closed", "B": "contested"},
                   "PY": {"A": "closed", "C": "unchanged"}},
            "S3": {"PX": {"A": "closed", "B": "unchanged"},
                   "PY": {"A": "closed", "C": "unchanged"}},
            "S4": {"PX": {"A": "unchanged", "B": "unverifiable"},
                   "PY": {"A": "unchanged", "C": "unchanged"}},
            "S5": {"PX": {"A": "unverifiable", "B": "unverifiable"},
                   "PY": {"A": "unverifiable", "C": "unverifiable"}},
            "S6": {"PX": {"A": "unverifiable", "B": "unverifiable"},
                   "PY": {"A": "unverifiable", "C": "unverifiable"}},
            "S7": {"PX": {"A": "unchanged", "B": "unverifiable"},
                   "PY": {"A": "unchanged", "C": "unchanged"}},
            "S8": {"PX": {"A": "unchanged", "B": "unverifiable"},
                   "PY": {"A": "unchanged", "C": "unchanged"}}},
        "expected_reasons": {
            "S4": {"B": "assertion_missing_in_later_store"},
            "S5": {"all": "result_id_mismatch"},
            "S6": {"all": "assertion_missing_in_later_store"},
            "S7": {"B": "assertion_scope_mismatch"},
            "S8": {"B": "assertion_not_confirmed"}},
        "s1_closed_values": {
            "A": {"verb": "replace", "relation_id": "rel-replace-a",
                  "acting_document_id": "docFix1"}},
        "s2_contest_values": ["乙公司", "乙补充公司"],
        "same_unit_different_sections": {"A": "甲公司", "C": "甲公司"},
        "stores": {k: locators[k] for k in
                   ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7",
                    "S8")}})


def main() -> None:
    if FIX.exists():
        shutil.rmtree(FIX)
    FIX.mkdir(parents=True)
    base = build_base_store()
    locators = build_variants(base)
    build_ledger_packages(base)
    write_expected(base, locators)
    print("impact fixtures written to", FIX)


if __name__ == "__main__":
    main()
