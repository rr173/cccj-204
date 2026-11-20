"""Trial and close orchestration for the monthly fund ledger.

Both operations stream one month into a private *staging* SQLite
database in a temporary directory:

* :func:`trial_close` runs every check, then removes the staging file.
  It consumes no refund capacity, writes no closed-month record and
  changes nothing except its own temporary directory.
* :func:`close_month` re-runs the full validation, takes the exclusive
  ledger lock, re-reads the closed-month state, and copies the staged
  rows into the durable ledger in a single transaction.  A failure at
  any point leaves no partial month, does not advance the month
  sequence and does not consume refund capacity.

The announcement result store is opened read-only for evidence and its
digest is verified again immediately before the durable write; source
files are only ever opened for reading.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import evidence as ev
from .allocate import PlanRow, select_per_clearing, split_voucher
from .errors import CloseBlocked, FundError, MonthAlreadyClosed
from .inputs import (BUSINESS_KINDS, MonthPackage, SourceRef, iter_file,
                     load_package, parse_control_row, parse_row,
                     submitted_digest)
from .ledger import (LedgerReader, MonthRow, connect_ro, exclusive_lock,
                     get_month, list_months, order_check, read_month_payload,
                     write_month)
from .money import next_month
from .reconcile import reconcile_stage
from .refund import attach_refund_flows, plan_refunds

# Staging tables.  Columns that are copied into the durable ledger must
# match :mod:`bidfact.fund.ledger` exactly; ``stg_issues``, ``stg_control``
# and ``clearings.valid`` are staging-only.
STAGE_SCHEMA = """
CREATE TABLE stg_issues (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL, message TEXT NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    detail_json TEXT);
CREATE TABLE stg_control (
    side TEXT NOT NULL, account_id TEXT, currency TEXT NOT NULL,
    direction TEXT NOT NULL, count INTEGER NOT NULL,
    amount_minor INTEGER NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL);
CREATE TABLE bank_flows (
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, account_id TEXT NOT NULL,
    bank_flow_id TEXT NOT NULL, currency TEXT NOT NULL,
    direction TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    PRIMARY KEY (month, ledger_id, account_id, bank_flow_id));
CREATE UNIQUE INDEX ux_flow_global
    ON bank_flows(ledger_id, account_id, bank_flow_id);
CREATE TABLE vouchers (
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, voucher_id TEXT NOT NULL,
    currency TEXT NOT NULL, direction TEXT NOT NULL,
    voucher_type TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    payment_voucher_id TEXT, rate TEXT,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    PRIMARY KEY (month, ledger_id, voucher_id));
CREATE UNIQUE INDEX ux_voucher_global ON vouchers(ledger_id, voucher_id);
CREATE TABLE clearings (
    clearing_id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, account_id TEXT NOT NULL,
    bank_flow_id TEXT NOT NULL, voucher_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL, valid INTEGER NOT NULL DEFAULT 1,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    UNIQUE (month, ledger_id, account_id, bank_flow_id, voucher_id));
CREATE TABLE plan_lines (
    plan_line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, plan_id TEXT NOT NULL,
    voucher_id TEXT NOT NULL, project_id TEXT NOT NULL,
    section_id TEXT NOT NULL, weight INTEGER NOT NULL,
    result_id TEXT NOT NULL, assertion_id TEXT NOT NULL,
    ev_document_id TEXT, ev_unit_raw TEXT, ev_published_at TEXT,
    ev_span_json TEXT,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL);
CREATE TABLE payment_allocations (
    line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, voucher_id TEXT NOT NULL,
    project_id TEXT NOT NULL, section_id TEXT NOT NULL,
    rank INTEGER NOT NULL, weight INTEGER NOT NULL,
    sum_weights INTEGER NOT NULL, remainder INTEGER NOT NULL,
    amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
    result_id TEXT NOT NULL, assertion_id TEXT NOT NULL,
    ev_document_id TEXT, ev_unit_raw TEXT, ev_published_at TEXT,
    ev_span_json TEXT, plan_id TEXT NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL);
CREATE TABLE payment_flow_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id INTEGER NOT NULL, month TEXT NOT NULL, ledger_id TEXT NOT NULL,
    voucher_id TEXT NOT NULL, rank INTEGER NOT NULL,
    account_id TEXT NOT NULL, bank_flow_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL);
CREATE TABLE refund_allocations (
    refund_line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL, ledger_id TEXT NOT NULL,
    refund_voucher_id TEXT NOT NULL, original_voucher_id TEXT NOT NULL,
    original_month TEXT NOT NULL, original_rank INTEGER NOT NULL,
    project_id TEXT NOT NULL, section_id TEXT NOT NULL,
    rank INTEGER NOT NULL, amount_minor INTEGER NOT NULL,
    capacity INTEGER NOT NULL, prior_consumed INTEGER NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL);
CREATE TABLE refund_flow_lines (
    month TEXT NOT NULL, ledger_id TEXT NOT NULL,
    refund_voucher_id TEXT NOT NULL, rank INTEGER NOT NULL,
    account_id TEXT NOT NULL, bank_flow_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    PRIMARY KEY (month, ledger_id, refund_voucher_id, rank, account_id,
                bank_flow_id));
CREATE TABLE month_summary (
    month TEXT NOT NULL, project_id TEXT NOT NULL, section_id TEXT NOT NULL,
    currency TEXT NOT NULL, payment_minor INTEGER NOT NULL,
    refund_minor INTEGER NOT NULL, net_minor INTEGER NOT NULL,
    PRIMARY KEY (month, project_id, section_id, currency));
"""



@dataclass
class Report:
    month: str
    ledger_id: str
    ok: bool
    issues: list[dict]
    summary: list[dict]
    input_digest: str
    announcement_result_id: str
    announcement_store_digest: str
    package_dir: str
    counts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "month": self.month, "ledger_id": self.ledger_id,
            "ok": self.ok, "issues": self.issues, "summary": self.summary,
            "input_digest": self.input_digest,
            "announcement_result_id": self.announcement_result_id,
            "announcement_store_digest": self.announcement_store_digest,
            "package_dir": self.package_dir, "counts": self.counts,
        }


class _Sink:
    """Streaming issue collector; deduplicates identical issues."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._seen: set[tuple] = set()

    def __call__(self, issue: dict) -> None:
        src = issue["source"]
        detail = issue.get("detail")
        key = (issue["code"], issue["message"], src["file"], src["line"],
               json.dumps(detail, ensure_ascii=False, sort_keys=True)
               if detail is not None else "")
        if key in self._seen:
            return
        self._seen.add(key)
        self.conn.execute(
            "INSERT INTO stg_issues(code, message, source_file, source_line, "
            "detail_json) VALUES(?,?,?,?,?)",
            (issue["code"], issue["message"], src["file"], int(src["line"]),
             json.dumps(detail, ensure_ascii=False, sort_keys=True)
             if detail is not None else None))

    def add(self, code: str, ref, message: str,
            detail: Optional[dict] = None) -> None:
        self({
            "code": code, "message": message,
            "source": ref.as_dict() if hasattr(ref, "as_dict") else dict(ref),
            **({"detail": detail} if detail is not None else {}),
        })


def _stage_issues(conn) -> list[dict]:
    out = []
    for row in conn.execute(
            "SELECT * FROM stg_issues ORDER BY source_file, source_line, "
            "seq"):
        item = {"code": row["code"], "message": row["message"],
                "source": {"file": row["source_file"],
                           "line": int(row["source_line"])}}
        if row["detail_json"]:
            item["detail"] = json.loads(row["detail_json"])
        out.append(item)
    return out


# ------------------------------------------------------------------ staging
def _stage_inputs(conn, package: MonthPackage, sink: _Sink,
                  files: dict[str, str]) -> None:
    def hash_sink(name: str, digest: str) -> None:
        files[name] = digest

    for kind in ("bank_flow", "voucher", "clearing", "allocation_plan"):
        rel = package.files.get(kind)
        if rel is None:
            sink.add("file_not_declared", SourceRef("manifest.json", 0),
                     f"manifest.files does not declare the {kind} file; "
                     "the month is incomplete", {"kind": kind})
            continue
        for lineno, row in iter_file(package, rel, sink, hash_sink):
            ref = SourceRef(rel, lineno)
            row_kind = row.get("kind")
            if row_kind not in BUSINESS_KINDS:
                sink.add("unknown_row_kind", ref,
                         f"kind must be one of {sorted(BUSINESS_KINDS)}; "
                         f"got {row_kind!r}", {"kind": row_kind})
                continue
            if row_kind != kind:
                sink.add("row_kind_mismatch", ref,
                         f"row kind {row_kind!r} found in the {kind!r} file")
                continue
            record = parse_row(kind, row, ref, sink)
            if record is None:
                continue
            if kind == "bank_flow":
                cur = conn.execute(
                    "INSERT OR IGNORE INTO bank_flows VALUES(?,?,?,?,?,?,?,?,?)",
                    (record.month, record.ledger_id, record.account_id,
                     record.bank_flow_id, record.currency, record.direction,
                     record.amount_minor, ref.file, ref.line))
                if cur.rowcount == 0:
                    sink.add("duplicate_bank_flow", ref,
                             f"bank flow {record.account_id}/"
                             f"{record.bank_flow_id} repeats an earlier row",
                             {"key": [record.account_id,
                                      record.bank_flow_id]})
            elif kind == "voucher":
                cur = conn.execute(
                    "INSERT OR IGNORE INTO vouchers VALUES"
                    "(?,?,?,?,?,?,?,?,?,?,?)",
                    (record.month, record.ledger_id, record.voucher_id,
                     record.currency, record.direction, record.voucher_type,
                     record.amount_minor, record.payment_voucher_id,
                     record.rate, ref.file, ref.line))
                if cur.rowcount == 0:
                    sink.add("duplicate_voucher", ref,
                             f"voucher {record.voucher_id} repeats an "
                             "earlier row", {"key": record.voucher_id})
            elif kind == "clearing":
                cur = conn.execute(
                    "INSERT OR IGNORE INTO clearings(month, ledger_id, "
                    "account_id, bank_flow_id, voucher_id, amount_minor, "
                    "valid, source_file, source_line) "
                    "VALUES(?,?,?,?,?,?,1,?,?)",
                    (package.month, record.ledger_id, record.account_id,
                     record.bank_flow_id, record.voucher_id,
                     record.amount_minor, ref.file, ref.line))
                if cur.rowcount == 0:
                    sink.add("duplicate_clearing_pair", ref,
                             f"clearing for bank flow {record.account_id}/"
                             f"{record.bank_flow_id} and voucher "
                             f"{record.voucher_id} repeats an earlier row",
                             {"bank_flow": [record.account_id,
                                            record.bank_flow_id],
                              "voucher": record.voucher_id})
            else:
                conn.execute(
                    "INSERT INTO plan_lines(month, ledger_id, plan_id, "
                    "voucher_id, project_id, section_id, weight, result_id, "
                    "assertion_id, source_file, source_line) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (package.month, record.ledger_id, record.plan_id,
                     record.voucher_id, record.project_id, record.section_id,
                     record.weight, record.result_id,
                     record.result_unit_assertion_id, ref.file, ref.line))

    for side in ("bank", "voucher"):
        rel = package.control_files.get(side)
        if rel is None:
            sink.add("control_file_not_declared",
                     SourceRef("manifest.json", 0),
                     f"manifest.control_files does not declare the {side} "
                     "control file", {"side": side})
            continue
        for lineno, row in iter_file(package, rel, sink, hash_sink):
            ref = SourceRef(rel, lineno)
            control = parse_control_row(row, side, ref, sink)
            if control is None:
                continue
            conn.execute(
                "INSERT INTO stg_control(side, account_id, currency, "
                "direction, count, amount_minor, source_file, source_line) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (control.side, control.account_id, control.currency,
                 control.direction, control.count, control.amount_minor,
                 ref.file, ref.line))


def _check_prior_ledger(conn, package: MonthPackage, ledger_conn,
                        sink: _Sink) -> None:
    """Voucher and bank-flow numbers are unique across the whole ledger.

    Re-using a number in another month means the same document was
    submitted twice; it is reported at the line that repeats it and the
    close is blocked.  (The durable write checks this again under the
    lock, so a racing close cannot slip through.)
    """
    reader = LedgerReader(ledger_conn)
    for row in conn.execute(
            "SELECT * FROM vouchers ORDER BY source_file, source_line"):
        prior = reader.voucher(package.ledger_id, row["voucher_id"])
        if prior is not None and prior["month"] != package.month:
            sink.add("voucher_already_in_ledger",
                     SourceRef(row["source_file"], int(row["source_line"])),
                     f"voucher {row['voucher_id']} already exists in closed "
                     f"month {prior['month']}; voucher numbers are unique in "
                     "the ledger",
                     {"voucher_id": row["voucher_id"],
                      "closed_month": prior["month"]})
    for row in conn.execute(
            "SELECT * FROM bank_flows ORDER BY source_file, source_line"):
        if _flow_in_other_month(ledger_conn, package, row["account_id"],
                                row["bank_flow_id"]):
            sink.add("bank_flow_already_in_ledger",
                     SourceRef(row["source_file"], int(row["source_line"])),
                     f"bank flow {row['account_id']}/{row['bank_flow_id']} "
                     "already exists in a closed month",
                     {"account_id": row["account_id"],
                      "bank_flow_id": row["bank_flow_id"]})


# ----------------------------------------------------------------- evidence
def _flow_in_other_month(ledger_conn, package: MonthPackage, account_id: str,
                         bank_flow_id: str) -> bool:
    if ledger_conn is None:
        return False
    return ledger_conn.execute(
        "SELECT 1 FROM bank_flows WHERE ledger_id=? AND account_id=? AND "
        "bank_flow_id=? AND month != ?",
        (package.ledger_id, account_id, bank_flow_id,
         package.month)).fetchone() is not None


def _plan_row(row) -> PlanRow:
    return PlanRow(
        project_id=row["project_id"], section_id=row["section_id"],
        weight=int(row["weight"]), result_id=row["result_id"],
        assertion_id=row["assertion_id"], source_file=row["source_file"],
        source_line=int(row["source_line"]), plan_id=row["plan_id"])


def _verify_plans(conn, package: MonthPackage, sink: _Sink) -> str:
    """Check every plan line against the announcement store; return result id."""
    store_path = package.announcement_result_dir / "store.sqlite3"
    try:
        ann = ev.open_readonly(store_path)
    except ev.EvidenceError as exc:
        raise FundError(str(exc)) from exc
    try:
        meta = ann.execute(
            "SELECT value FROM meta WHERE key='result_id'").fetchone()
        result_id = meta["value"] if meta else ""
        seen_keys: dict[tuple, tuple] = {}
        plan_vouchers: dict[tuple, set] = {}
        for row in conn.execute(
                "SELECT * FROM plan_lines ORDER BY plan_id, project_id, "
                "section_id, plan_line_id"):
            ref = SourceRef(row["source_file"], int(row["source_line"]))
            key = (row["ledger_id"], row["plan_id"], row["project_id"],
                   row["section_id"])
            if key in seen_keys:
                first = seen_keys[key]
                sink.add("plan_duplicate_section", ref,
                         f"plan {row['plan_id']!r} lists section "
                         f"{row['project_id']}/{row['section_id']} again at "
                         f"{first[0]}:{first[1]}; one section key may appear "
                         "once per plan",
                         {"plan_id": row["plan_id"],
                          "project_id": row["project_id"],
                          "section_id": row["section_id"],
                          "first_source": {"file": first[0],
                                           "line": first[1]}})
                continue
            seen_keys[key] = (ref.file, ref.line)
            plan_vouchers.setdefault(
                (row["ledger_id"], row["plan_id"]), set()).add(
                    row["voucher_id"])
            if row["ledger_id"] != package.ledger_id:
                sink.add("plan_foreign_ledger", ref,
                         f"allocation plan {row['plan_id']!r} belongs to "
                         f"ledger {row['ledger_id']!r}, not "
                         f"{package.ledger_id!r}",
                         {"plan_id": row["plan_id"]})
                continue
            proof, reason = ev.check_plan_line(ann, _plan_row(row), result_id)
            if proof is None:
                sink.add("plan_evidence_invalid", ref,
                         f"announcement evidence for {row['project_id']}/"
                         f"{row['section_id']} is not valid: {reason}",
                         {"plan_id": row["plan_id"],
                          "result_id": row["result_id"],
                          "assertion_id": row["assertion_id"],
                          "reason": reason})
                continue
            conn.execute(
                "UPDATE plan_lines SET ev_document_id=?, ev_unit_raw=?, "
                "ev_published_at=?, ev_span_json=? WHERE plan_line_id=?",
                (proof.document_id, proof.unit_raw, proof.published_at,
                 json.dumps(proof.span, ensure_ascii=False, sort_keys=True),
                 row["plan_line_id"]))
        for (ledger_id, plan_id), vouchers in plan_vouchers.items():
            if len(vouchers) > 1:
                row = conn.execute(
                    "SELECT source_file, source_line FROM plan_lines WHERE "
                    "plan_id=? ORDER BY plan_line_id LIMIT 1",
                    (plan_id,)).fetchone()
                sink.add("plan_multiple_vouchers",
                         SourceRef(row["source_file"],
                                   int(row["source_line"])),
                         f"plan {plan_id!r} is referenced by several "
                         f"vouchers {sorted(vouchers)}; one approved plan "
                         "per voucher is required",
                         {"plan_id": plan_id,
                          "vouchers": sorted(vouchers)})
        return result_id
    finally:
        ann.close()


# --------------------------------------------------------------- allocation
def _allocate_payments(conn, package: MonthPackage, sink: _Sink) -> None:
    """Split each payment voucher exactly once and select its flow shares."""
    for voucher in conn.execute(
            "SELECT * FROM vouchers WHERE voucher_type='payment' ORDER BY "
            "voucher_id"):
        ref = SourceRef(voucher["source_file"],
                        int(voucher["source_line"]))
        voucher_id = voucher["voucher_id"]
        rows = list(conn.execute(
            "SELECT * FROM plan_lines WHERE ledger_id=? AND voucher_id=? "
            "ORDER BY plan_line_id", (package.ledger_id, voucher_id)))
        if not rows:
            sink.add("payment_without_plan", ref,
                     f"payment voucher {voucher_id} has no allocation plan "
                     "for this month; its sections cannot be determined",
                     {"voucher_id": voucher_id})
            continue
        if any(r["ev_document_id"] is None for r in rows):
            sink.add("payment_plan_incomplete", ref,
                     f"payment voucher {voucher_id} has a plan line without "
                     "valid announcement evidence; allocation is blocked",
                     {"voucher_id": voucher_id})
            continue
        lines = split_voucher(int(voucher["amount_minor"]),
                              [_plan_row(r) for r in rows])
        line_ids: list[int] = []
        for rank, line in enumerate(lines):
            cur = conn.execute(
                "INSERT INTO payment_allocations(month, ledger_id, "
                "voucher_id, project_id, section_id, rank, weight, "
                "sum_weights, remainder, amount_minor, currency, result_id, "
                "assertion_id, ev_document_id, ev_unit_raw, ev_published_at, "
                "ev_span_json, plan_id, source_file, source_line) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (package.month, package.ledger_id, voucher_id,
                 line.project_id, line.section_id, rank, line.weight,
                 line.sum_weights, line.remainder, line.amount_minor,
                 voucher["currency"], line.result_id, line.assertion_id,
                 rows[rank]["ev_document_id"], rows[rank]["ev_unit_raw"],
                 rows[rank]["ev_published_at"], rows[rank]["ev_span_json"],
                 line.plan_id, line.source_file, line.source_line))
            line_ids.append(cur.lastrowid)
        clearings = [(int(c["amount_minor"]), c["account_id"],
                      c["bank_flow_id"]) for c in conn.execute(
            "SELECT * FROM clearings WHERE valid=1 AND voucher_id=? "
            "ORDER BY account_id, bank_flow_id, clearing_id", (voucher_id,))]
        if sum(c[0] for c in clearings) != int(voucher["amount_minor"]):
            # Not fully paid: reconciliation already reported the gap, so
            # no exact flow attribution can exist and none is invented.
            continue
        attribution = select_per_clearing(
            [(rank, line.amount_minor, line.project_id, line.section_id)
             for rank, line in enumerate(lines)], clearings,
            int(voucher["amount_minor"]))
        for rank, account_id, bank_flow_id, share in attribution:
            conn.execute(
                "INSERT INTO payment_flow_lines(line_id, month, ledger_id, "
                "voucher_id, rank, account_id, bank_flow_id, amount_minor) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (line_ids[rank], package.month, package.ledger_id,
                 voucher_id, rank, account_id, bank_flow_id, share))


# ------------------------------------------------------------------ summary
def _build_summary(conn, package: MonthPackage) -> list[dict]:
    """Payment/refund/net per project+section+currency, cross-footed."""
    rows: dict[tuple, list[int]] = {}
    for r in conn.execute(
            "SELECT project_id, section_id, currency, amount_minor "
            "FROM payment_allocations"):
        key = (r["project_id"], r["section_id"], r["currency"])
        rows.setdefault(key, [0, 0])[0] += int(r["amount_minor"])
    for r in conn.execute(
            "SELECT a.project_id, a.section_id, v.currency, a.amount_minor "
            "FROM refund_allocations a JOIN vouchers v ON "
            "v.ledger_id=? AND v.voucher_id=a.refund_voucher_id",
            (package.ledger_id,)):
        key = (r["project_id"], r["section_id"], r["currency"])
        rows.setdefault(key, [0, 0])[1] += -int(r["amount_minor"])
    out = []
    for key in sorted(rows):
        payment, refund = rows[key]
        out.append({"project_id": key[0], "section_id": key[1],
                    "currency": key[2], "payment_minor": payment,
                    "refund_minor": refund, "net_minor": payment - refund})
        conn.execute(
            "INSERT INTO month_summary(month, project_id, section_id, "
            "currency, payment_minor, refund_minor, net_minor) "
            "VALUES(?,?,?,?,?,?,?)",
            (package.month, key[0], key[1], key[2], payment, refund,
             payment - refund))
    return out


def _totals_check(conn, sink: _Sink) -> None:
    """Cross-foot allocations against the vouchers they come from."""
    row = conn.execute(
        "SELECT (SELECT COALESCE(SUM(amount_minor), 0) FROM "
        "payment_allocations) AS allocated, (SELECT COALESCE(SUM("
        "amount_minor), 0) FROM vouchers WHERE voucher_type='payment') "
        "AS payments").fetchone()
    if int(row["allocated"]) != int(row["payments"]):
        sink.add("allocation_total_mismatch", SourceRef("manifest.json", 0),
                 f"section allocations sum to {row['allocated']} but the "
                 f"month's payment vouchers sum to {row['payments']}")
    # Only refunds that planned successfully take part: a refund blocked
    # on its own (capacity, currency, evidence) is already reported at its
    # own source line, and must not produce a second, less useful issue.
    row = conn.execute(
        "SELECT COALESCE(-SUM(amount_minor), 0) AS planned, "
        "COALESCE((SELECT SUM(amount_minor) FROM vouchers WHERE "
        "voucher_type='refund' AND voucher_id IN (SELECT DISTINCT "
        "refund_voucher_id FROM refund_allocations)), 0) AS planned_total "
        "FROM refund_allocations").fetchone()
    if int(row["planned"]) != int(row["planned_total"]):
        sink.add("refund_total_mismatch", SourceRef("manifest.json", 0),
                 f"refund allocations sum to {row['planned']} but the "
                 f"planned refund vouchers sum to {row['planned_total']}")


def _flow_crossfoot(conn, package: MonthPackage, sink: _Sink) -> None:
    """Both margins of the flow/line attribution must close exactly.

    For every fully paid voucher, the shares attributed to its bank flows
    must add up to each allocation line, and each flow's shares must add
    up to that flow's clearing amount.  A mismatch would mean a unit was
    invented or lost by the attribution, so it blocks the close.
    """
    for row in conn.execute(
            "SELECT voucher_id, SUM(amount_minor) AS attributed FROM "
            "payment_flow_lines GROUP BY ledger_id, voucher_id"):
        lines = conn.execute(
            "SELECT COALESCE(SUM(amount_minor), 0) FROM payment_allocations "
            "WHERE ledger_id=? AND voucher_id=?",
            (package.ledger_id, row["voucher_id"])).fetchone()[0]
        if int(row["attributed"]) != int(lines):
            sink.add("flow_attribution_mismatch", SourceRef("manifest.json", 0),
                     f"bank flow shares for {row['voucher_id']} sum to "
                     f"{row['attributed']} but its allocation lines sum to "
                     f"{lines}; no rounding may hide a unit",
                     {"voucher_id": row["voucher_id"]})
    for row in conn.execute(
            "SELECT voucher_id, account_id, bank_flow_id, "
            "SUM(amount_minor) AS attributed FROM payment_flow_lines "
            "GROUP BY ledger_id, voucher_id, account_id, bank_flow_id"):
        cleared = conn.execute(
            "SELECT amount_minor FROM clearings WHERE valid=1 AND "
            "ledger_id=? AND account_id=? AND bank_flow_id=? AND voucher_id=?",
            (package.ledger_id, row["account_id"], row["bank_flow_id"],
             row["voucher_id"])).fetchone()
        if cleared is None or int(row["attributed"]) != int(
                cleared["amount_minor"]):
            sink.add("flow_attribution_mismatch", SourceRef("manifest.json", 0),
                     f"flow {row['account_id']}/{row['bank_flow_id']} of "
                     f"{row['voucher_id']} attributed {row['attributed']} "
                     "but cleared "
                     f"{cleared['amount_minor'] if cleared else 'nothing'}")


def _counts(conn) -> dict:
    return {
        "bank_flows": conn.execute(
            "SELECT COUNT(*) FROM bank_flows").fetchone()[0],
        "vouchers": conn.execute(
            "SELECT COUNT(*) FROM vouchers").fetchone()[0],
        "clearings": conn.execute(
            "SELECT COUNT(*) FROM clearings WHERE valid=1").fetchone()[0],
        "plan_lines": conn.execute(
            "SELECT COUNT(*) FROM plan_lines").fetchone()[0],
        "payment_allocations": conn.execute(
            "SELECT COUNT(*) FROM payment_allocations").fetchone()[0],
        "refund_allocations": conn.execute(
            "SELECT COUNT(*) FROM refund_allocations").fetchone()[0],
        "summary_rows": conn.execute(
            "SELECT COUNT(*) FROM month_summary").fetchone()[0],
    }


# ------------------------------------------------------------------ runs
def _stage(tmp: str) -> tuple[sqlite3.Connection, Path]:
    """Open the private staging database.

    ``synchronous=OFF`` is safe here: the file is scratch data that is
    discarded after the run, and the durable ledger is written with its
    own default (FULL) durability.
    """
    path = Path(tmp) / "stage.sqlite3"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(STAGE_SCHEMA)
    return conn, path


def _validate(conn, package: MonthPackage, ledger_conn, sink: _Sink,
              files: dict[str, str]) -> str:
    try:
        # One transaction for the whole month keeps the staging write to
        # a single fsync-free batch; it is scratch data, never the ledger.
        conn.execute("BEGIN")
        _stage_inputs(conn, package, sink, files)
        _check_prior_ledger(conn, package, ledger_conn, sink)
        reconcile_stage(conn, package, sink)
        result_id = _verify_plans(conn, package, sink)
        _allocate_payments(conn, package, sink)
        plan_refunds(conn, package, sink, LedgerReader(ledger_conn))
        attach_refund_flows(conn, package)
        _build_summary(conn, package)
        _totals_check(conn, sink)
        _flow_crossfoot(conn, package, sink)
        conn.execute("COMMIT")
        return result_id
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def trial_close(package_dir: Path, ledger_dir: Path) -> Report:
    """Compute the month without consuming anything durable."""
    package = load_package(package_dir)
    files: dict[str, str] = {}
    ledger_conn = connect_ro(ledger_dir)
    tmp = tempfile.mkdtemp(prefix="bidfact-fund-trial-")
    conn, _ = _stage(tmp)
    try:
        sink = _Sink(conn)
        result_id = _validate(conn, package, ledger_conn, sink, files)
        issues = _stage_issues(conn)
        digest = submitted_digest(package.ledger_id, package.month,
                                  package.manifest_digest, result_id, files)
        months = list_months(ledger_conn)
        order_error = order_check(months, package.month)
        existing = get_month(ledger_conn, package.month)
        if existing is not None:
            if existing.input_digest == digest:
                issues.append({
                    "code": "informational_already_closed",
                    "message": f"{package.month} is already closed with "
                               "exactly these inputs; close will reuse the "
                               "closed result",
                    "source": {"file": "manifest.json", "line": 0}})
            else:
                issues.append({
                    "code": "month_already_closed",
                    "message": f"{package.month} is already closed with "
                               "different inputs; a closed month is never "
                               "overwritten",
                    "source": {"file": "manifest.json", "line": 0},
                    "detail": {"closed_input_digest": existing.input_digest,
                               "submitted_input_digest": digest}})
        elif order_error:
            issues.append({
                "code": "month_out_of_order", "message": order_error,
                "source": {"file": "manifest.json", "line": 0},
                "detail": {"closed_months": [m.month for m in months],
                           "expected": _expected_month(months)}})
        blocking = [i for i in issues
                    if i["code"] != "informational_already_closed"]
        summary = [dict(r) for r in conn.execute(
            "SELECT * FROM month_summary ORDER BY project_id, section_id, "
            "currency")]
        return Report(
            month=package.month, ledger_id=package.ledger_id,
            ok=not blocking, issues=issues, summary=summary,
            input_digest=digest, announcement_result_id=result_id,
            announcement_store_digest=package.announcement_store_digest,
            package_dir=str(package.directory), counts=_counts(conn))
    finally:
        conn.close()
        if ledger_conn is not None:
            ledger_conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _expected_month(months: list[MonthRow]) -> Optional[str]:
    if not months:
        return None
    return next_month(max(m.month for m in months))


def close_month(package_dir: Path, ledger_dir: Path) -> dict:
    """Validate and append the month; atomic and idempotent."""
    package = load_package(package_dir)
    files: dict[str, str] = {}
    tmp = tempfile.mkdtemp(prefix="bidfact-fund-close-")
    stage_conn, stage_path = _stage(tmp)
    try:
        sink = _Sink(stage_conn)
        ledger_conn = connect_ro(ledger_dir)
        try:
            result_id = _validate(stage_conn, package, ledger_conn, sink,
                                  files)
            digest = submitted_digest(package.ledger_id, package.month,
                                      package.manifest_digest, result_id,
                                      files)
            issues = _stage_issues(stage_conn)
            if issues:
                raise CloseBlocked(package.month, issues)
            existing = get_month(ledger_conn, package.month)
            if existing is not None:
                if existing.input_digest == digest:
                    return {"month": existing.month, "reused": True,
                            "input_digest": existing.input_digest,
                            "envelope": existing.envelope}
                raise MonthAlreadyClosed(package.month, existing.input_digest,
                                         digest)
            order_error = order_check(list_months(ledger_conn),
                                      package.month)
            if order_error:
                raise FundError(order_error)
        finally:
            if ledger_conn is not None:
                ledger_conn.close()

        with exclusive_lock(ledger_dir):
            current = ev.file_digest(
                package.announcement_result_dir / "store.sqlite3")
            if current != package.announcement_store_digest:
                raise FundError(
                    "announcement result store changed while this month was "
                    "being closed; evidence must stay immutable")
            envelope, created = write_month(
                ledger_dir, package, stage_path, digest, result_id,
                {"counts": _counts(stage_conn)})
            return {"month": package.month, "reused": not created,
                    "input_digest": digest, "envelope": envelope}
    finally:
        stage_conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ readers
def months_closed(ledger_dir: Path) -> list[dict]:
    conn = connect_ro(ledger_dir)
    try:
        return [{"month": m.month, "ledger_id": m.ledger_id,
                 "input_digest": m.input_digest, "closed_at": m.closed_at,
                 "announcement_result_id": m.announcement_result_id,
                 "package_dir": m.package_dir} for m in list_months(conn)]
    finally:
        if conn is not None:
            conn.close()


def read_month(ledger_dir: Path, month: str) -> dict:
    conn = connect_ro(ledger_dir)
    if conn is None:
        raise FundError(f"no fund ledger at {Path(ledger_dir)}")
    try:
        return read_month_payload(conn, month)
    except KeyError:
        raise FundError(f"month {month} is not closed") from None
    finally:
        conn.close()


def read_cumulative(ledger_dir: Path, month: str,
                    project_id: Optional[str] = None,
                    section_id: Optional[str] = None) -> list[dict]:
    """Payment/refund/net per section+currency up to and including ``month``."""
    conn = connect_ro(ledger_dir)
    if conn is None:
        raise FundError(f"no fund ledger at {Path(ledger_dir)}")
    try:
        sql = ("SELECT project_id, section_id, currency, "
               "SUM(payment_minor) AS payment, SUM(refund_minor) AS refund, "
               "SUM(net_minor) AS net FROM month_summary WHERE month <= ?")
        params: list = [month]
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        if section_id is not None:
            sql += " AND section_id = ?"
            params.append(section_id)
        sql += " GROUP BY project_id, section_id, currency ORDER BY "
        sql += "project_id, section_id, currency"
        return [{"project_id": r["project_id"],
                 "section_id": r["section_id"],
                 "currency": r["currency"],
                 "payment_minor": int(r["payment"]),
                 "refund_minor": int(r["refund"]),
                 "net_minor": int(r["net"])}
                for r in conn.execute(sql, params)]
    finally:
        conn.close()
