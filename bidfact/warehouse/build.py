"""Build a durable warehouse lineage library from a read-only package.

The build is *all or nothing*: every row and every movement is validated;
if any blocking issue remains, nothing is delivered and an existing usable
library is left byte-for-byte untouched.  An identical package reuses the
existing library; a different package is only replaced with ``--force``.

Movements are applied strictly in business-sequence order (never file
line order), each inside its own SQLite savepoint.  A movement that fails
is rolled back entirely: its targets are not created and its pieces stay
where they were, so a bad movement cannot leave a plausible-looking but
wrong recall dataset behind.

Storage is interval based.  ``stock_segments`` holds one row per live or
historical half-open piece segment with

* its carrier and local coordinates,
* the source receipt, supplier lot and status/location at that time,
* ``valid_from``/``valid_to`` business sequences,
* the single mapping hop that created it (``origin_*``), which lets a
  recall walk back to the receipt interval without replaying the package.

Segments are streamed in classification order; the build never expands a
long interval into per-piece rows and never loads all mappings into
Python.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from .errors import LibraryError, LibraryExists, WarehouseError
from .inputs import (ReceiptRow, SourceRef, WarehousePackage, hash_file,
                     load_manifest, parse_movement, parse_receipt)
from .store import (SCHEMA_VERSION, connect_ro, library_digest,
                    library_path, lock_path)

STATUS_AVAILABLE = "available"
STATUS_QUARANTINED = "quarantined"
STATUS_SHIPPED = "shipped"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (
    seq INTEGER NOT NULL, receipt_id TEXT PRIMARY KEY,
    location_id TEXT NOT NULL, sku TEXT NOT NULL,
    supplier_id TEXT NOT NULL, supplier_batch TEXT NOT NULL,
    qty INTEGER NOT NULL, result_id TEXT NOT NULL,
    document_id TEXT NOT NULL, project_id TEXT NOT NULL,
    section_id TEXT NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_receipts_lot
    ON receipts(supplier_id, supplier_batch, sku);
CREATE TABLE IF NOT EXISTS carriers (
    carrier_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL, created_seq INTEGER NOT NULL,
    receipt_id TEXT NOT NULL, sku TEXT NOT NULL,
    location_id TEXT, status TEXT NOT NULL, capacity INTEGER NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_carriers_receipt ON carriers(receipt_id);
CREATE TABLE IF NOT EXISTS movements (
    seq INTEGER PRIMARY KEY, movement_type TEXT NOT NULL, sku TEXT NOT NULL,
    recipient TEXT, shipment_document TEXT,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS movement_targets (
    seq INTEGER NOT NULL, target_id TEXT NOT NULL,
    location_id TEXT, status TEXT NOT NULL,
    PRIMARY KEY (seq, target_id)
);
CREATE TABLE IF NOT EXISTS movement_mappings (
    seq INTEGER NOT NULL, idx INTEGER NOT NULL,
    source_id TEXT NOT NULL, from_start INTEGER NOT NULL,
    from_end INTEGER NOT NULL, target_id TEXT NOT NULL,
    to_start INTEGER NOT NULL, to_end INTEGER NOT NULL,
    PRIMARY KEY (seq, idx)
);
CREATE INDEX IF NOT EXISTS ix_mappings_target ON movement_mappings(target_id);
CREATE INDEX IF NOT EXISTS ix_mappings_source ON movement_mappings(source_id);
CREATE TABLE IF NOT EXISTS stock_segments (
    seg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    carrier_id TEXT NOT NULL, pos INTEGER NOT NULL,
    length INTEGER NOT NULL, sku TEXT NOT NULL,
    supplier_id TEXT NOT NULL, supplier_batch TEXT NOT NULL,
    status TEXT NOT NULL, location_id TEXT, receipt_id TEXT NOT NULL,
    receipt_pos INTEGER NOT NULL,
    valid_from INTEGER NOT NULL, valid_to INTEGER,
    origin_carrier_id TEXT, origin_pos INTEGER,
    mapping_seq INTEGER, mapping_idx INTEGER
);
CREATE INDEX IF NOT EXISTS ix_segments_carrier_pos
    ON stock_segments(carrier_id, pos);
CREATE INDEX IF NOT EXISTS ix_segments_lot
    ON stock_segments(supplier_id, supplier_batch, sku, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS ix_segments_receipt
    ON stock_segments(receipt_id, valid_from, valid_to);
CREATE TABLE IF NOT EXISTS shipments (
    ship_id TEXT PRIMARY KEY, seq INTEGER NOT NULL,
    carrier_id TEXT NOT NULL, pos INTEGER NOT NULL, length INTEGER NOT NULL,
    sku TEXT NOT NULL, supplier_id TEXT NOT NULL,
    supplier_batch TEXT NOT NULL, receipt_id TEXT NOT NULL,
    receipt_pos INTEGER NOT NULL,
    recipient TEXT NOT NULL, shipment_document TEXT NOT NULL,
    origin_carrier_id TEXT, origin_pos INTEGER,
    mapping_seq INTEGER, mapping_idx INTEGER,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_shipments_lot
    ON shipments(supplier_id, supplier_batch, sku);
CREATE INDEX IF NOT EXISTS ix_shipments_receipt
    ON shipments(receipt_id);
CREATE TABLE IF NOT EXISTS movements_by_source (
    source_id TEXT NOT NULL, seq INTEGER NOT NULL,
    PRIMARY KEY (source_id, seq)
);
CREATE TABLE IF NOT EXISTS seq_owner (
    seq INTEGER PRIMARY KEY, owner_kind TEXT NOT NULL,
    owner_id TEXT NOT NULL, source_file TEXT NOT NULL,
    source_line INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL, message TEXT NOT NULL,
    source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS file_digests (
    name TEXT PRIMARY KEY, digest TEXT NOT NULL
);
"""


# ------------------------------------------------------------------ report
@dataclass
class BuildReport:
    ok: bool
    issues: list[dict]
    package_id: str
    result_id: str
    input_digest: str
    library_digest: Optional[str]
    path: Optional[str]
    reused: bool = False
    counts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "issues": self.issues,
            "package_id": self.package_id, "result_id": self.result_id,
            "input_digest": self.input_digest,
            "library_digest": self.library_digest, "path": self.path,
            "reused": self.reused, "counts": self.counts}


# ------------------------------------------------------------------ issues
class _IssueSink:
    """Streaming issue collector; deduplicates identical issues."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.seen: set[tuple] = set()

    def __call__(self, issue: dict) -> None:
        self.add(issue["code"], issue["source"], issue["message"],
                 issue.get("detail"))

    def add(self, code: str, ref, message: str,
            detail: Optional[dict] = None) -> None:
        if isinstance(ref, SourceRef):
            file, line = ref.file, ref.line
        else:
            file, line = ref["file"], ref["line"]
        key = (code, message, file, line,
               json.dumps(detail, ensure_ascii=False, sort_keys=True)
               if detail is not None else "")
        if key in self.seen:
            return
        self.seen.add(key)
        self.conn.execute(
            "INSERT INTO issues(code, message, source_file, source_line, "
            "detail_json) VALUES(?,?,?,?,?)",
            (code, message, file, int(line),
             json.dumps(detail, ensure_ascii=False, sort_keys=True)
             if detail is not None else None))


def _issues(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for row in conn.execute(
            "SELECT * FROM issues ORDER BY source_file, source_line, "
            "issue_id"):
        item = {"code": row["code"], "message": row["message"],
                "source": {"file": row["source_file"],
                           "line": int(row["source_line"])}}
        if row["detail_json"]:
            item["detail"] = json.loads(row["detail_json"])
        out.append(item)
    return out


# ----------------------------------------------------------- announcement
def _open_announcement(package: WarehousePackage) -> sqlite3.Connection:
    path = package.announcement_result_dir / "store.sqlite3"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=120)
    except sqlite3.Error as exc:
        raise WarehouseError(
            f"cannot open announcement store {path} read-only: {exc}"
        ) from exc
    conn.row_factory = sqlite3.Row
    return conn


def _check_locator(ann: sqlite3.Connection, row: ReceiptRow,
                   expected_result_id: str) -> Optional[str]:
    """Prove the receipt's section is registered in the cited result.

    This only proves the purchase scope exists.  It never interprets an
    award assertion, never looks at payment and never requires the notice
    to still be current.
    """
    if row.result_id != expected_result_id:
        return (f"result_id {row.result_id!r} does not match the opened "
                f"announcement store {expected_result_id!r}")
    doc = ann.execute(
        "SELECT document_id, project_id FROM documents WHERE document_id=?",
        (row.document_id,)).fetchone()
    if doc is None:
        return (f"document {row.document_id!r} does not exist in result "
                f"{row.result_id!r}")
    if doc["project_id"] != row.project_id:
        return (f"document {row.document_id!r} belongs to project "
                f"{doc['project_id']!r}, not {row.project_id!r}")
    section = ann.execute(
        "SELECT 1 FROM doc_sections WHERE document_id=? AND section_id=?",
        (row.document_id, row.section_id)).fetchone()
    if section is None:
        return (f"section {row.section_id!r} is not registered for document "
                f"{row.document_id!r}")
    return None


# --------------------------------------------------------------- staging
def _new_stage(directory: Path) -> tuple[sqlite3.Connection, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "warehouse-stage.sqlite3"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-4096")
    conn.executescript(_SCHEMA)
    return conn, path


def _iter_raw(path: Path) -> Iterator[tuple[int, bytes]]:
    with path.open("rb") as fh:
        for lineno, raw in enumerate(fh, 1):
            yield lineno, raw


def _read_jsonl(path: Path, rel: str, sink: _IssueSink, hasher):
    for lineno, raw in _iter_raw(path):
        hasher.update(raw)
        if not raw.strip():
            continue
        ref = SourceRef(rel, lineno)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            sink.add("line_not_json", ref,
                     f"line is not valid UTF-8 JSON: "
                     f"{getattr(exc, 'msg', str(exc))}")
            continue
        if not isinstance(value, dict):
            sink.add("line_not_object", ref,
                     "JSONL line must be one JSON object")
            continue
        yield ref, value


# --------------------------------------------------------------- receipts
def _stage_receipts(conn, package: WarehousePackage, sink: _IssueSink,
                    digests: dict[str, str], ann: sqlite3.Connection) -> None:
    rel = package.files["receipts"]
    path = package.directory / rel
    if not path.is_file():
        sink.add("file_missing", SourceRef(rel, 0),
                 f"declared input file {rel!r} does not exist")
        return
    h = hashlib.sha256()
    for ref, value in _read_jsonl(path, rel, sink, h):
        row = parse_receipt(value, ref, sink)
        if row is None:
            continue
        clash = conn.execute(
            "SELECT source_file, source_line FROM receipts WHERE receipt_id=?",
            (row.receipt_id,)).fetchone()
        if clash is not None:
            sink.add("duplicate_receipt_id", ref,
                     f"receipt {row.receipt_id!r} already appeared at "
                     f"{clash['source_file']}:{clash['source_line']}",
                     {"receipt_id": row.receipt_id,
                      "first_source": {"file": clash["source_file"],
                                       "line": int(clash["source_line"])}})
            continue
        owner = conn.execute(
            "SELECT owner_kind, owner_id FROM seq_owner WHERE seq=?",
            (row.seq,)).fetchone()
        if owner is not None:
            sink.add("duplicate_seq", ref,
                     f"business seq {row.seq} is already used by "
                     f"{owner['owner_kind']} {owner['owner_id']!r}",
                     {"seq": row.seq, "owner_kind": owner["owner_kind"],
                      "owner_id": owner["owner_id"]})
            continue
        reason = _check_locator(ann, row, package.announcement_result_id)
        if reason is not None:
            sink.add("receipt_locator_invalid", ref,
                     f"purchase locator for receipt {row.receipt_id!r}: "
                     f"{reason}",
                     {"receipt_id": row.receipt_id,
                      "result_id": row.result_id,
                      "document_id": row.document_id,
                      "project_id": row.project_id,
                      "section_id": row.section_id})
            continue
        conn.execute(
            "INSERT INTO receipts(seq, receipt_id, location_id, sku, "
            "supplier_id, supplier_batch, qty, result_id, document_id, "
            "project_id, section_id, source_file, source_line) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row.seq, row.receipt_id, row.location_id, row.sku,
             row.supplier_id, row.supplier_batch, row.qty, row.result_id,
             row.document_id, row.project_id, row.section_id, ref.file,
             ref.line))
        conn.execute(
            "INSERT INTO carriers(carrier_id, kind, created_seq, receipt_id, "
            "sku, location_id, status, capacity, source_file, source_line) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (row.receipt_id, "receipt", row.seq, row.receipt_id, row.sku,
             row.location_id, STATUS_AVAILABLE, row.qty, ref.file, ref.line))
        conn.execute(
            "INSERT INTO stock_segments(carrier_id, pos, length, sku, "
            "supplier_id, supplier_batch, status, location_id, receipt_id, "
            "receipt_pos, valid_from, valid_to) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (row.receipt_id, 0, row.qty, row.sku, row.supplier_id,
             row.supplier_batch, STATUS_AVAILABLE, row.location_id,
             row.receipt_id, 0, row.seq))
        conn.execute(
            "INSERT INTO seq_owner(seq, owner_kind, owner_id, source_file, "
            "source_line) VALUES(?,?,?,?,?)",
            (row.seq, "receipt", row.receipt_id, ref.file, ref.line))
    digests[rel] = h.hexdigest()


# -------------------------------------------------------------- movements
def _stage_movements(conn, package: WarehousePackage, sink: _IssueSink,
                     digests: dict[str, str]) -> None:
    rel = package.files["movements"]
    path = package.directory / rel
    if not path.is_file():
        sink.add("file_missing", SourceRef(rel, 0),
                 f"declared input file {rel!r} does not exist")
        return
    h = hashlib.sha256()
    for ref, value in _read_jsonl(path, rel, sink, h):
        row = parse_movement(value, ref, sink)
        if row is None:
            continue
        owner = conn.execute(
            "SELECT owner_kind, owner_id, source_file, source_line "
            "FROM seq_owner WHERE seq=?", (row.seq,)).fetchone()
        if owner is not None:
            sink.add("duplicate_seq", ref,
                     f"business seq {row.seq} is already used by "
                     f"{owner['owner_kind']} {owner['owner_id']!r} at "
                     f"{owner['source_file']}:{owner['source_line']}",
                     {"seq": row.seq, "owner_kind": owner["owner_kind"],
                      "owner_id": owner["owner_id"]})
            continue
        conn.execute(
            "INSERT INTO movements(seq, movement_type, sku, recipient, "
            "shipment_document, source_file, source_line) "
            "VALUES(?,?,?,?,?,?,?)",
            (row.seq, row.movement_type, row.sku, row.recipient,
             row.shipment_document, ref.file, ref.line))
        conn.execute(
            "INSERT INTO seq_owner(seq, owner_kind, owner_id, source_file, "
            "source_line) VALUES(?,?,?,?,?)",
            (row.seq, "movement", f"movement:{row.seq}", ref.file, ref.line))
        for target in row.targets:
            conn.execute(
                "INSERT INTO movement_targets(seq, target_id, location_id, "
                "status) VALUES(?,?,?,?)",
                (row.seq, target.target_id, target.location_id,
                 target.status or ""))
        for idx, mp in enumerate(row.mappings):
            conn.execute(
                "INSERT INTO movement_mappings(seq, idx, source_id, "
                "from_start, from_end, target_id, to_start, to_end) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (row.seq, idx, mp.source_id, mp.from_start, mp.from_end,
                 mp.target_id, mp.to_start, mp.to_end))
            conn.execute(
                "INSERT OR IGNORE INTO movements_by_source(source_id, seq) "
                "VALUES(?,?)", (mp.source_id, row.seq))
    digests[rel] = h.hexdigest()


# --------------------------------------------------- structural prechecks
def _movement_ref(conn, seq: int) -> SourceRef:
    row = conn.execute(
        "SELECT source_file, source_line FROM movements WHERE seq=?",
        (seq,)).fetchone()
    return SourceRef(row["source_file"], int(row["source_line"]))


def _structural_checks(conn, sink: _IssueSink) -> set[int]:
    """Check target coverage, source overlaps and carrier-id freshness.

    Returns the set of movement sequences that must not be applied.  A
    movement with a structural defect is reported once and skipped, so a
    bad row can never half-create a target.
    """
    bad: set[int] = set()

    # Target coordinates: one segment per target must start at zero and
    # chain exactly; gaps and overlaps are both fatal.
    for row in conn.execute(
            "SELECT seq, target_id, to_start, prev_end, "
            "       ROW_NUMBER() OVER (PARTITION BY seq, target_id "
            "                          ORDER BY to_start, idx) AS rn "
            "FROM (SELECT seq, idx, target_id, to_start, "
            "             LAG(to_end) OVER (PARTITION BY seq, target_id "
            "                             ORDER BY to_start, idx) "
            "             AS prev_end "
            "      FROM movement_mappings) "
            "ORDER BY seq, target_id, rn"):
        seq = int(row["seq"])
        expected = 0 if row["rn"] == 1 else row["prev_end"]
        if row["to_start"] != expected:
            if seq not in bad:
                bad.add(seq)
                sink.add("target_not_exactly_covered", _movement_ref(conn, seq),
                         f"movement seq {seq}: target {row['target_id']!r} "
                         f"starts at {int(row['to_start'])} where "
                         f"{expected} was required; a new carrier must be "
                         "covered exactly from zero with no gap or overlap",
                         {"seq": seq, "target_id": row["target_id"],
                          "to_start": int(row["to_start"]),
                          "expected": int(expected)})

    # Two mappings of one movement may not take the same piece twice.
    for row in conn.execute(
            "SELECT seq, source_id, from_start, prev_end "
            "FROM (SELECT seq, idx, source_id, from_start, "
            "             LAG(from_end) OVER (PARTITION BY seq, source_id "
            "                                ORDER BY from_start, idx) "
            "             AS prev_end "
            "      FROM movement_mappings) "
            "WHERE prev_end IS NOT NULL AND from_start < prev_end "
            "ORDER BY seq, source_id, from_start"):
        seq = int(row["seq"])
        if seq in bad:
            continue
        bad.add(seq)
        sink.add("source_consumed_twice_in_movement",
                 _movement_ref(conn, seq),
                 f"movement seq {seq}: source {row['source_id']!r} is taken "
                 f"at [{int(row['from_start'])}, …) while an earlier mapping "
                 f"already took up to {int(row['prev_end'])}",
                 {"seq": seq, "source_id": row["source_id"]})

    # A target id must be fresh: never a receipt id, never produced by a
    # second movement, never consumed by a movement with a lower sequence.
    for row in conn.execute(
            "SELECT t.target_id, MIN(t.seq) AS first_seq, MAX(t.seq) AS "
            "last_seq, COUNT(DISTINCT t.seq) AS n FROM movement_targets t "
            "GROUP BY t.target_id HAVING COUNT(DISTINCT t.seq) > 1"):
        first_seq = int(row["first_seq"])
        for other in conn.execute(
                "SELECT seq FROM movement_targets WHERE target_id=? AND "
                "seq != ? ORDER BY seq", (row["target_id"], first_seq)):
            seq = int(other["seq"])
            if seq in bad:
                continue
            bad.add(seq)
            sink.add("target_id_reused", _movement_ref(conn, seq),
                     f"carrier id {row['target_id']!r} is created again by "
                     f"movement seq {seq} (first created by seq {first_seq}); "
                     "a carrier id is never reused",
                     {"seq": seq, "target_id": row["target_id"],
                      "first_seq": first_seq})
    for row in conn.execute(
            "SELECT t.seq, t.target_id FROM movement_targets t "
            "JOIN receipts r ON r.receipt_id = t.target_id "
            "ORDER BY t.seq"):
        seq = int(row["seq"])
        if seq in bad:
            continue
        bad.add(seq)
        sink.add("target_reuses_receipt_id", _movement_ref(conn, seq),
                 f"movement seq {seq} reuses receipt "
                 f"{row['target_id']!r} as a new carrier id",
                 {"seq": seq, "target_id": row["target_id"]})
    return bad


# --------------------------------------------------------------- applying
class MovementError(Exception):
    """One movement cannot be applied; carries a located issue."""

    def __init__(self, issue: dict):
        self.issue = issue
        super().__init__(issue["message"])


def _carrier(conn, carrier_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM carriers WHERE carrier_id=?",
        (carrier_id,)).fetchone()


def _live_rows(conn, carrier_id: str, start: int, end: int):
    return conn.execute(
        "SELECT * FROM stock_segments WHERE carrier_id=? AND "
        "valid_to IS NULL AND pos < ? AND pos + length > ? ORDER BY pos",
        (carrier_id, end, start))


def _fail(code: str, ref: SourceRef, message: str, **detail):
    raise MovementError({"code": code, "message": message,
                         "source": ref.as_dict(), "detail": detail})


def _apply_movement(conn, mrow: sqlite3.Row, sink: _IssueSink) -> None:
    seq = int(mrow["seq"])
    ref = SourceRef(mrow["source_file"], int(mrow["source_line"]))
    mtype = mrow["movement_type"]
    sku = mrow["sku"]

    maps = list(conn.execute(
        "SELECT * FROM movement_mappings WHERE seq=? ORDER BY idx", (seq,)))
    targets = list(conn.execute(
        "SELECT * FROM movement_targets WHERE seq=? ORDER BY target_id",
        (seq,)))

    # -- source carriers: existence, business order, SKU ---------------
    source_ids: list[str] = []
    for mp in maps:
        if mp["source_id"] not in source_ids:
            source_ids.append(mp["source_id"])
    for sid in source_ids:
        carrier = _carrier(conn, sid)
        if carrier is None:
            _fail("unknown_source_carrier", ref,
                  f"movement seq {seq} consumes {sid!r}, which does not exist",
                  seq=seq, source_id=sid)
        if int(carrier["created_seq"]) >= seq:
            _fail("source_carrier_not_yet_created", ref,
                  f"movement seq {seq} consumes {sid!r}, created by sequence "
                  f"{int(carrier['created_seq'])}; only earlier carriers can "
                  "be consumed", seq=seq, source_id=sid,
                  created_seq=int(carrier["created_seq"]))
        if carrier["status"] == STATUS_SHIPPED:
            _fail("source_already_shipped", ref,
                  f"movement seq {seq} consumes {sid!r}, which was already "
                  "shipped and is a terminal record",
                  seq=seq, source_id=sid)
        if carrier["sku"] != sku:
            _fail("movement_sku_mismatch", ref,
                  f"movement seq {seq} handles SKU {sku!r} but source "
                  f"carrier {sid!r} holds {carrier['sku']!r}",
                  seq=seq, source_id=sid)

    # -- consume every mapping interval, mapping bounds first -----------
    consumed: list[tuple[str, int, int, sqlite3.Row, str, int, int]] = []
    for mp in maps:
        a, b = int(mp["from_start"]), int(mp["from_end"])
        sid = mp["source_id"]
        carrier = _carrier(conn, sid)
        if carrier is not None and b > int(carrier["capacity"]):
            _fail("mapping_out_of_bounds", ref,
                  f"movement seq {seq} maps [{a}, {b}) of {sid!r}, whose "
                  f"carrier only has {int(carrier['capacity'])} pieces",
                  seq=seq, index=int(mp["idx"]), source_id=sid, to=b,
                  capacity=int(carrier["capacity"]))
        rows = list(_live_rows(conn, sid, a, b))
        any_overlap = bool(rows)
        cursor = a
        found = []
        for row in rows:
            piece_start = int(row["pos"])
            piece_end = piece_start + int(row["length"])
            if piece_start > cursor:
                break
            x = max(piece_start, a)
            y = min(piece_end, b)
            if x > cursor:
                break
            if x == cursor and y > x:
                found.append((row, x, y))
                cursor = y
            if cursor >= b:
                break
        if cursor != b:
            if not any_overlap:
                code = "source_interval_not_in_stock"
                message = (f"movement seq {seq} mapping {int(mp['idx'])} "
                           f"needs [{a}, {b}) of {sid!r}, but none of those "
                           "pieces is still in stock at this sequence")
            else:
                code = "source_interval_partly_consumed"
                message = (f"movement seq {seq} mapping {int(mp['idx'])} "
                           f"needs [{a}, {b}) of {sid!r}, but pieces from "
                           f"{cursor} onward are no longer in stock; a partly "
                           "consumed interval is rejected, never shortened")
            _fail(code, ref, message, seq=seq, index=int(mp["idx"]),
                  source_id=sid, from_start=a, from_end=b,
                  missing_from=cursor)
        for row, x, y in found:
            consumed.append((sid, x, y, row, mp["target_id"],
                             int(mp["idx"]), seq))

    # -- status and location rules -------------------------------------
    if mtype in ("quarantine", "ship"):
        bad = [p for p in consumed if p[3]["status"] != STATUS_AVAILABLE]
        if bad:
            _fail("pieces_not_available", ref,
                  f"movement seq {seq} ({mtype}) needs available pieces, but "
                  f"{len(bad)} piece(s) are quarantined",
                  seq=seq, movement_type=mtype)
    elif mtype == "release":
        bad = [p for p in consumed if p[3]["status"] != STATUS_QUARANTINED]
        if bad:
            _fail("pieces_not_quarantined", ref,
                  f"movement seq {seq} (release) needs quarantined pieces, "
                  f"but {len(bad)} piece(s) are available",
                  seq=seq, movement_type=mtype)
    locations = {p[3]["location_id"] for p in consumed}
    if mtype != "transfer" and len(locations) > 1:
        _fail("movement_changes_location", ref,
              f"movement seq {seq} ({mtype}) combines pieces from locations "
              f"{sorted(locations)}; only a transfer may change the location",
              seq=seq, locations=sorted(locations))
    target_location = None
    if mtype in ("repack", "quarantine", "release"):
        target_location = next(iter(locations)) if locations else None

    # -- rewrite consumed source segments -------------------------------
    def _segment(carrier_id, pos, length, row, receipt_pos, valid_from,
                 valid_to, origin_carrier_id, origin_pos):
        conn.execute(
            "INSERT INTO stock_segments(carrier_id, pos, length, sku, "
            "supplier_id, supplier_batch, status, location_id, receipt_id, "
            "receipt_pos, valid_from, valid_to, origin_carrier_id, "
            "origin_pos, mapping_seq, mapping_idx) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (carrier_id, pos, length, row["sku"], row["supplier_id"],
             row["supplier_batch"], row["status"], row["location_id"],
             row["receipt_id"], receipt_pos, valid_from, valid_to,
             row["origin_carrier_id"], row["origin_pos"],
             row["mapping_seq"], row["mapping_idx"]))

    # Several mappings of one movement may take different slices of the
    # same live segment, so group the rewrites per original row and do
    # the split once.  A per-mapping delete/re-insert would duplicate the
    # still-live remainder.
    by_row: dict[int, tuple[sqlite3.Row, list[tuple[int, int]]]] = {}
    order: list[int] = []
    for sid, x, y, row, _tid, _idx, _mseq in consumed:
        key = int(row["seg_id"])
        if key not in by_row:
            by_row[key] = (row, [])
            order.append(key)
        by_row[key][1].append((x, y))
    for key in order:
        row, intervals = by_row[key]
        intervals.sort()
        pos = int(row["pos"])
        end = pos + int(row["length"])
        valid_from = int(row["valid_from"])
        receipt_pos = int(row["receipt_pos"])
        origin_id = row["origin_carrier_id"]
        origin_pos = row["origin_pos"]
        conn.execute("DELETE FROM stock_segments WHERE seg_id=?", (key,))
        cursor = pos
        for x, y in intervals:
            if x > cursor:
                _segment(row["carrier_id"], cursor, x - cursor, row,
                         receipt_pos + (cursor - pos), valid_from, None,
                         origin_id, origin_pos)
            _segment(row["carrier_id"], x, y - x, row,
                     receipt_pos + (x - pos), valid_from, seq,
                     origin_id, origin_pos)
            cursor = max(cursor, y)
        if cursor < end:
            _segment(row["carrier_id"], cursor, end - cursor, row,
                     receipt_pos + (cursor - pos), valid_from, None,
                     origin_id, origin_pos)

    # -- create targets --------------------------------------------------
    consumed_skus = {p[3]["sku"] for p in consumed}
    if consumed_skus != {sku}:
        _fail("movement_sku_mismatch", ref,
              f"movement seq {seq} handles SKU {sku!r} but consumed pieces "
              f"of {sorted(consumed_skus)}; one movement handles one SKU",
              seq=seq, skus=sorted(consumed_skus))
    for target in targets:
        tid = target["target_id"]
        capacity = max(
            (int(mp["to_end"]) for mp in maps if mp["target_id"] == tid),
            default=0)
        if target["status"]:
            status = target["status"]
        elif mtype == "quarantine":
            status = STATUS_QUARANTINED
        elif mtype == "release":
            status = STATUS_AVAILABLE
        elif mtype == "ship":
            status = STATUS_SHIPPED
        else:
            status = None  # per-piece (repack/transfer preserve status)
        if conn.execute("SELECT 1 FROM carriers WHERE carrier_id=?",
                        (tid,)).fetchone() is not None:
            _fail("target_id_reused", ref,
                  f"movement seq {seq} creates {tid!r}, which already exists "
                  "as a carrier", seq=seq, target_id=tid)
        conn.execute(
            "INSERT INTO carriers(carrier_id, kind, created_seq, receipt_id, "
            "sku, location_id, status, capacity, source_file, source_line) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tid, "movement", seq, "", sku,
             target["location_id"] if mtype == "transfer" else target_location,
             status if status else STATUS_AVAILABLE, capacity,
             ref.file, ref.line))

        for mp in maps:
            if mp["target_id"] != tid:
                continue
            a = int(mp["from_start"])
            to = int(mp["to_start"])
            for sid, x, y, row, ptid, idx, mseq in consumed:
                if ptid != tid or sid != mp["source_id"]:
                    continue
                if y <= a or x >= int(mp["from_end"]):
                    continue
                ox = max(x, a)
                oy = min(y, int(mp["from_end"]))
                length = oy - ox
                if length <= 0:
                    continue
                tstart = to + (ox - a)
                if mtype == "ship":
                    ship_id = f"{seq}:{tid}:{tstart}"
                    conn.execute(
                        "INSERT INTO shipments(ship_id, seq, carrier_id, "
                        "pos, length, sku, supplier_id, supplier_batch, "
                        "receipt_id, receipt_pos, recipient, "
                        "shipment_document, origin_carrier_id, origin_pos, "
                        "mapping_seq, mapping_idx, source_file, source_line) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ship_id, seq, tid, tstart, length, sku,
                         row["supplier_id"], row["supplier_batch"],
                         row["receipt_id"],
                         int(row["receipt_pos"]) + (ox - int(row["pos"])),
                         mrow["recipient"], mrow["shipment_document"], sid,
                         ox, seq, idx, ref.file, ref.line))
                else:
                    piece_status = status if status else row["status"]
                    conn.execute(
                        "INSERT INTO stock_segments(carrier_id, pos, length, "
                        "sku, supplier_id, supplier_batch, status, "
                        "location_id, receipt_id, receipt_pos, valid_from, "
                        "valid_to, origin_carrier_id, origin_pos, "
                        "mapping_seq, mapping_idx) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
                        (tid, tstart, length, sku, row["supplier_id"],
                         row["supplier_batch"], piece_status,
                         target_location if mtype != "transfer"
                         else target["location_id"],
                         row["receipt_id"],
                         int(row["receipt_pos"]) + (ox - int(row["pos"])),
                         seq, sid, ox, seq, idx))


def _apply_all(conn, bad: set[int], sink: _IssueSink) -> None:
    """Apply movements in business-sequence order with bounded paging."""
    last = 0
    while True:
        batch = conn.execute(
            "SELECT * FROM movements WHERE seq > ? ORDER BY seq LIMIT 64",
            (last,)).fetchall()
        if not batch:
            return
        for mrow in batch:
            seq = int(mrow["seq"])
            last = seq
            if seq in bad:
                continue
            conn.execute("SAVEPOINT movement")
            try:
                _apply_movement(conn, mrow, sink)
                conn.execute("RELEASE movement")
            except MovementError as exc:
                conn.execute("ROLLBACK TO movement")
                conn.execute("RELEASE movement")
                issue = exc.issue
                sink.add(issue["code"], issue["source"], issue["message"],
                         issue.get("detail"))


# ------------------------------------------------------------- crossfoot
def _crossfoot(conn, sink: _IssueSink) -> None:
    """Live pieces + shipped pieces must equal every receipt's quantity."""
    for row in conn.execute(
            "SELECT r.receipt_id, r.qty, r.source_file, r.source_line, "
            "  COALESCE((SELECT SUM(length) FROM stock_segments s "
            "            WHERE s.receipt_id=r.receipt_id AND "
            "                  s.valid_to IS NULL), 0) AS live, "
            "  COALESCE((SELECT SUM(length) FROM shipments p "
            "            WHERE p.receipt_id=r.receipt_id), 0) AS shipped "
            "FROM receipts r ORDER BY r.receipt_id"):
        total = int(row["live"]) + int(row["shipped"])
        if total != int(row["qty"]):
            sink.add("piece_conservation_broken",
                     SourceRef(row["source_file"], int(row["source_line"])),
                     f"receipt {row['receipt_id']!r}: {row['qty']} pieces "
                     f"received but {row['live']} live + {row['shipped']} "
                     "shipped do not add up",
                     {"receipt_id": row["receipt_id"],
                      "qty": int(row["qty"]), "live": int(row["live"]),
                      "shipped": int(row["shipped"])})


def _counts(conn) -> dict:
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM receipts) AS receipts, "
        "(SELECT COUNT(*) FROM movements) AS movements, "
        "(SELECT COUNT(*) FROM carriers) AS carriers, "
        "(SELECT COUNT(*) FROM stock_segments WHERE valid_to IS NULL) "
        "AS live_segments, "
        "(SELECT COUNT(*) FROM shipments) AS shipments, "
        "(SELECT COALESCE(SUM(qty),0) FROM receipts) AS received_pieces"
    ).fetchone()
    return {k: int(row[k]) for k in row.keys()}


# ------------------------------------------------------------------ build
def _package_digest(package: WarehousePackage, digests: dict[str, str]
                    ) -> str:
    h = hashlib.sha256(b"bidfact-warehouse-input-v1\n")
    for part in (package.package_id, package.manifest_digest,
                 package.announcement_result_id,
                 package.announcement_store_digest):
        h.update(part.encode("utf-8"))
        h.update(b"\n")
    for name in sorted(digests):
        h.update(name.encode("utf-8"))
        h.update(b":")
        h.update(digests[name].encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


@contextlib.contextmanager
def _exclusive_lock(library_dir: Path) -> Iterator[None]:
    path = lock_path(library_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def build_library(package_dir: Path, library_dir: Path,
                  force: bool = False) -> BuildReport:
    """Validate a package and deliver an atomic warehouse library.

    The staging database lives in a private directory next to the final
    file (same filesystem) and is moved into place with one ``os.replace``
    only after every validation has passed.  The scratch directory is
    removed on every exit path, including a blocked build.
    """
    package = load_manifest(package_dir)
    library_dir = Path(library_dir).resolve()
    library_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".build-", dir=library_dir))
    stage_path = tmp / "warehouse-stage.sqlite3"
    try:
        conn, _ = _new_stage(tmp)
        ann = _open_announcement(package)
        digests: dict[str, str] = {}
        try:
            conn.execute("BEGIN")
            sink = _IssueSink(conn)
            _stage_receipts(conn, package, sink, digests, ann)
            _stage_movements(conn, package, sink, digests)
            bad = _structural_checks(conn, sink)
            _apply_all(conn, bad, sink)
            _crossfoot(conn, sink)
            input_digest = _package_digest(package, digests)
            issues = _issues(conn)
            if not issues:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                    ("schema_version", SCHEMA_VERSION))
                for key, value in (
                        ("package_id", package.package_id),
                        ("manifest_digest", package.manifest_digest),
                        ("announcement_result_id",
                         package.announcement_result_id),
                        ("announcement_store_digest",
                         package.announcement_store_digest),
                        ("input_digest", input_digest)):
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                        (key, value))
                for name, digest in digests.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO file_digests(name, digest) "
                        "VALUES(?,?)", (name, digest))
            conn.execute("COMMIT")
            if issues:
                return BuildReport(
                    ok=False, issues=issues, package_id=package.package_id,
                    result_id=package.announcement_result_id,
                    input_digest=input_digest, library_digest=None,
                    path=None, counts=_counts(conn))
            digest = library_digest(conn)
            counts = _counts(conn)
        finally:
            ann.close()
            conn.close()

        # Prove the inputs did not change while being read.
        if hash_file(package.directory / "manifest.json") != \
                package.manifest_digest:
            raise WarehouseError(
                "manifest.json changed while the library was being built; "
                "no library was delivered")
        if hash_file(package.announcement_result_dir / "store.sqlite3") != \
                package.announcement_store_digest:
            raise WarehouseError(
                "the announcement store changed while the library was being "
                "built; no library was delivered")
        for name, digest_value in digests.items():
            if hash_file(package.directory / name) != digest_value:
                raise WarehouseError(
                    f"input file {name!r} changed while the library was "
                    "being built; no library was delivered")

        final_path = library_path(library_dir)
        with _exclusive_lock(library_dir):
            if final_path.is_file():
                try:
                    existing = connect_ro(library_dir)
                except LibraryError:
                    existing = None
                if existing is not None:
                    try:
                        existing_digest = library_digest(existing)
                        existing_input = existing.execute(
                            "SELECT value FROM meta WHERE key='input_digest'"
                        ).fetchone()["value"]
                        existing_counts = _counts(existing)
                    finally:
                        existing.close()
                    if existing_input == input_digest:
                        return BuildReport(
                            ok=True, issues=[], package_id=package.package_id,
                            result_id=package.announcement_result_id,
                            input_digest=input_digest,
                            library_digest=existing_digest,
                            path=str(final_path), reused=True,
                            counts=existing_counts)
                    if not force:
                        raise LibraryExists(str(final_path), existing_digest,
                                           digest)
                elif not force:
                    raise LibraryError(
                        f"{final_path} exists but is not a usable warehouse "
                        "library; pass --force to replace it")
            os.replace(str(stage_path), str(final_path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return BuildReport(
        ok=True, issues=[], package_id=package.package_id,
        result_id=package.announcement_result_id, input_digest=input_digest,
        library_digest=digest, path=str(final_path), reused=False,
        counts=counts)


__all__ = ["BuildReport", "build_library", "library_path", "connect_ro",
           "library_digest"]
