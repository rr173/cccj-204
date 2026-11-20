"""Opening, path helpers and business digest for a warehouse library.

The schema itself lives in :mod:`bidfact.warehouse.build` (the only writer).
Readers open the file with SQLite's read-only URI mode, so a recall never
modifies the library and never needs the source package.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Optional

from .errors import LibraryError

SCHEMA_VERSION = "warehouse-lineage-1"


def library_path(library_dir: Path) -> Path:
    return Path(library_dir).resolve() / "warehouse.sqlite3"


def lock_path(library_dir: Path) -> Path:
    return Path(library_dir).resolve() / "warehouse.sqlite3.lock"


def connect_ro(library_dir: Path) -> Optional[sqlite3.Connection]:
    """Read-only connection, or ``None`` when no library exists."""
    path = library_path(library_dir)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=120)
    except sqlite3.Error as exc:
        raise LibraryError(f"cannot open {path} read-only: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise LibraryError(
            f"{path} is not a warehouse library: {exc}") from exc
    if row is None or row["value"] != SCHEMA_VERSION:
        conn.close()
        raise LibraryError(
            f"{path} holds schema "
            f"{row['value'] if row is not None else None!r}, expected "
            f"{SCHEMA_VERSION!r}")
    return conn


def library_digest(conn: sqlite3.Connection) -> str:
    """Business digest of the delivered library (stable across re-runs)."""
    h = hashlib.sha256(b"warehouse-lineage-digest-1\n")
    for key in ("package_id", "manifest_digest", "announcement_result_id",
                "announcement_store_digest"):
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        h.update(key.encode("utf-8"))
        h.update(b"=")
        h.update((row["value"] if row else "").encode("utf-8"))
        h.update(b"\n")
    for table, columns in (
            ("receipts", "seq, receipt_id, location_id, sku, supplier_id, "
                         "supplier_batch, qty, result_id, document_id, "
                         "project_id, section_id"),
            ("movements", "seq, movement_type, sku, recipient, "
                          "shipment_document"),
            ("movement_targets", "seq, target_id, location_id, status"),
            ("movement_mappings", "seq, idx, source_id, from_start, "
                                  "from_end, target_id, to_start, to_end"),
            ("stock_segments", "carrier_id, pos, length, status, location_id,"
                               " receipt_id, receipt_pos, valid_from, "
                               "valid_to"),
            ("shipments", "ship_id, seq, carrier_id, pos, length, "
                          "receipt_id, receipt_pos, recipient, "
                          "shipment_document")):
        # Order by the business columns, never by insertion rowid: the
        # same package with its lines shuffled must give the same digest.
        for row in conn.execute(
                f"SELECT {columns} FROM {table} ORDER BY {columns}"):
            h.update(repr(tuple(row)).encode("utf-8"))
            h.update(b"\n")
    return h.hexdigest()
