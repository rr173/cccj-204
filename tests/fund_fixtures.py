"""Helpers to build throwaway month packages for fund-close tests.

Every package is written as JSON Lines with the same shape the
committed fixtures use, and its announcement evidence is read from the
committed read-only result store.  Nothing here imports the code under
test: a package is plain JSON on disk.
"""
from __future__ import annotations

import json
from pathlib import Path

FUND_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "fund"


def evidence(project: str = "PRJ-A", section: str = "A") -> dict:
    """The verified ``result_unit`` assertion proof for one section."""
    table = json.loads((FUND_FIX / "announcement_evidence.json").read_text(
        encoding="utf-8"))
    return table[f"{project}/{section}"]


def announcement_rel() -> str:
    return json.loads((FUND_FIX / "announcement_result_id.json").read_text(
        encoding="utf-8"))["result_dir"]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")


def flow(account: str, flow_id: str, month: str, amount: int,
         direction: str = "outflow", currency: str = "CNY",
         ledger: str = "BOOK-1") -> dict:
    return {"kind": "bank_flow", "ledger_id": ledger, "account_id": account,
            "bank_flow_id": flow_id, "month": month, "currency": currency,
            "direction": direction, "amount_minor": amount}


def voucher(voucher_id: str, month: str, amount: int, kind: str = "payment",
            direction: str = "outflow", currency: str = "CNY",
            ledger: str = "BOOK-1", original: str | None = None,
            rate: str | None = None) -> dict:
    row = {"kind": "voucher", "ledger_id": ledger, "voucher_id": voucher_id,
           "month": month, "currency": currency, "direction": direction,
           "amount_minor": amount, "voucher_type": kind}
    if original is not None:
        row["payment_voucher_id"] = original
    if rate is not None:
        row["rate"] = rate
    return row


def clearing(account: str, flow_id: str, voucher_id: str, amount: int,
             ledger: str = "BOOK-1") -> dict:
    return {"kind": "clearing", "ledger_id": ledger, "account_id": account,
            "bank_flow_id": flow_id, "voucher_id": voucher_id,
            "amount_minor": amount}


def plan(plan_id: str, voucher_id: str, project: str, section: str,
         weight: int, *, assertion: str | None = None,
         result_id: str | None = None, ledger: str = "BOOK-1",
         proof: dict | None = None) -> dict:
    proof = proof or evidence(project, section)
    return {"kind": "allocation_plan", "ledger_id": ledger,
            "plan_id": plan_id, "voucher_id": voucher_id,
            "project_id": project, "section_id": section, "weight": weight,
            "result_id": result_id if result_id is not None
            else proof["result_id"],
            "result_unit_assertion_id": assertion if assertion is not None
            else proof["assertion_id"]}


def control(side: str, currency: str, direction: str, count: int,
            amount: int, account: str | None = None) -> dict:
    """One upstream control row; voucher rows never carry an account."""
    row = {"kind": "control", "side": side, "currency": currency,
           "direction": direction, "count": count, "amount_minor": amount}
    if side == "bank":
        row["account_id"] = account or "ACC-1"
    return row


def build_package(directory: Path, *, ledger: str = "BOOK-1",
                  month: str = "2024-01",
                  flows: list[dict] | None = None,
                  vouchers: list[dict] | None = None,
                  clearings: list[dict] | None = None,
                  plans: list[dict] | None = None,
                  bank_control: list[dict] | None = None,
                  voucher_control: list[dict] | None = None,
                  announcement: str | None = None) -> Path:
    """Write one month package; returns its directory.

    Control files default to nothing at all, so a test that does not
    care about control totals must pass the matching rows explicitly.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "bank_flow": "bank_flows.jsonl",
        "voucher": "vouchers.jsonl",
        "clearing": "clearing.jsonl",
        "allocation_plan": "allocation_plan.jsonl",
    }
    write_jsonl(directory / files["bank_flow"], flows or [])
    write_jsonl(directory / files["voucher"], vouchers or [])
    write_jsonl(directory / files["clearing"], clearings or [])
    write_jsonl(directory / files["allocation_plan"], plans or [])
    write_jsonl(directory / "control_bank.jsonl", bank_control or [])
    write_jsonl(directory / "control_voucher.jsonl", voucher_control or [])
    manifest = {
        "ledger_id": ledger, "month": month,
        "announcement_result_dir": announcement or (
            "../" + announcement_rel() if directory.parent.name != "fund"
            else "../" + announcement_rel()),
        "files": files,
        "control_files": {"bank": "control_bank.jsonl",
                          "voucher": "control_voucher.jsonl"},
    }
    if announcement is None:
        # Resolve relative to the package directory; tests usually put
        # packages next to the fixtures, but an absolute path also works.
        manifest["announcement_result_dir"] = str(
            (FUND_FIX / announcement_rel()).resolve())
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return directory
