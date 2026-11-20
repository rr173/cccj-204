"""Durable monthly ledger (separate SQLite file) with single-writer locking.

One ledger database holds every closed month of one account set.  A
month is appended exactly once, inside one SQLite transaction, while a
process-wide file lock is held; a second process closing the same month
blocks on the lock, then observes the committed month and either reuses
it (identical inputs) or refuses to overwrite it (different inputs).

Every row keeps
* business identities (account/flow, voucher number, clearing pair);
* signed section allocation, its rank, weight and remainder;
* the announcement locator of the plan line (result id, document,
  assertion id, unit text, verified span);
* the exact source file and 1-based JSONL line of the row that produced
  every amount;
* for refund lines, the original payment allocation it reduces
  (natural key) and the original month.

Allocation lines are keyed naturally — ``(month, ledger_id, voucher_id,
rank)`` — so appending a month can never collide with another month's
autoincrement ids.  Nothing is held in memory for a whole month: rows
are parsed into a temporary SQLite file and copied by ``INSERT …
SELECT`` under the final transaction.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .errors import FundError, MonthAlreadyClosed
from .money import next_month

SCHEMA_VERSION = "fund-ledger-1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS closed_months (
    month TEXT PRIMARY KEY,
    ledger_id TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    package_dir TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    announcement_result_id TEXT NOT NULL,
    announcement_store_digest TEXT NOT NULL,
    envelope_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_flows (
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, account_id TEXT NOT NULL,
    bank_flow_id TEXT NOT NULL, currency TEXT NOT NULL,
    direction TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    PRIMARY KEY (month, ledger_id, account_id, bank_flow_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_flow_global
    ON bank_flows(ledger_id, account_id, bank_flow_id);
CREATE TABLE IF NOT EXISTS vouchers (
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, voucher_id TEXT NOT NULL,
    currency TEXT NOT NULL, direction TEXT NOT NULL,
    voucher_type TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    payment_voucher_id TEXT, rate TEXT,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    PRIMARY KEY (month, ledger_id, voucher_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_voucher_global
    ON vouchers(ledger_id, voucher_id);
CREATE TABLE IF NOT EXISTS clearings (
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, account_id TEXT NOT NULL,
    bank_flow_id TEXT NOT NULL, voucher_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    PRIMARY KEY (month, ledger_id, account_id, bank_flow_id, voucher_id)
);
CREATE TABLE IF NOT EXISTS plan_lines (
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, plan_id TEXT NOT NULL,
    voucher_id TEXT NOT NULL, project_id TEXT NOT NULL,
    section_id TEXT NOT NULL, weight INTEGER NOT NULL,
    result_id TEXT NOT NULL, assertion_id TEXT NOT NULL,
    ev_document_id TEXT, ev_unit_raw TEXT, ev_published_at TEXT,
    ev_span_json TEXT,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    PRIMARY KEY (month, ledger_id, plan_id, project_id, section_id)
);
CREATE TABLE IF NOT EXISTS payment_allocations (
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, voucher_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    project_id TEXT NOT NULL, section_id TEXT NOT NULL,
    weight INTEGER NOT NULL, sum_weights INTEGER NOT NULL,
    remainder INTEGER NOT NULL,
    amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
    result_id TEXT NOT NULL, assertion_id TEXT NOT NULL,
    ev_document_id TEXT, ev_unit_raw TEXT, ev_published_at TEXT,
    ev_span_json TEXT, plan_id TEXT NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    PRIMARY KEY (month, ledger_id, voucher_id, rank)
);
CREATE INDEX IF NOT EXISTS ix_payment_voucher
    ON payment_allocations(ledger_id, voucher_id);
CREATE TABLE IF NOT EXISTS payment_flow_lines (
    month TEXT NOT NULL, ledger_id TEXT NOT NULL, voucher_id TEXT NOT NULL,
    rank INTEGER NOT NULL, account_id TEXT NOT NULL,
    bank_flow_id TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    PRIMARY KEY (month, ledger_id, voucher_id, rank, account_id,
                bank_flow_id)
);
CREATE TABLE IF NOT EXISTS refund_allocations (
    month TEXT NOT NULL, ledger_id TEXT NOT NULL,
    refund_voucher_id TEXT NOT NULL, rank INTEGER NOT NULL,
    original_voucher_id TEXT NOT NULL, original_month TEXT NOT NULL,
    original_rank INTEGER NOT NULL,
    project_id TEXT NOT NULL, section_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    capacity INTEGER NOT NULL, prior_consumed INTEGER NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    PRIMARY KEY (month, ledger_id, refund_voucher_id, rank)
);
CREATE INDEX IF NOT EXISTS ix_refund_original
    ON refund_allocations(ledger_id, original_voucher_id, original_rank);
CREATE TABLE IF NOT EXISTS refund_flow_lines (
    month TEXT NOT NULL, ledger_id TEXT NOT NULL,
    refund_voucher_id TEXT NOT NULL, rank INTEGER NOT NULL,
    account_id TEXT NOT NULL, bank_flow_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    PRIMARY KEY (month, ledger_id, refund_voucher_id, rank, account_id,
                bank_flow_id)
);
CREATE TABLE IF NOT EXISTS month_summary (
    month TEXT NOT NULL, project_id TEXT NOT NULL, section_id TEXT NOT NULL,
    currency TEXT NOT NULL, payment_minor INTEGER NOT NULL,
    refund_minor INTEGER NOT NULL, net_minor INTEGER NOT NULL,
    PRIMARY KEY (month, project_id, section_id, currency)
);
"""

# Tables copied from the staging database, in dependency order.  Each
# entry is (table, SELECT over the staging database); the staging-only
# ``clearings.valid`` flag is not carried into the durable ledger.
COPY_TABLES = (
    ("bank_flows", "SELECT * FROM stage.bank_flows"),
    ("vouchers", "SELECT * FROM stage.vouchers"),
    ("clearings", "SELECT month, ledger_id, account_id, bank_flow_id, "
                  "voucher_id, amount_minor, source_file, source_line "
                  "FROM stage.clearings WHERE valid=1"),
    ("plan_lines", "SELECT * FROM stage.plan_lines"),
    ("payment_allocations", "SELECT * FROM stage.payment_allocations"),
    ("payment_flow_lines", "SELECT * FROM stage.payment_flow_lines"),
    ("refund_allocations", "SELECT * FROM stage.refund_allocations"),
    ("refund_flow_lines", "SELECT * FROM stage.refund_flow_lines"),
    ("month_summary", "SELECT * FROM stage.month_summary"),
)


class LedgerError(RuntimeError):
    """The ledger file is unreadable or structurally inconsistent."""


def ledger_path(ledger_dir: Path) -> Path:
    return Path(ledger_dir).resolve() / "fund.sqlite3"


def lock_path(ledger_dir: Path) -> Path:
    return Path(ledger_dir).resolve() / "fund.sqlite3.lock"


def connect_rw(ledger_dir: Path) -> sqlite3.Connection:
    path = ledger_path(ledger_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=120, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) "
                     "VALUES('schema_version', ?)", (SCHEMA_VERSION,))
    elif row["value"] != SCHEMA_VERSION:
        conn.close()
        raise LedgerError(
            f"ledger schema {row['value']!r} != {SCHEMA_VERSION!r}")
    return conn


def connect_ro(ledger_dir: Path) -> Optional[sqlite3.Connection]:
    """Read-only connection, or ``None`` when no ledger exists yet."""
    path = ledger_path(ledger_dir)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=120)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise LedgerError(f"{path} is not a fund ledger: {exc}") from exc
    return conn


@contextlib.contextmanager
def exclusive_lock(ledger_dir: Path) -> Iterator[None]:
    """Block until this process holds the single-writer ledger lock."""
    path = lock_path(ledger_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass(frozen=True)
class MonthRow:
    month: str
    ledger_id: str
    input_digest: str
    closed_at: str
    package_dir: str
    announcement_result_id: str
    envelope: dict


def list_months(conn: Optional[sqlite3.Connection]) -> list[MonthRow]:
    if conn is None:
        return []
    return [MonthRow(
        month=r["month"], ledger_id=r["ledger_id"],
        input_digest=r["input_digest"], closed_at=r["closed_at"],
        package_dir=r["package_dir"],
        announcement_result_id=r["announcement_result_id"],
        envelope=json.loads(r["envelope_json"])) for r in conn.execute(
            "SELECT * FROM closed_months ORDER BY month")]


def get_month(conn: Optional[sqlite3.Connection], month: str
              ) -> Optional[MonthRow]:
    if conn is None:
        return None
    r = conn.execute(
        "SELECT * FROM closed_months WHERE month=?", (month,)).fetchone()
    if r is None:
        return None
    return MonthRow(
        month=r["month"], ledger_id=r["ledger_id"],
        input_digest=r["input_digest"], closed_at=r["closed_at"],
        package_dir=r["package_dir"],
        announcement_result_id=r["announcement_result_id"],
        envelope=json.loads(r["envelope_json"]))


def order_check(months: list[MonthRow], month: str) -> Optional[str]:
    """Why closing ``month`` now is out of order, or None when allowed."""
    if not months:
        return None  # the first close may choose any start month
    closed = [m.month for m in months]
    if month in closed:
        return None  # handled by the reuse/conflict branch
    expected = next_month(max(closed))
    if month != expected:
        return (f"month {month} is not the next natural month after "
                f"{max(closed)} (expected {expected}); months close in "
                "order")
    return None


# ------------------------------------------------------------------- readers
class LedgerReader:
    """Point lookups against already-closed months (never writes)."""

    def __init__(self, conn: Optional[sqlite3.Connection]):
        self.conn = conn

    def voucher(self, ledger_id: str, voucher_id: str) -> Optional[dict]:
        if self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM vouchers WHERE ledger_id=? AND voucher_id=?",
            (ledger_id, voucher_id)).fetchone()
        return dict(row) if row else None

    def flow_exists(self, ledger_id: str, account_id: str,
                    bank_flow_id: str) -> bool:
        if self.conn is None:
            return False
        return self.conn.execute(
            "SELECT 1 FROM bank_flows WHERE ledger_id=? AND account_id=? "
            "AND bank_flow_id=?",
            (ledger_id, account_id, bank_flow_id)).fetchone() is not None

    def payment_lines(self, ledger_id: str, voucher_id: str) -> list[dict]:
        if self.conn is None:
            return []
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM payment_allocations WHERE ledger_id=? AND "
            "voucher_id=? ORDER BY project_id, section_id",
            (ledger_id, voucher_id))]

    def prior_consumed(self, ledger_id: str, original_voucher_id: str,
                       original_rank: int) -> int:
        if self.conn is None:
            return 0
        row = self.conn.execute(
            "SELECT COALESCE(SUM(-amount_minor), 0) AS n FROM "
            "refund_allocations WHERE ledger_id=? AND "
            "original_voucher_id=? AND original_rank=?",
            (ledger_id, original_voucher_id, original_rank)).fetchone()
        return int(row["n"])


# -------------------------------------------------------------------- writer
def write_month(ledger_dir: Path, package, stage_path: Path,
                input_digest: str, announcement_result_id: str,
                envelope_extra: dict) -> tuple[dict, bool]:
    """Append one month atomically.  Caller must hold :func:`exclusive_lock`.

    Returns ``(envelope, created)``: ``created`` is False when another
    process already committed the same month with exactly these inputs
    while this one waited for the lock.  Raises ``MonthAlreadyClosed``
    when the month exists with a different input digest.
    """
    conn = connect_rw(ledger_dir)
    try:
        conn.execute("ATTACH DATABASE ? AS stage", (str(stage_path),))
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT input_digest FROM closed_months WHERE month=?",
                (package.month,)).fetchone()
            if existing is not None:
                conn.execute("ROLLBACK")
                if existing["input_digest"] == input_digest:
                    return get_month(conn, package.month).envelope, False
                raise MonthAlreadyClosed(package.month,
                                         existing["input_digest"],
                                         input_digest)

            closed = [r["month"] for r in conn.execute(
                "SELECT month FROM closed_months ORDER BY month")]
            if closed and package.month != next_month(max(closed)):
                conn.execute("ROLLBACK")
                raise FundError(
                    f"{package.month} is not the next natural month after "
                    f"{max(closed)}")

            # Global identities are unique across the ledger: reusing a
            # voucher/flow number in another month means the same document
            # was submitted twice under different months.
            clash = conn.execute(
                "SELECT v.voucher_id AS id, v.month AS month FROM "
                "stage.vouchers s JOIN vouchers v ON "
                "v.ledger_id=s.ledger_id AND v.voucher_id=s.voucher_id "
                "LIMIT 1").fetchone()
            if clash is not None:
                conn.execute("ROLLBACK")
                raise FundError(
                    f"voucher {clash['id']} already exists in month "
                    f"{clash['month']}; voucher numbers are unique in the "
                    "ledger")
            clash = conn.execute(
                "SELECT f.bank_flow_id AS id, f.account_id AS acct, "
                "f.month AS month FROM stage.bank_flows s JOIN bank_flows f "
                "ON f.ledger_id=s.ledger_id AND "
                "f.account_id=s.account_id AND f.bank_flow_id=s.bank_flow_id "
                "LIMIT 1").fetchone()
            if clash is not None:
                conn.execute("ROLLBACK")
                raise FundError(
                    f"bank flow {clash['acct']}/{clash['id']} already "
                    f"exists in month {clash['month']}")

            closed_at = _dt.datetime.now(_dt.timezone.utc).replace(
                microsecond=0).isoformat()
            envelope = {
                "month": package.month,
                "ledger_id": package.ledger_id,
                "input_digest": input_digest,
                "closed_at": closed_at,
                "package_dir": str(package.directory),
                "announcement_result_id": announcement_result_id,
                "announcement_store_digest":
                    package.announcement_store_digest,
                "manifest_digest": package.manifest_digest,
                **envelope_extra,
            }
            conn.execute(
                "INSERT INTO closed_months(month, ledger_id, input_digest, "
                "closed_at, package_dir, manifest_digest, "
                "announcement_result_id, announcement_store_digest, "
                "envelope_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (package.month, package.ledger_id, input_digest, closed_at,
                 str(package.directory), package.manifest_digest,
                 announcement_result_id,
                 package.announcement_store_digest,
                 json.dumps(envelope, ensure_ascii=False, sort_keys=True)))
            for table, select_sql in COPY_TABLES:
                cols = _columns(conn, table)
                conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} "
                             f"FROM ({select_sql})")
            conn.execute("COMMIT")
            return envelope, True
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> str:
    ledger_cols = [r["name"] for r in conn.execute(
        f"PRAGMA table_info({table})")]
    return ", ".join(f'"{c}"' for c in ledger_cols)


# ------------------------------------------------------------------ readback
def read_month_payload(conn: sqlite3.Connection, month: str) -> dict:
    row = get_month(conn, month)
    if row is None:
        raise KeyError(month)
    payload = dict(row.envelope)
    payload["rows"] = {
        "bank_flows": [dict(r) for r in conn.execute(
            "SELECT * FROM bank_flows WHERE month=? ORDER BY account_id, "
            "bank_flow_id", (month,))],
        "vouchers": [dict(r) for r in conn.execute(
            "SELECT * FROM vouchers WHERE month=? ORDER BY voucher_id",
            (month,))],
        "clearings": [dict(r) for r in conn.execute(
            "SELECT * FROM clearings WHERE month=? ORDER BY account_id, "
            "bank_flow_id, voucher_id", (month,))],
        "plan_lines": [dict(r) for r in conn.execute(
            "SELECT * FROM plan_lines WHERE month=? ORDER BY plan_id, "
            "project_id, section_id", (month,))],
        "payment_allocations": [dict(r) for r in conn.execute(
            "SELECT * FROM payment_allocations WHERE month=? ORDER BY "
            "voucher_id, rank", (month,))],
        "payment_flow_lines": [dict(r) for r in conn.execute(
            "SELECT f.*, a.project_id, a.section_id FROM payment_flow_lines "
            "f JOIN payment_allocations a ON a.month=f.month AND "
            "a.ledger_id=f.ledger_id AND a.voucher_id=f.voucher_id AND "
            "a.rank=f.rank WHERE f.month=? ORDER BY f.voucher_id, "
            "f.rank, f.account_id, f.bank_flow_id", (month,))],
        "refund_allocations": [dict(r) for r in conn.execute(
            "SELECT * FROM refund_allocations WHERE month=? ORDER BY "
            "refund_voucher_id, rank", (month,))],
        "refund_flow_lines": [dict(r) for r in conn.execute(
            "SELECT f.*, a.project_id, a.section_id, "
            "a.amount_minor AS line_amount_minor FROM refund_flow_lines f "
            "JOIN refund_allocations a ON a.month=f.month AND "
            "a.ledger_id=f.ledger_id AND "
            "a.refund_voucher_id=f.refund_voucher_id AND "
            "a.rank=f.rank WHERE f.month=? ORDER BY "
            "f.refund_voucher_id, f.rank, f.account_id, f.bank_flow_id",
            (month,))],
        "month_summary": [dict(r) for r in conn.execute(
            "SELECT * FROM month_summary WHERE month=? ORDER BY project_id, "
            "section_id, currency", (month,))],
    }
    return payload
