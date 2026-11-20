"""Read-only announcement-basis impact analysis for closed fund payments.

The monthly close (see :mod:`bidfact.fund.close`) records, on every
positive payment allocation line, the exact ``result_unit`` announcement
assertion that finance relied on (result id, assertion id, document,
verified span).  This module answers a *later* audit question: given a
subsequent bidfact result store and a query cutoff instant, what has
since become of the assertion behind each already-closed payment line,
how much of that line has been refunded, and where is the evidence?

The analysis never writes to either input.  Both databases are opened
with SQLite read-only URI connections; all scratch work goes into a
private temporary staging database, and the payment allocation /
refund / assertion / closure rows are streamed by cursor in bounded
batches — nothing is materialised as a whole-table Python list.

Status of one payment allocation line, at the cutoff:

* ``closed`` — the relied-on assertion is closed by a confirmed
  relation (``replace``/``retract``) whose acting document was
  published at or before the cutoff.  The closure evidence names the
  acting notice, relation id, verb and publication time.  A later
  retraction of *other* assertions never reopens a closed one, and a
  newer publication time by itself never replaces anything.
* ``contested`` — the assertion is still open, but the same
  ``(project, section, result_unit)`` scope holds two or more open
  confirmed assertions with different values at the cutoff.  Every
  distinct value with every provenance row is reported; none is chosen
  for being newer.
* ``unchanged`` — the assertion is still open, confirmed, in the same
  scope, and every open assertion of that scope carries the same value.
* ``unverifiable`` — the later evidence cannot answer: the result id
  does not match the opened store, the assertion is missing, not
  confirmed, out of scope, not published yet at the cutoff, or the
  input store/ledger cannot be read.  The precise reason code is kept
  on the row; absence of data is never interpreted as "unchanged".

Refunds are taken exclusively from the closed ledger's
``refund_allocations`` lineage (the waterfall persisted at each close);
they are never recomputed from the refund month's scheme.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from .errors import FundError
from .ledger import LedgerError, connect_ro
from .money import parse_month

# Public status vocabulary (kept as module constants for callers/tests).
STATUS_UNCHANGED = "unchanged"
STATUS_CONTESTED = "contested"
STATUS_CLOSED = "closed"
STATUS_UNVERIFIABLE = "unverifiable"
STATUSES = (STATUS_UNCHANGED, STATUS_CONTESTED, STATUS_CLOSED,
            STATUS_UNVERIFIABLE)

# Reason codes carried by unverifiable rows.
REASON_STORE_RESULT_MISMATCH = "result_id_mismatch"
REASON_ASSERTION_MISSING = "assertion_missing_in_later_store"
REASON_ASSERTION_NOT_CONFIRMED = "assertion_not_confirmed"
REASON_SCOPE_MISMATCH = "assertion_scope_mismatch"
REASON_FIELD_MISMATCH = "assertion_field_mismatch"
REASON_NOT_PUBLISHED_AT_CUTOFF = "assertion_not_published_at_cutoff"
REASON_LEDGER_UNREADABLE = "ledger_unreadable"

_BATCH = 1000

# Scratch schema.  Only compact locator/key columns are copied from the
# inputs; the full evidence blobs stay in the read-only stores.
STAGE_SCHEMA = """
CREATE TABLE stg_allocations (
    line_key TEXT PRIMARY KEY,
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, voucher_id TEXT NOT NULL,
    rank INTEGER NOT NULL, project_id TEXT NOT NULL, section_id TEXT NOT NULL,
    currency TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    result_id TEXT NOT NULL, assertion_id TEXT NOT NULL,
    ev_document_id TEXT, ev_published_at TEXT, ev_span_json TEXT,
    plan_id TEXT NOT NULL, source_file TEXT NOT NULL, source_line INTEGER NOT NULL
);
CREATE TABLE stg_refunds (
    original_voucher_id TEXT NOT NULL, original_rank INTEGER NOT NULL,
    refund_month TEXT NOT NULL, refund_voucher_id TEXT NOT NULL,
    rank INTEGER NOT NULL, amount_minor INTEGER NOT NULL
);
CREATE INDEX ix_stg_refund_line ON stg_refunds(original_voucher_id,
                                               original_rank);
CREATE TABLE stg_closures (
    target_assertion_id TEXT NOT NULL, closure_id TEXT NOT NULL,
    relation_id TEXT NOT NULL, acting_document_id TEXT NOT NULL,
    verb TEXT NOT NULL, field TEXT NOT NULL, published_at TEXT NOT NULL
);
CREATE INDEX ix_stg_closure_target ON stg_closures(target_assertion_id);
CREATE TABLE stg_open_assertions (
    assertion_id TEXT NOT NULL, document_id TEXT NOT NULL,
    project_id TEXT, section_id TEXT, field TEXT NOT NULL,
    normalized TEXT, raw_value TEXT NOT NULL, currency TEXT,
    status TEXT NOT NULL, published_at TEXT NOT NULL,
    span_json TEXT NOT NULL
);
CREATE INDEX ix_stg_open_scope ON stg_open_assertions(project_id,
                                                      section_id, field);
"""


class ImpactError(FundError):
    """The impact analysis inputs cannot be used (nothing was written)."""


@dataclass(frozen=True)
class ImpactFilters:
    month_from: Optional[str] = None
    month_to: Optional[str] = None
    project_id: Optional[str] = None
    section_id: Optional[str] = None


@dataclass
class ImpactReport:
    """Full analysis result.  ``lines`` stays empty when streaming to JSONL."""

    ledger_dir: str
    result_store: str
    result_id: str
    cutoff: str
    filters: dict
    ledger_store_digest: str
    announcement_store_digest: str
    line_count: int = 0
    lines: list[dict] = field(default_factory=list)
    summary_by_status: list[dict] = field(default_factory=list)
    summary_by_section: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kind": "announcement_impact_report",
            "ledger_dir": self.ledger_dir,
            "result_store": self.result_store,
            "result_id": self.result_id,
            "cutoff": self.cutoff,
            "filters": self.filters,
            "ledger_store_digest": self.ledger_store_digest,
            "announcement_store_digest": self.announcement_store_digest,
            "line_count": self.line_count,
            "summary_by_status": self.summary_by_status,
            "summary_by_section": self.summary_by_section,
            "lines": self.lines,
        }


# ----------------------------------------------------------------- validation
def _validate_cutoff(cutoff: str) -> str:
    if not isinstance(cutoff, str) or not cutoff:
        raise ImpactError("cutoff must be a non-empty ISO datetime string")
    # The stores compare publication instants lexicographically (ISO-8601
    # with a fixed layout); reject obvious non-ISO shapes rather than
    # silently comparing garbage strings.
    text = cutoff.strip()
    if len(text) < 10 or text[4] != "-" or text[7] != "-":
        raise ImpactError(f"cutoff {cutoff!r} is not an ISO datetime")
    return text


def _validate_filters(filters: ImpactFilters) -> dict:
    out: dict = {}
    if filters.month_from is not None:
        out["month_from"] = parse_month(filters.month_from, "month_from")
    else:
        out["month_from"] = None
    if filters.month_to is not None:
        out["month_to"] = parse_month(filters.month_to, "month_to")
    else:
        out["month_to"] = None
    if (out["month_from"] is not None and out["month_to"] is not None
            and out["month_from"] > out["month_to"]):
        raise ImpactError(
            f"month_from {out['month_from']} is later than month_to "
            f"{out['month_to']}")
    out["project_id"] = filters.project_id
    out["section_id"] = filters.section_id
    return out


def _resolve_store_path(result_arg: Path) -> Path:
    """Accept either the result directory or its ``store.sqlite3`` file."""
    p = Path(result_arg)
    if p.is_dir():
        p = p / "store.sqlite3"
    if not p.is_file():
        raise ImpactError(
            f"announcement result {result_arg} is not a bidfact result "
            "store (no store.sqlite3)")
    return p.resolve()


def _open_announcement_store(store_path: Path) -> sqlite3.Connection:
    """Open a later-result store read-only and verify its bidfact shape."""
    try:
        conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True,
                               timeout=120)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise ImpactError(
            f"cannot open announcement store {store_path} read-only: "
            f"{exc}") from exc
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='result_id'").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise ImpactError(
            f"{store_path} is not a bidfact result store: {exc}") from exc
    if row is None or not row["value"]:
        conn.close()
        raise ImpactError(
            f"{store_path} has no result_id in meta; only bidfact-generated "
            "SQLite result stores are accepted")
    for table in ("assertions", "closures"):
        try:
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            conn.close()
            raise ImpactError(
                f"{store_path} is missing the bidfact {table} table: "
                f"{exc}") from exc
    return conn


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------- staging
def _new_stage() -> tuple[sqlite3.Connection, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="bidfact-impact-"))
    path = tmp / "impact-stage.sqlite3"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(STAGE_SCHEMA)
    return conn, tmp


def _stream(cursor: sqlite3.Cursor) -> Iterator[sqlite3.Row]:
    while True:
        rows = cursor.fetchmany(_BATCH)
        if not rows:
            return
        for row in rows:
            yield row


def _stage_fund_data(stage: sqlite3.Connection, fund: sqlite3.Connection,
                     flt: dict) -> None:
    """Copy the locator columns of in-scope closed lines and their refunds."""
    where = ["1=1"]
    params: list = []
    if flt["month_from"] is not None:
        where.append("p.month >= ?")
        params.append(flt["month_from"])
    if flt["month_to"] is not None:
        where.append("p.month <= ?")
        params.append(flt["month_to"])
    if flt["project_id"] is not None:
        where.append("p.project_id = ?")
        params.append(flt["project_id"])
    if flt["section_id"] is not None:
        where.append("p.section_id = ?")
        params.append(flt["section_id"])
    where_sql = " AND ".join(where)

    stage.execute("BEGIN")
    cur = fund.execute(
        "SELECT month, ledger_id, voucher_id, rank, project_id, section_id, "
        "currency, amount_minor, result_id, assertion_id, ev_document_id, "
        "ev_published_at, ev_span_json, plan_id, source_file, source_line "
        f"FROM payment_allocations p WHERE {where_sql} ORDER BY month, "
        "ledger_id, voucher_id, rank", params)
    n = 0
    batch: list[tuple] = []
    for r in _stream(cur):
        line_key = "|".join((r["month"], r["ledger_id"], r["voucher_id"],
                             str(r["rank"])))
        batch.append((line_key, r["month"], r["ledger_id"], r["voucher_id"],
                      r["rank"], r["project_id"], r["section_id"],
                      r["currency"], int(r["amount_minor"]), r["result_id"],
                      r["assertion_id"], r["ev_document_id"],
                      r["ev_published_at"], r["ev_span_json"], r["plan_id"],
                      r["source_file"], int(r["source_line"])))
        if len(batch) >= _BATCH:
            stage.executemany(
                "INSERT INTO stg_allocations VALUES ("
                + ",".join("?" for _ in batch[0]) + ")", batch)
            batch.clear()
        n += 1
    if batch:
        stage.executemany(
            "INSERT INTO stg_allocations VALUES ("
            + ",".join("?" for _ in batch[0]) + ")", batch)

    # Refund lineage for every in-scope original line.  Refunds in months
    # after month_to must not count: the report answers "refunded as of
    # the selected window".  The rows themselves come from the closed
    # ledger; the waterfall is never recomputed here.
    refund_where = ["1=1"]
    refund_params: list = []
    if flt["month_to"] is not None:
        refund_where.append("r.month <= ?")
        refund_params.append(flt["month_to"])
    cur = fund.execute(
        "SELECT r.original_voucher_id, r.original_rank, r.month, "
        "r.refund_voucher_id, r.rank, r.amount_minor FROM "
        "refund_allocations r WHERE "
        + " AND ".join(refund_where)
        + " ORDER BY r.original_voucher_id, r.original_rank, r.month, "
        "r.refund_voucher_id, r.rank", refund_params)
    batch = []
    for r in _stream(cur):
        batch.append((r["original_voucher_id"], int(r["original_rank"]),
                      r["month"], r["refund_voucher_id"], int(r["rank"]),
                      int(r["amount_minor"])))
        if len(batch) >= _BATCH:
            stage.executemany(
                "INSERT INTO stg_refunds VALUES (?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        stage.executemany("INSERT INTO stg_refunds VALUES (?,?,?,?,?,?)",
                          batch)
    stage.execute("COMMIT")


def _stage_announcement_data(stage: sqlite3.Connection,
                             ann: sqlite3.Connection, cutoff: str) -> None:
    """Copy cutoff-eligible closures and open-candidate assertions."""
    stage.execute("BEGIN")
    cur = ann.execute(
        "SELECT target_assertion_id, closure_id, relation_id, "
        "acting_document_id, verb, field, published_at FROM closures "
        "WHERE published_at <= ? ORDER BY published_at, closure_id",
        (cutoff,))
    batch = []
    for r in _stream(cur):
        batch.append((r["target_assertion_id"], r["closure_id"],
                      r["relation_id"], r["acting_document_id"], r["verb"],
                      r["field"], r["published_at"]))
        if len(batch) >= _BATCH:
            stage.executemany(
                "INSERT INTO stg_closures VALUES (?,?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        stage.executemany("INSERT INTO stg_closures VALUES (?,?,?,?,?,?,?)",
                          batch)

    cur = ann.execute(
        "SELECT assertion_id, document_id, project_id, section_id, field, "
        "normalized, raw_value, currency, status, published_at, span_json "
        "FROM assertions ORDER BY assertion_id")
    batch = []
    for r in _stream(cur):
        batch.append((r["assertion_id"], r["document_id"], r["project_id"],
                      r["section_id"], r["field"], r["normalized"],
                      r["raw_value"], r["currency"], r["status"],
                      r["published_at"], r["span_json"]))
        if len(batch) >= _BATCH:
            stage.executemany(
                "INSERT INTO stg_open_assertions VALUES ("
                + ",".join("?" for _ in batch[0]) + ")", batch)
            batch.clear()
    if batch:
        stage.executemany(
            "INSERT INTO stg_open_assertions VALUES ("
            + ",".join("?" for _ in batch[0]) + ")", batch)
    stage.execute("COMMIT")


# ------------------------------------------------------------- per-line logic
def _closure_payload(stage: sqlite3.Connection, assertion_id: str
                     ) -> list[dict]:
    return [{
        "closure_id": r["closure_id"],
        "relation_id": r["relation_id"],
        "acting_document_id": r["acting_document_id"],
        "verb": r["verb"],
        "field": r["field"],
        "published_at": r["published_at"],
        "target_assertion_id": assertion_id,
    } for r in stage.execute(
        "SELECT * FROM stg_closures WHERE target_assertion_id=? ORDER BY "
        "published_at, closure_id", (assertion_id,))]


def _provenance(row: sqlite3.Row) -> dict:
    return {
        "assertion_id": row["assertion_id"],
        "document_id": row["document_id"],
        "project_id": row["project_id"],
        "section_id": row["section_id"],
        "field": row["field"],
        "normalized": row["normalized"],
        "raw_value": row["raw_value"],
        "currency": row["currency"],
        "published_at": row["published_at"],
        "span": json.loads(row["span_json"]),
    }


def _open_scope_values(stage: sqlite3.Connection, project_id: str,
                       section_id: str, cutoff: str) -> list[sqlite3.Row]:
    """Open confirmed result_unit assertions of one exact scope at cutoff.

    The closed-set filter runs inside SQLite against the indexed staging
    tables, so neither the closure table nor a scope's rows are held in
    Python as whole-table collections.
    """
    return list(stage.execute(
        "SELECT a.* FROM stg_open_assertions a WHERE a.project_id=? AND "
        "a.section_id=? AND a.field='result_unit' AND a.status='confirmed' "
        "AND a.published_at <= ? AND NOT EXISTS (SELECT 1 FROM "
        "stg_closures c WHERE c.target_assertion_id=a.assertion_id) "
        "ORDER BY a.published_at, a.assertion_id",
        (project_id, section_id, cutoff)))


def _classify(stage: sqlite3.Connection, line: sqlite3.Row,
              store_result_id: str, cutoff: str) -> dict:
    """Return the status block for one staged allocation line."""
    relied_assertion = line["assertion_id"]
    relied_result = line["result_id"]

    if relied_result != store_result_id:
        return {"status": STATUS_UNVERIFIABLE,
                "reason": REASON_STORE_RESULT_MISMATCH,
                "detail": {"stored_result_id": store_result_id,
                           "relied_result_id": relied_result}}

    # Closure first: a confirmed replace/retract at or before the cutoff
    # closes the assertion; nothing reopens it afterwards.
    closures = _closure_payload(stage, relied_assertion)
    if closures:
        return {"status": STATUS_CLOSED, "reason": None,
                "closure_evidence": closures,
                "open_values": []}

    row = stage.execute(
        "SELECT * FROM stg_open_assertions WHERE assertion_id=?",
        (relied_assertion,)).fetchone()
    if row is None:
        # Absent from the later store altogether.
        return {"status": STATUS_UNVERIFIABLE,
                "reason": REASON_ASSERTION_MISSING,
                "detail": {"assertion_id": relied_assertion}}
    if row["published_at"] > cutoff:
        # The later store knows the assertion, but it did not exist at
        # the cutoff; "not visible yet" is not "unchanged".
        return {"status": STATUS_UNVERIFIABLE,
                "reason": REASON_NOT_PUBLISHED_AT_CUTOFF,
                "detail": {"assertion_id": relied_assertion,
                           "published_at": row["published_at"],
                           "cutoff": cutoff}}
    if row["status"] != "confirmed":
        return {"status": STATUS_UNVERIFIABLE,
                "reason": REASON_ASSERTION_NOT_CONFIRMED,
                "detail": {"assertion_id": relied_assertion,
                           "assertion_status": row["status"]}}
    if row["field"] != "result_unit":
        return {"status": STATUS_UNVERIFIABLE,
                "reason": REASON_FIELD_MISMATCH,
                "detail": {"assertion_id": relied_assertion,
                           "field": row["field"]}}
    if (row["project_id"] != line["project_id"]
            or row["section_id"] != line["section_id"]):
        return {"status": STATUS_UNVERIFIABLE,
                "reason": REASON_SCOPE_MISMATCH,
                "detail": {
                    "assertion_id": relied_assertion,
                    "assertion_project_id": row["project_id"],
                    "assertion_section_id": row["section_id"],
                    "line_project_id": line["project_id"],
                    "line_section_id": line["section_id"]}}

    open_rows = _open_scope_values(stage, line["project_id"],
                                   line["section_id"], cutoff)
    # The relied assertion must itself be among the open rows; if it is
    # not, the closure branch above would already have fired. Defensive
    # guard keeps the reason explicit instead of silently misclassifying.
    if not any(r["assertion_id"] == relied_assertion for r in open_rows):
        return {"status": STATUS_UNVERIFIABLE,
                "reason": REASON_ASSERTION_MISSING,
                "detail": {"assertion_id": relied_assertion,
                           "note": "relied assertion is not an open "
                                   "confirmed scope assertion at cutoff"}}
    distinct = {(r["normalized"], r["currency"]) for r in open_rows}
    provenance = [_provenance(r) for r in open_rows]
    if len(distinct) == 1:
        return {"status": STATUS_UNCHANGED, "reason": None,
                "closure_evidence": [], "open_values": provenance}
    # Different open values: conflict, listed with every provenance row.
    # Publish time never selects a winner.
    return {"status": STATUS_CONTESTED, "reason": None,
            "closure_evidence": [], "open_values": provenance,
            "distinct_values": [
                {"normalized": n, "currency": c}
                for n, c in sorted(distinct,
                                   key=lambda nc: (str(nc[0]), str(nc[1])))]}


def _refund_view(stage: sqlite3.Connection, voucher_id: str, rank: int
                 ) -> dict:
    """Refunds already applied to one original allocation line."""
    rows = list(stage.execute(
        "SELECT * FROM stg_refunds WHERE original_voucher_id=? AND "
        "original_rank=? ORDER BY refund_month, refund_voucher_id, rank",
        (voucher_id, rank)))
    refunded = sum(-int(r["amount_minor"]) for r in rows)
    return {
        "refunded_minor": refunded,
        "refund_lineage": [{
            "refund_month": r["refund_month"],
            "refund_voucher_id": r["refund_voucher_id"],
            "refund_rank": int(r["rank"]),
            "amount_minor": -int(r["amount_minor"]),
        } for r in rows],
    }


# --------------------------------------------------------------------- runner
def _iter_line_rows(stage: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    cur = stage.execute(
        "SELECT * FROM stg_allocations ORDER BY month, ledger_id, "
        "voucher_id, rank")
    yield from _stream(cur)


def _make_line(stage: sqlite3.Connection, line: sqlite3.Row,
               store_result_id: str, seq: int, cutoff: str) -> dict:
    status_block = _classify(stage, line, store_result_id, cutoff)
    refunds = _refund_view(stage, line["voucher_id"], int(line["rank"]))
    amount = int(line["amount_minor"])
    refunded = refunds["refunded_minor"]
    outstanding = amount - refunded
    return {
        "kind": "announcement_impact_line",
        "line_seq": seq,
        "line_key": line["line_key"],
        "closed_month": line["month"],
        "ledger_id": line["ledger_id"],
        "payment_voucher_id": line["voucher_id"],
        "allocation_rank": int(line["rank"]),
        "project_id": line["project_id"],
        "section_id": line["section_id"],
        "currency": line["currency"],
        "allocated_minor": amount,
        "refunded_minor": refunded,
        "outstanding_minor": outstanding,
        "original_assertion": {
            "result_id": line["result_id"],
            "assertion_id": line["assertion_id"],
            "document_id": line["ev_document_id"],
            "published_at": line["ev_published_at"],
            "span": json.loads(line["ev_span_json"])
            if line["ev_span_json"] else None,
            "plan_id": line["plan_id"],
            "source": {"file": line["source_file"],
                       "line": int(line["source_line"])},
        },
        "later_status": status_block,
        "refund_lineage": refunds["refund_lineage"],
    }


def _summarize(lines: Iterator[dict]) -> tuple[list[dict], list[dict], int]:
    """Bounded aggregation: only running totals are kept, not the lines."""
    by_status: dict[tuple, dict] = {}
    by_section: dict[tuple, dict] = {}
    count = 0
    for item in lines:
        count += 1
        status = item["later_status"]["status"]
        cur = item["currency"]
        skey = (status, cur)
        bucket = by_status.setdefault(skey, {
            "kind": "announcement_impact_summary_status",
            "status": status, "currency": cur,
            "line_count": 0, "allocated_minor": 0, "refunded_minor": 0,
            "outstanding_minor": 0})
        bucket["line_count"] += 1
        bucket["allocated_minor"] += item["allocated_minor"]
        bucket["refunded_minor"] += item["refunded_minor"]
        bucket["outstanding_minor"] += item["outstanding_minor"]

        pkey = (item["project_id"], item["section_id"], cur)
        pbucket = by_section.setdefault(pkey, {
            "kind": "announcement_impact_summary_section",
            "project_id": item["project_id"],
            "section_id": item["section_id"], "currency": cur,
            "line_count": 0, "allocated_minor": 0, "refunded_minor": 0,
            "outstanding_minor": 0,
            "statuses": {s: 0 for s in STATUSES}})
        pbucket["line_count"] += 1
        pbucket["allocated_minor"] += item["allocated_minor"]
        pbucket["refunded_minor"] += item["refunded_minor"]
        pbucket["outstanding_minor"] += item["outstanding_minor"]
        pbucket["statuses"][status] += 1
    status_rows = [by_status[k] for k in sorted(by_status)]
    section_rows = [by_section[k] for k in sorted(by_section)]
    for row in section_rows:
        row["statuses"] = [{"status": s, "line_count": row["statuses"][s]}
                           for s in STATUSES if row["statuses"][s]]
    return status_rows, section_rows, count


def analyze_impact(ledger_dir: Path, result_arg: Path, cutoff: str,
                   filters: Optional[ImpactFilters] = None,
                   jsonl_path: Optional[Path] = None) -> ImpactReport:
    """Run the read-only impact analysis.

    When ``jsonl_path`` is given, detail records (and the two summaries)
    are streamed to that JSONL file and :attr:`ImpactReport.lines` stays
    empty; otherwise the detail lines are returned on the report.
    Nothing is ever written to the ledger or the announcement store.
    """
    cutoff = _validate_cutoff(cutoff)
    flt = _validate_filters(filters or ImpactFilters())
    ledger_dir = Path(ledger_dir)
    store_path = _resolve_store_path(result_arg)

    try:
        fund = connect_ro(ledger_dir)
    except LedgerError as exc:
        raise ImpactError(str(exc)) from exc
    if fund is None:
        raise ImpactError(
            f"no fund ledger at {ledger_dir} (fund.sqlite3 missing); an "
            "impact analysis needs a closed fund ledger")
    ann = _open_announcement_store(store_path)
    ledger_db = (Path(ledger_dir).resolve() / "fund.sqlite3")
    ledger_digest_before = _file_digest(ledger_db)
    store_digest_before = _file_digest(store_path)
    stage_conn, tmp = _new_stage()
    jsonl_fh = None
    try:
        store_result_id = ann.execute(
            "SELECT value FROM meta WHERE key='result_id'").fetchone()["value"]
        _stage_fund_data(stage_conn, fund, flt)
        _stage_announcement_data(stage_conn, ann, cutoff)

        def generated() -> Iterator[dict]:
            seq = 0
            for line in _iter_line_rows(stage_conn):
                seq += 1
                yield _make_line(stage_conn, line, store_result_id, seq,
                                 cutoff)

        if jsonl_path is not None:
            jsonl_path = Path(jsonl_path)
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            jsonl_fh = jsonl_path.open("w", encoding="utf-8")
            summary_s, summary_p, count = _summarize(_tee(generated(),
                                                          jsonl_fh))
        else:
            materialised = list(generated())
            summary_s, summary_p, count = _summarize(iter(materialised))
        report = ImpactReport(
            ledger_dir=str(Path(ledger_dir).resolve()),
            result_store=str(store_path),
            result_id=store_result_id, cutoff=cutoff, filters=flt,
            ledger_store_digest=ledger_digest_before,
            announcement_store_digest=store_digest_before,
            line_count=count,
            lines=[] if jsonl_path is not None else materialised,
            summary_by_status=summary_s,
            summary_by_section=summary_p)
    finally:
        if jsonl_fh is not None:
            jsonl_fh.close()
        stage_conn.close()
        ann.close()
        fund.close()
        shutil.rmtree(tmp, ignore_errors=True)

    # Re-prove immutability of both inputs after the read-only run.
    if _file_digest(ledger_db) != ledger_digest_before:
        raise ImpactError(
            "internal error: the fund ledger changed during the read-only "
            "analysis")
    if _file_digest(store_path) != store_digest_before:
        raise ImpactError(
            "internal error: the announcement store changed during the "
            "read-only analysis")
    return report


def _tee(lines: Iterator[dict], fh) -> Iterator[dict]:
    for item in lines:
        fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
        fh.write("\n")
        yield item


def write_report_jsonl(report: ImpactReport, path: Path) -> None:
    """Write a report (header + summaries + lines) as one JSONL stream."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        header = {k: v for k, v in report.as_dict().items()
                  if k not in ("summary_by_status", "summary_by_section",
                               "lines")}
        fh.write(json.dumps(header, ensure_ascii=False, sort_keys=True))
        fh.write("\n")
        for row in report.summary_by_status:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
        for row in report.summary_by_section:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
        for row in report.lines:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")


# ------------------------------------------------------------------ terminal
def render_terminal(report: ImpactReport) -> str:
    """Compact, deterministic terminal summary (Chinese labels)."""
    lines = []
    lines.append("公告依据影响分析（只读）")
    lines.append(f"  资金库        : {report.ledger_dir}")
    lines.append(f"  后续结果库    : {report.result_store}")
    lines.append(f"  结果号        : {report.result_id}")
    lines.append(f"  截止时刻      : {report.cutoff}")
    flt = report.filters
    lines.append(
        "  过滤          : 月份 "
        f"{flt['month_from'] or '*'} ~ {flt['month_to'] or '*'} / "
        f"项目 {flt['project_id'] or '*'} / 标段 {flt['section_id'] or '*'}")
    lines.append(f"  分摊行总数    : {report.line_count}")
    lines.append("")
    lines.append("按状态汇总:")
    header = f"  {'status':<13}{'ccy':<6}{'lines':>7}{'allocated':>12}" \
             f"{'refunded':>12}{'outstanding':>12}"
    lines.append(header)
    for row in report.summary_by_status:
        lines.append(
            f"  {row['status']:<13}{row['currency']:<6}"
            f"{row['line_count']:>7}{row['allocated_minor']:>12}"
            f"{row['refunded_minor']:>12}{row['outstanding_minor']:>12}")
    lines.append("")
    lines.append("按项目/标段汇总:")
    lines.append(f"  {'project':<12}{'section':<10}{'ccy':<6}{'lines':>7}"
                 f"{'allocated':>12}{'refunded':>12}{'outstanding':>12}"
                 "  statuses")
    for row in report.summary_by_section:
        statuses = ",".join(f"{s['status']}={s['line_count']}"
                            for s in row["statuses"])
        lines.append(
            f"  {row['project_id']:<12}{row['section_id']:<10}"
            f"{row['currency']:<6}{row['line_count']:>7}"
            f"{row['allocated_minor']:>12}{row['refunded_minor']:>12}"
            f"{row['outstanding_minor']:>12}  {statuses}")
    lines.append("")
    lines.append("明细可由 JSONL 输出按 line_key / line_seq 逐项展开。")
    return "\n".join(lines)
