#!/usr/bin/env python3
"""Build the committable fund-close fixtures.

The *announcement* corpus is built with the existing pipeline and its
result store becomes read-only evidence.  The monthly finance packages
are written as JSON Lines with hand-computed expectations in
``expected.json`` next to each package — those expected amounts are
*typed into this file from the specification*, never computed by the
code under test.

Run from the repository root::

    python3 -I tests/make_fund_fixtures.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bidfact.pipeline import run_pipeline  # noqa: E402
from bidfact.storage import Store  # noqa: E402

FIX = ROOT / "fixtures" / "fund"


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


# --------------------------------------------------------------- announcement
def build_announcement() -> None:
    """Two notices naming A, B, G, L (project PRJ-A) and a (project prj-a).

    The lower-case project/section pair exists so tests can prove that
    identifiers are compared case-sensitively by Unicode code point.
    """
    col = FIX / "_announcement_corpus"
    if col.exists():
        shutil.rmtree(col)
    (col / "pages").mkdir(parents=True)
    text = (
        "某采购项目招标公告\n"
        "发布时间：2023-12-01T09:00:00\n"
        "标段编号：A\n中标单位：甲公司\n中标金额：人民币 100 元\n"
        "标段编号：B\n中标单位：乙公司\n中标金额：人民币 50 元\n"
        "标段编号：G\n中标单位：庚公司\n中标金额：人民币 10 元\n"
        "标段编号：L\n中标单位：己公司\n中标金额：人民币 1 元\n")
    payload = {"document_id": "docAnn1",
               "pages": [{"page_no": 1, "text": text}]}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    (col / "pages" / "docAnn1.json").write_bytes(data)
    manifest = {"collection": "fund-announcement", "documents": [{
        "document_id": "docAnn1", "notice_id": "N-ANN-1", "site": "site1",
        "project_id": "PRJ-A", "section_ids": ["A", "B", "G", "L"],
        "published_at": "2023-12-01T09:00:00",
        "source": {"file": "pages/docAnn1.json",
                   "sha256": sha256_bytes(data)}}]}
    lower_text = (
        "某采购项目招标公告（小写编号）\n"
        "发布时间：2023-12-02T09:00:00\n"
        "标段编号：a\n中标单位：甲小写公司\n中标金额：人民币 20 元\n")
    lower = {"document_id": "docAnn2",
             "pages": [{"page_no": 1, "text": lower_text}]}
    lower_data = json.dumps(lower, ensure_ascii=False).encode("utf-8")
    (col / "pages" / "docAnn2.json").write_bytes(lower_data)
    manifest["documents"].append({
        "document_id": "docAnn2", "notice_id": "N-ANN-2", "site": "site1",
        "project_id": "prj-a", "section_ids": ["a"],
        "published_at": "2023-12-02T09:00:00",
        "source": {"file": "pages/docAnn2.json",
                   "sha256": sha256_bytes(lower_data)}})
    write_json(col / "manifest.json", manifest)
    # Announcement result numbers are content-addressed; clear any older
    # run so only the store this fixture set points at is committed.
    shutil.rmtree(FIX / "announcement_results", ignore_errors=True)
    out = run_pipeline(col, FIX / "announcement_results", None,
                       force_rebuild=True)
    store = Store(Path(out["path"]) / "store.sqlite3")
    try:
        result_id = store.get_meta("result_id")
        evidence = {}
        for row in store.iter_assertions():
            if row["field"] != "result_unit":
                continue
            evidence[f"{row['project_id']}/{row['section_id']}"] = {
                "assertion_id": row["assertion_id"],
                "document_id": row["document_id"],
                "result_id": result_id,
                "unit_raw": row["raw_value"],
            }
    finally:
        store.close()
    write_json(FIX / "announcement_evidence.json", evidence)
    # Relative pointer used by every month manifest.
    write_json(FIX / "announcement_result_id.json",
               {"result_dir": f"announcement_results/{result_id}",
                "result_id": result_id})


def evidence() -> dict:
    return json.loads(
        (FIX / "announcement_evidence.json").read_text(encoding="utf-8"))


def ann_dir() -> str:
    rel = json.loads((FIX / "announcement_result_id.json").read_text(
        encoding="utf-8"))["result_dir"]
    return rel


# ------------------------------------------------------------------ month 1
def m1_plan(p, q, *, weights=None):
    """January plan: P equal A/B, Q all A."""
    ev = evidence()
    a = ev["PRJ-A/A"]
    b = ev["PRJ-A/B"]
    rows = []
    for proj, sec, weight in weights or [
            ("PRJ-A", "A", 1), ("PRJ-A", "B", 1)]:
        rows.append({
            "kind": "allocation_plan", "ledger_id": "BOOK-1",
            "plan_id": f"PLAN-{p}", "voucher_id": p, "project_id": proj,
            "section_id": sec, "weight": weight,
            "result_id": a["result_id"] if sec == "A" else b["result_id"],
            "result_unit_assertion_id": a["assertion_id"] if sec == "A"
            else b["assertion_id"]})
    rows.append({
        "kind": "allocation_plan", "ledger_id": "BOOK-1",
        "plan_id": f"PLAN-{q}", "voucher_id": q, "project_id": "PRJ-A",
        "section_id": "A", "weight": 1, "result_id": a["result_id"],
        "result_unit_assertion_id": a["assertion_id"]})
    return rows


def build_january() -> None:
    pkg = FIX / "january"
    if pkg.exists():
        shutil.rmtree(pkg)
    write_jsonl(pkg / "bank_flows.jsonl", [
        {"kind": "bank_flow", "ledger_id": "BOOK-1", "account_id": "ACC-1",
         "bank_flow_id": "B1", "month": "2024-01", "currency": "CNY",
         "direction": "outflow", "amount_minor": 4},
        {"kind": "bank_flow", "ledger_id": "BOOK-1", "account_id": "ACC-1",
         "bank_flow_id": "B2", "month": "2024-01", "currency": "CNY",
         "direction": "outflow", "amount_minor": 5},
    ])
    write_jsonl(pkg / "vouchers.jsonl", [
        {"kind": "voucher", "ledger_id": "BOOK-1", "voucher_id": "P",
         "month": "2024-01", "currency": "CNY", "direction": "outflow",
         "amount_minor": 5, "voucher_type": "payment"},
        {"kind": "voucher", "ledger_id": "BOOK-1", "voucher_id": "Q",
         "month": "2024-01", "currency": "CNY", "direction": "outflow",
         "amount_minor": 4, "voucher_type": "payment"},
    ])
    write_jsonl(pkg / "clearing.jsonl", [
        {"kind": "clearing", "ledger_id": "BOOK-1", "account_id": "ACC-1",
         "bank_flow_id": "B1", "voucher_id": "P", "amount_minor": 4},
        {"kind": "clearing", "ledger_id": "BOOK-1", "account_id": "ACC-1",
         "bank_flow_id": "B2", "voucher_id": "P", "amount_minor": 1},
        {"kind": "clearing", "ledger_id": "BOOK-1", "account_id": "ACC-1",
         "bank_flow_id": "B2", "voucher_id": "Q", "amount_minor": 4},
    ])
    write_jsonl(pkg / "allocation_plan.jsonl", m1_plan("P", "Q"))
    write_jsonl(pkg / "control_bank.jsonl", [
        {"kind": "control", "side": "bank", "account_id": "ACC-1",
         "currency": "CNY", "direction": "outflow", "count": 2,
         "amount_minor": 9}])
    write_jsonl(pkg / "control_voucher.jsonl", [
        {"kind": "control", "side": "voucher", "currency": "CNY",
         "direction": "outflow", "count": 2, "amount_minor": 9}])
    write_json(pkg / "manifest.json", {
        "ledger_id": "BOOK-1", "month": "2024-01",
        "announcement_result_dir": "../" + ann_dir(),
        "files": {"bank_flow": "bank_flows.jsonl",
                  "voucher": "vouchers.jsonl",
                  "clearing": "clearing.jsonl",
                  "allocation_plan": "allocation_plan.jsonl"},
        "control_files": {"bank": "control_bank.jsonl",
                          "voucher": "control_voucher.jsonl"}})
    write_json(pkg / "expected.json", {
        "_note": "Hand-computed from the specification, not from the code.",
        "month": "2024-01",
        "payment": {"A": 7, "B": 2},
        "refund": {"A": 0, "B": 0},
        "net": {"A": 7, "B": 2},
        "payment_lines": {
            "P": [{"section_id": "A", "amount_minor": 3},
                  {"section_id": "B", "amount_minor": 2}],
            "Q": [{"section_id": "A", "amount_minor": 4}]},
        "flow_lines": {
            "P": [{"account_id": "ACC-1", "bank_flow_id": "B1",
                   "section_id": "A", "amount_minor": 2},
                  {"account_id": "ACC-1", "bank_flow_id": "B1",
                   "section_id": "B", "amount_minor": 2},
                  {"account_id": "ACC-1", "bank_flow_id": "B2",
                   "section_id": "A", "amount_minor": 1}],
            "Q": [{"account_id": "ACC-1", "bank_flow_id": "B2",
                   "section_id": "A", "amount_minor": 4}]},
    })


# ------------------------------------------------------------------ month 2
def build_february() -> None:
    pkg = FIX / "february"
    if pkg.exists():
        shutil.rmtree(pkg)
    write_jsonl(pkg / "bank_flows.jsonl", [
        {"kind": "bank_flow", "ledger_id": "BOOK-1", "account_id": "ACC-1",
         "bank_flow_id": "B3", "month": "2024-02", "currency": "CNY",
         "direction": "inflow", "amount_minor": 1}])
    write_jsonl(pkg / "vouchers.jsonl", [
        {"kind": "voucher", "ledger_id": "BOOK-1", "voucher_id": "R1",
         "month": "2024-02", "currency": "CNY", "direction": "inflow",
         "amount_minor": 1, "voucher_type": "refund",
         "payment_voucher_id": "P", "rate": "0.2"}])
    write_jsonl(pkg / "clearing.jsonl", [
        {"kind": "clearing", "ledger_id": "BOOK-1", "account_id": "ACC-1",
         "bank_flow_id": "B3", "voucher_id": "R1", "amount_minor": 1}])
    write_jsonl(pkg / "allocation_plan.jsonl", [])
    write_jsonl(pkg / "control_bank.jsonl", [
        {"kind": "control", "side": "bank", "account_id": "ACC-1",
         "currency": "CNY", "direction": "inflow", "count": 1,
         "amount_minor": 1}])
    write_jsonl(pkg / "control_voucher.jsonl", [
        {"kind": "control", "side": "voucher", "currency": "CNY",
         "direction": "inflow", "count": 1, "amount_minor": 1}])
    write_json(pkg / "manifest.json", {
        "ledger_id": "BOOK-1", "month": "2024-02",
        "announcement_result_dir": "../" + ann_dir(),
        "files": {"bank_flow": "bank_flows.jsonl",
                  "voucher": "vouchers.jsonl",
                  "clearing": "clearing.jsonl",
                  "allocation_plan": "allocation_plan.jsonl"},
        "control_files": {"bank": "control_bank.jsonl",
                          "voucher": "control_voucher.jsonl"}})
    write_json(pkg / "expected.json", {
        "_note": "1 of P's 5 is returned; B has the larger section key, so "
                 "the whole 1 is charged to B (B keeps 1 of its 2).",
        "month": "2024-02",
        "payment": {},
        "refund": {"A": 0, "B": 1},
        "net": {"A": 0, "B": -1},
        "refund_lines": {
            "R1": [{"section_id": "B", "amount_minor": -1,
                    "original_voucher_id": "P", "original_month": "2024-01",
                    "original_rank": 1}]},
    })


# ------------------------------------------------------------------ month 3
def build_march(ratio: str = "0.4", amount: int = 2,
                name: str = "march") -> None:
    pkg = FIX / name
    if pkg.exists():
        shutil.rmtree(pkg)
    # A fresh February plan proves March does not consult this month's
    # scheme: it is present but must be ignored for the refund.
    ev = evidence()
    month_plan = [{
        "kind": "allocation_plan", "ledger_id": "BOOK-1",
        "plan_id": "PLAN-MARCH", "voucher_id": "M1", "project_id": "PRJ-A",
        "section_id": "B", "weight": 1,
        "result_id": ev["PRJ-A/B"]["result_id"],
        "result_unit_assertion_id": ev["PRJ-A/B"]["assertion_id"]}]
    write_jsonl(pkg / "bank_flows.jsonl", [
        {"kind": "bank_flow", "ledger_id": "BOOK-1", "account_id": "ACC-1",
         "bank_flow_id": "B4", "month": "2024-03", "currency": "CNY",
         "direction": "inflow", "amount_minor": amount}])
    write_jsonl(pkg / "vouchers.jsonl", [
        {"kind": "voucher", "ledger_id": "BOOK-1", "voucher_id": "R2",
         "month": "2024-03", "currency": "CNY", "direction": "inflow",
         "amount_minor": amount, "voucher_type": "refund",
         "payment_voucher_id": "P", "rate": ratio}])
    write_jsonl(pkg / "clearing.jsonl", [
        {"kind": "clearing", "ledger_id": "BOOK-1", "account_id": "ACC-1",
         "bank_flow_id": "B4", "voucher_id": "R2", "amount_minor": amount}])
    write_jsonl(pkg / "allocation_plan.jsonl", month_plan)
    # M1 is declared in the plan file but is not a voucher of this month;
    # the plan itself is unused (no payment voucher references it).
    write_jsonl(pkg / "control_bank.jsonl", [
        {"kind": "control", "side": "bank", "account_id": "ACC-1",
         "currency": "CNY", "direction": "inflow", "count": 1,
         "amount_minor": amount}])
    write_jsonl(pkg / "control_voucher.jsonl", [
        {"kind": "control", "side": "voucher", "currency": "CNY",
         "direction": "inflow", "count": 1, "amount_minor": amount}])
    write_json(pkg / "manifest.json", {
        "ledger_id": "BOOK-1", "month": "2024-03",
        "announcement_result_dir": "../" + ann_dir(),
        "files": {"bank_flow": "bank_flows.jsonl",
                  "voucher": "vouchers.jsonl",
                  "clearing": "clearing.jsonl",
                  "allocation_plan": "allocation_plan.jsonl"},
        "control_files": {"bank": "control_bank.jsonl",
                          "voucher": "control_voucher.jsonl"}})
    write_json(pkg / "expected.json", {
        "_note": "February consumed 1 of P's B line (2 -> 1). March's 2 "
                 "still goes B first (1) then A (1); B is empty afterwards. "
                 "The March plan is ignored.",
        "month": "2024-03",
        "payment": {},
        "refund": {"A": 1, "B": 1},
        "net": {"A": -1, "B": -1},
        "refund_lines": {
            "R2": [{"section_id": "B", "amount_minor": -1,
                    "original_voucher_id": "P", "original_month": "2024-01",
                    "original_rank": 1},
                   {"section_id": "A", "amount_minor": -1,
                    "original_voucher_id": "P", "original_month": "2024-01",
                    "original_rank": 0}]},
    })


def build_march_over() -> None:
    """Same March package but the refund is 5, more than P can still return."""
    build_march(ratio="1.0", amount=5, name="march_over")
    path = FIX / "march_over" / "expected.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_note"] = ("P is 5, February returned 1, so only 4 is refundable. "
                     "A 5 refund must block the close with the difference.")
    data["blocking"] = {
        "code": "refund_exceeds_remaining",
        "remaining": 4,
        "refund": 5,
    }
    data.pop("refund_lines", None)
    write_json(path, data)


def build_wiring() -> None:
    """Two 1分 debits clearing one 2分 equal-weight voucher."""
    pkg = FIX / "wiring"
    if pkg.exists():
        shutil.rmtree(pkg)
    ev = evidence()
    write_jsonl(pkg / "bank_flows.jsonl", [
        {"kind": "bank_flow", "ledger_id": "BOOK-W", "account_id": "ACC-1",
         "bank_flow_id": "W1", "month": "2024-04", "currency": "CNY",
         "direction": "outflow", "amount_minor": 1},
        {"kind": "bank_flow", "ledger_id": "BOOK-W", "account_id": "ACC-1",
         "bank_flow_id": "W2", "month": "2024-04", "currency": "CNY",
         "direction": "outflow", "amount_minor": 1},
    ])
    write_jsonl(pkg / "vouchers.jsonl", [
        {"kind": "voucher", "ledger_id": "BOOK-W", "voucher_id": "VW",
         "month": "2024-04", "currency": "CNY", "direction": "outflow",
         "amount_minor": 2, "voucher_type": "payment"},
    ])
    write_jsonl(pkg / "clearing.jsonl", [
        {"kind": "clearing", "ledger_id": "BOOK-W", "account_id": "ACC-1",
         "bank_flow_id": "W1", "voucher_id": "VW", "amount_minor": 1},
        {"kind": "clearing", "ledger_id": "BOOK-W", "account_id": "ACC-1",
         "bank_flow_id": "W2", "voucher_id": "VW", "amount_minor": 1},
    ])
    write_jsonl(pkg / "allocation_plan.jsonl", [
        {"kind": "allocation_plan", "ledger_id": "BOOK-W",
         "plan_id": "PLAN-VW", "voucher_id": "VW", "project_id": "PRJ-A",
         "section_id": sec, "weight": 1,
         "result_id": ev[f"PRJ-A/{sec}"]["result_id"],
         "result_unit_assertion_id": ev[f"PRJ-A/{sec}"]["assertion_id"]}
        for sec in ("A", "B")])
    write_jsonl(pkg / "control_bank.jsonl", [
        {"kind": "control", "side": "bank", "account_id": "ACC-1",
         "currency": "CNY", "direction": "outflow", "count": 2,
         "amount_minor": 2}])
    write_jsonl(pkg / "control_voucher.jsonl", [
        {"kind": "control", "side": "voucher", "currency": "CNY",
         "direction": "outflow", "count": 1, "amount_minor": 2}])
    write_json(pkg / "manifest.json", {
        "ledger_id": "BOOK-W", "month": "2024-04",
        "announcement_result_dir": "../" + ann_dir(),
        "files": {"bank_flow": "bank_flows.jsonl",
                  "voucher": "vouchers.jsonl",
                  "clearing": "clearing.jsonl",
                  "allocation_plan": "allocation_plan.jsonl"},
        "control_files": {"bank": "control_bank.jsonl",
                          "voucher": "control_voucher.jsonl"}})
    write_json(pkg / "expected.json", {
        "_note": "The 2-unit voucher is split once, 1 to each section. The "
                 "two 1-unit debits then attribute their own unit exactly "
                 "(1+0 and 0+1), so the headline is A 1 / B 1. Rounding "
                 "each debit's share of both lines separately would have "
                 "given A 2.",
        "month": "2024-04",
        "payment": {"A": 1, "B": 1},
        "flow_lines": {
            "VW": [{"account_id": "ACC-1", "bank_flow_id": "W1",
                    "section_id": "A", "amount_minor": 1},
                   {"account_id": "ACC-1", "bank_flow_id": "W2",
                    "section_id": "B", "amount_minor": 1}]},
    })


def main() -> None:
    build_announcement()
    build_january()
    build_february()
    build_march()
    build_march_over()
    build_wiring()
    print("fund fixtures written to", FIX)


if __name__ == "__main__":
    main()
