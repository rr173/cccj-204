"""Recall list over the warehouse lineage library.

A recall is answered for one **recall key** — ``(supplier_id,
supplier_batch, sku)`` — at one business cutoff sequence (inclusive).  No
amount, award or payment is consulted; the key is exactly what the
supplier announced and what the receipts recorded.

What the list contains:

* every piece that was received **at or before** the cutoff sequence and
  belongs to that key.  Receipts after the cutoff never take part; a
  different supplier's batch or the same box's other lots never match.
* for each such piece: where it is (live segment: carrier + local
  interval, location, available/quarantined) or who has it (shipped
  segment: shipment document, recipient, outbound interval).  Segments are
  reported with quantity, never expanded into one row per piece.
* each segment carries its **lineage path** back to the original receipt
  interval and the purchase locator, one hop per mapping row.  The path is
  bounded by chain depth, not by the number of matching pieces.

An optional receipt filter ``receipt_id`` plus a half-open local interval
``[from_pos, to_pos)`` restricts the hits to those original receipt
coordinates.  A hit segment is then reported **clipped** to the local
range (in both carrier and receipt coordinates), so a partial selection
counts only the selected pieces.  The lineage still points at the full
stored segment.

Ordering is by business sequence and stable identifiers, never by input
file order.  The total is cross-footed at the cutoff::

    received pieces (up to cutoff, clipped) = live + shipped

Segment iteration is streamed with a server-side cursor, so a recall of
many segments never materialises them all in Python.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from .errors import LibraryError, RecallError
from .store import connect_ro, library_digest


@dataclass(frozen=True)
class RecallQuery:
    supplier_id: str
    supplier_batch: str
    sku: str
    cutoff_seq: int
    receipt_id: Optional[str] = None
    from_pos: Optional[int] = None
    to_pos: Optional[int] = None

    def __post_init__(self):
        for name in ("supplier_id", "supplier_batch", "sku"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise RecallError(f"{name} must be a non-empty string")
        if (isinstance(self.cutoff_seq, bool)
                or not isinstance(self.cutoff_seq, int)
                or self.cutoff_seq <= 0):
            raise RecallError("cutoff_seq must be a positive integer")
        if self.receipt_id is None:
            if self.from_pos is not None or self.to_pos is not None:
                raise RecallError(
                    "a local range needs a receipt_id to be relative to")
        else:
            if not isinstance(self.receipt_id, str) or not self.receipt_id:
                raise RecallError("receipt_id must be a non-empty string")
            lo, hi = self.from_pos, self.to_pos
            if lo is None and hi is None:
                return  # a bare receipt filter selects the whole receipt
            lo = 0 if lo is None else lo
            if isinstance(lo, bool) or not isinstance(lo, int) or lo < 0:
                raise RecallError("from_pos must be a non-negative integer")
            if hi is not None:
                if isinstance(hi, bool) or not isinstance(hi, int) or hi <= lo:
                    raise RecallError(
                        "to_pos must be an integer greater than from_pos")
            object.__setattr__(self, "from_pos", lo)
            object.__setattr__(self, "to_pos", hi)

    @property
    def local_range(self) -> Optional[tuple[int, Optional[int]]]:
        """Half-open receipt-local range; ``to_pos=None`` means unbounded."""
        if self.receipt_id is None:
            return None
        if self.from_pos is None and self.to_pos is None:
            return None
        return (self.from_pos or 0, self.to_pos)


@dataclass
class RecallReport:
    query: RecallQuery
    library_path: str
    library_digest: str
    result_id: str
    live: list[dict] = field(default_factory=list)
    shipped: list[dict] = field(default_factory=list)
    live_total: int = 0
    shipped_total: int = 0
    received_total: int = 0
    receipts: list[dict] = field(default_factory=list)
    skipped_receipts: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.live_total + self.shipped_total

    def as_dict(self) -> dict:
        return {
            "query": _query_dict(self.query),
            "library": self.library_path,
            "library_digest": self.library_digest,
            "result_id": self.result_id,
            "received_total": self.received_total,
            "live_total": self.live_total,
            "shipped_total": self.shipped_total,
            "total": self.total,
            "receipts": self.receipts,
            "skipped_receipts": self.skipped_receipts,
            "live": self.live,
            "shipped": self.shipped,
        }


def _query_dict(query: RecallQuery) -> dict:
    return {"supplier_id": query.supplier_id,
            "supplier_batch": query.supplier_batch, "sku": query.sku,
            "cutoff_seq": query.cutoff_seq,
            "receipt_id": query.receipt_id,
            "from_pos": query.from_pos, "to_pos": query.to_pos}


def _cursor_batches(conn: sqlite3.Connection, sql: str, params
                    ) -> Iterator[sqlite3.Row]:
    cursor = conn.execute(sql, params)
    while True:
        rows = cursor.fetchmany(64)
        if not rows:
            return
        for row in rows:
            yield row


def _path(conn: sqlite3.Connection, carrier_id: str, pos: int
          ) -> list[dict]:
    """Walk the single-hop origin links back to the receipt interval.

    Each hop is the exact mapping row that produced the carrier offset;
    coordinates are preserved by offset through the hop.  The walk is
    bounded by chain depth, not by piece count.
    """
    steps: list[dict] = []
    current_id, current_pos = carrier_id, pos
    guard = 0
    while True:
        guard += 1
        if guard > 100000:
            raise LibraryError(
                f"lineage path for {carrier_id}@{pos} does not terminate")
        carrier = conn.execute(
            "SELECT * FROM carriers WHERE carrier_id=?",
            (current_id,)).fetchone()
        if carrier is None:
            raise LibraryError(
                f"lineage path references missing carrier {current_id!r}")
        if carrier["kind"] == "receipt":
            receipt = conn.execute(
                "SELECT * FROM receipts WHERE receipt_id=?",
                (carrier["receipt_id"],)).fetchone()
            steps.append({
                "kind": "receipt",
                "carrier_id": current_id,
                "pos": current_pos,
                "receipt_id": receipt["receipt_id"],
                "receipt_pos": current_pos,
                "seq": int(receipt["seq"]),
                "qty": int(receipt["qty"]),
                "source": {"file": receipt["source_file"],
                           "line": int(receipt["source_line"])},
                "locator": {
                    "result_id": receipt["result_id"],
                    "document_id": receipt["document_id"],
                    "project_id": receipt["project_id"],
                    "section_id": receipt["section_id"],
                },
            })
            steps.reverse()
            return steps
        mapping = conn.execute(
            "SELECT * FROM movement_mappings WHERE target_id=? AND "
            "to_start <= ? AND to_end > ? ORDER BY idx LIMIT 1",
            (current_id, current_pos, current_pos)).fetchone()
        if mapping is None:
            raise LibraryError(
                f"no mapping produced carrier {current_id!r} at "
                f"{current_pos}")
        length = int(mapping["to_end"]) - int(mapping["to_start"])
        if length != int(mapping["from_end"]) - int(mapping["from_start"]):
            raise LibraryError(
                f"mapping {int(mapping['seq'])}:{int(mapping['idx'])} maps "
                "intervals of different length; the library is inconsistent")
        offset = current_pos - int(mapping["to_start"])
        if not 0 <= offset < length:
            raise LibraryError(
                f"offset {offset} is outside mapping {int(mapping['seq'])}:"
                f"{int(mapping['idx'])} (length {length})")
        new_pos = int(mapping["from_start"]) + offset
        movement = conn.execute(
            "SELECT * FROM movements WHERE seq=?",
            (int(mapping["seq"]),)).fetchone()
        if movement is None:
            raise LibraryError(
                f"mapping references missing movement {int(mapping['seq'])}")
        steps.append({
            "kind": "movement",
            "seq": int(mapping["seq"]),
            "movement_type": movement["movement_type"],
            "mapping_index": int(mapping["idx"]),
            "target_id": current_id,
            "target_pos": current_pos,
            "source_id": mapping["source_id"],
            "source_pos": new_pos,
            "from": [int(mapping["from_start"]), int(mapping["from_end"])],
            "to": [int(mapping["to_start"]), int(mapping["to_end"])],
            "length": int(mapping["to_end"]) - int(mapping["to_start"]),
        })
        current_id, current_pos = mapping["source_id"], new_pos


def _receipt_locator(conn: sqlite3.Connection, receipt_id: str) -> dict:
    row = conn.execute(
        "SELECT result_id, document_id, project_id, section_id, "
        "source_file, source_line FROM receipts WHERE receipt_id=?",
        (receipt_id,)).fetchone()
    if row is None:
        raise LibraryError(f"segment references missing receipt {receipt_id!r}")
    return {"result_id": row["result_id"],
            "document_id": row["document_id"],
            "project_id": row["project_id"],
            "section_id": row["section_id"],
            "source": {"file": row["source_file"],
                       "line": int(row["source_line"])}}


def _clip(receipt_pos: int, length: int,
          local: Optional[tuple[int, Optional[int]]]
          ) -> Optional[tuple[int, int]]:
    """The visible part of a segment's receipt interval, or None."""
    lo, hi = receipt_pos, receipt_pos + length
    if local is None:
        return lo, hi
    clo = max(lo, local[0])
    chi = hi if local[1] is None else min(hi, local[1])
    if clo >= chi:
        return None
    return clo, chi


def _segment_record(conn, row: sqlite3.Row, kind: str,
                    local: Optional[tuple[int, int]]) -> Optional[dict]:
    rpos = int(row["receipt_pos"])
    length = int(row["length"])
    clipped = _clip(rpos, length, local)
    if clipped is None:
        return None
    clo, chi = clipped
    pos = int(row["pos"]) + (clo - rpos)
    shown = chi - clo
    record = {
        "kind": kind,
        "carrier_id": row["carrier_id"],
        "interval": [pos, pos + shown],
        "length": shown,
        "receipt_id": row["receipt_id"],
        "receipt_interval": [clo, chi],
        "receipt_segment_full": [rpos, rpos + length],
        "receipt_qty": int(row["receipt_qty"]),
        "locator": _receipt_locator(conn, row["receipt_id"]),
        "lineage": _path(conn, row["carrier_id"], pos),
    }
    return record


def iter_recall(library_dir: Path, query: RecallQuery) -> Iterator[dict]:
    """Stream the recall as ``header``/``live``/``shipped``/``totals``.

    The library is opened read-only and closed when the iterator is
    exhausted or closed.  Verified cross-foot is done before ``totals``
    is yielded; a broken library raises :class:`LibraryError` instead of
    emitting a plausible but wrong list.
    """
    conn = connect_ro(library_dir)
    if conn is None:
        raise LibraryError(f"no warehouse library at {library_dir}")
    try:
        yield from _iter(conn, library_dir, query)
    finally:
        conn.close()


def _iter(conn: sqlite3.Connection, library_dir: Path, query: RecallQuery
          ) -> Iterator[dict]:
    digest = library_digest(conn)
    result_id = conn.execute(
        "SELECT value FROM meta WHERE key='announcement_result_id'"
    ).fetchone()["value"]
    local = query.local_range
    yield {
        "record": "header", "query": _query_dict(query),
        "library": str(Path(library_dir).resolve()),
        "library_digest": digest, "result_id": result_id,
    }

    # -- receipts that define the candidate scope ----------------------
    receipt_where = ["supplier_id=? AND supplier_batch=? AND sku=?"]
    params: list = [query.supplier_id, query.supplier_batch, query.sku]
    if query.receipt_id is not None:
        receipt_where.append("receipt_id=?")
        params.append(query.receipt_id)
    receipt_where.append("seq <= ?")
    params.append(query.cutoff_seq)
    seen_receipts: set[str] = set()
    received_total = 0
    for row in _cursor_batches(
            conn,
            "SELECT receipt_id, seq, qty, location_id FROM receipts WHERE "
            + " AND ".join(receipt_where) + " ORDER BY seq, receipt_id",
            params):
        seen_receipts.add(row["receipt_id"])
        lo, hi = _clip(0, int(row["qty"]), local) or (0, 0)
        received_total += hi - lo
        yield {"record": "receipt", "receipt_id": row["receipt_id"],
               "seq": int(row["seq"]), "qty": int(row["qty"]),
               "selected": [lo, hi],
               "locator": _receipt_locator(conn, row["receipt_id"])}

    # Receipts matching the key but arriving after the cutoff are named,
    # never silently mixed into the result or into the total.
    late_where = ["supplier_id=? AND supplier_batch=? AND sku=?",
                  "seq > ?"]
    late_params: list = [query.supplier_id, query.supplier_batch,
                         query.sku, query.cutoff_seq]
    if query.receipt_id is not None:
        late_where.append("receipt_id=?")
        late_params.append(query.receipt_id)
    for row in _cursor_batches(
            conn,
            "SELECT receipt_id, seq, qty FROM receipts WHERE "
            + " AND ".join(late_where) + " ORDER BY seq, receipt_id",
            late_params):
        yield {"record": "skipped_receipt", "receipt_id": row["receipt_id"],
               "seq": int(row["seq"]), "qty": int(row["qty"]),
               "reason": "received_after_cutoff"}

    live_total = 0
    shipped_total = 0
    live_sql = (
        "SELECT s.*, r.qty AS receipt_qty, c.created_seq AS created_seq "
        "FROM stock_segments s "
        "JOIN receipts r ON r.receipt_id = s.receipt_id "
        "JOIN carriers c ON c.carrier_id = s.carrier_id "
        "WHERE s.supplier_id=? AND s.supplier_batch=? AND s.sku=? "
        "AND s.valid_from <= ? AND (s.valid_to IS NULL OR s.valid_to > ?)")
    live_params: list = [query.supplier_id, query.supplier_batch,
                         query.sku, query.cutoff_seq, query.cutoff_seq]
    if query.receipt_id is not None:
        live_sql += " AND s.receipt_id=?"
        live_params.append(query.receipt_id)
    live_sql += " ORDER BY c.created_seq, s.carrier_id, s.pos"
    for row in _cursor_batches(conn, live_sql, live_params):
        base = _segment_record(conn, row, "live", local)
        if base is None:
            continue
        base.update({"record": "live", "status": row["status"],
                     "location_id": row["location_id"],
                     "carrier_created_seq": int(row["created_seq"])})
        live_total += base["length"]
        yield base

    ship_sql = (
        "SELECT p.*, r.qty AS receipt_qty FROM shipments p "
        "JOIN receipts r ON r.receipt_id = p.receipt_id "
        "WHERE p.supplier_id=? AND p.supplier_batch=? AND p.sku=? "
        "AND p.seq <= ?")
    ship_params: list = [query.supplier_id, query.supplier_batch,
                         query.sku, query.cutoff_seq]
    if query.receipt_id is not None:
        ship_sql += " AND p.receipt_id=?"
        ship_params.append(query.receipt_id)
    ship_sql += (" ORDER BY p.seq, p.shipment_document, p.carrier_id, p.pos")
    for row in _cursor_batches(conn, ship_sql, ship_params):
        base = _segment_record(conn, row, "shipped", local)
        if base is None:
            continue
        base.update({"record": "shipped",
                     "shipment_document": row["shipment_document"],
                     "recipient": row["recipient"],
                     "shipment_seq": int(row["seq"])})
        shipped_total += base["length"]
        yield base

    if received_total != live_total + shipped_total:
        raise LibraryError(
            "recall cross-foot broken: received "
            f"{received_total} != live {live_total} + shipped "
            f"{shipped_total} (library is internally inconsistent)")
    yield {"record": "totals", "received_total": received_total,
           "live_total": live_total, "shipped_total": shipped_total,
           "total": live_total + shipped_total}


def recall(library_dir: Path, query: RecallQuery) -> RecallReport:
    """Collect :func:`iter_recall` into a report (for small results/tests)."""
    conn = connect_ro(library_dir)
    if conn is None:
        raise LibraryError(f"no warehouse library at {library_dir}")
    report = RecallReport(
        query=query, library_path=str(Path(library_dir).resolve()),
        library_digest="", result_id="")
    try:
        for rec in _iter(conn, library_dir, query):
            kind = rec["record"]
            if kind == "header":
                report.library_digest = rec["library_digest"]
                report.result_id = rec["result_id"]
            elif kind == "receipt":
                report.receipts.append(
                    {k: rec[k] for k in ("receipt_id", "seq", "qty",
                                         "selected", "locator")})
            elif kind == "skipped_receipt":
                report.skipped_receipts.append(
                    {k: rec[k] for k in ("receipt_id", "seq", "qty",
                                         "reason")})
            elif kind == "live":
                report.live.append(
                    {k: v for k, v in rec.items() if k != "record"})
                report.live_total += rec["length"]
            elif kind == "shipped":
                report.shipped.append(
                    {k: v for k, v in rec.items() if k != "record"})
                report.shipped_total += rec["length"]
            elif kind == "totals":
                report.received_total = rec["received_total"]
                assert report.live_total == rec["live_total"]
                assert report.shipped_total == rec["shipped_total"]
    finally:
        conn.close()
    return report


def write_recall_json(report: RecallReport, path: Path) -> None:
    """Write the collected report as the standard JSON document."""
    Path(path).write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2,
                   sort_keys=True) + "\n", encoding="utf-8")


def write_recall_jsonl(library_dir: Path, query: RecallQuery,
                       path: Path) -> dict:
    """Stream a recall straight to JSONL and return the totals line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    totals = None
    with path.open("w", encoding="utf-8") as fh:
        for rec in iter_recall(library_dir, query):
            if rec["record"] == "totals":
                totals = rec
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
    return totals or {}
