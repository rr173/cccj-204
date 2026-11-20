"""Package manifest and streaming JSONL readers for warehouse lineage.

A *warehouse package* is one directory describing a warehouse's receipts
and internal movements::

    manifest.json
    receipts.jsonl      one receipt (载体记录) per line
    movements.jsonl     one movement (流转) per line
    announcement_result_dir  -> existing bidfact result store (read-only)

Manifest::

    {
      "package_id": "WH-2024-11",
      "files": {"receipts": "receipts.jsonl",
                "movements": "movements.jsonl"},
      "announcement_result_dir": "../announcement_results/R…"
    }

Receipt row (``kind: "receipt"``)::

    {kind, seq, receipt_id, location_id, sku, supplier_id,
     supplier_batch, qty, result_id, document_id, project_id, section_id}

* ``seq`` is the receipt's positive business sequence number.  Receipts and
  movements share one sequence space; numbers are unique and need not be
  contiguous.  The numbering — never the file line — orders the package.
* ``qty`` is a positive integer piece count.  The receipt's pieces are the
  half-open interval ``[0, qty)`` in its own coordinates.
* ``(supplier_id, supplier_batch, sku)`` is the recall key.  Two receipts
  may share a supplier batch number; they remain distinct sources.

Movement row (``kind: "movement"``)::

    {kind, seq, movement_type, sku, targets, mappings,
     recipient?, shipment_document?}

    movement_type: repack | transfer | quarantine | release | ship
    targets: [{target_id, location_id?, status?}, ...]
    mappings: [{source_id, from_start, from_end,
                target_id, to_start, to_end}, ...]

* Every movement declares its new carrier(s) in ``targets`` and gives the
  old↔new interval correspondences in ``mappings``.  ``from``/``to`` pairs
  must be equal length; order within a segment is preserved by offset.  One
  movement handles exactly one SKU.
* Target coordinates must start at zero and be covered exactly, with no gap
  and no overlap.  A target id is fresh: it may not repeat a receipt id, a
  target of another movement, or a target of the same movement.
* ``repack`` keeps both location and isolation status; ``transfer`` names a
  new ``location_id`` and keeps the status; ``quarantine``/``release`` keep
  the location and flip the status; ``ship`` takes available pieces only and
  must give ``recipient`` and ``shipment_document``.  The per-type rules are
  enforced by :func:`parse_movement` and again against live stock during the
  build.
* Only pieces that exist and are still in stock may be consumed; the
  remainder keeps its old coordinates (it is never compacted).

All ids and codes are compared case-sensitively by Unicode code point.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ..fund.money import MoneyError, check_interval, parse_positive_quantity
from .errors import PackageError

RECEIPT_KEYS = ("kind", "seq", "receipt_id", "location_id", "sku",
                "supplier_id", "supplier_batch", "qty", "result_id",
                "document_id", "project_id", "section_id")
MOVEMENT_KEYS = ("kind", "seq", "movement_type", "sku", "targets", "mappings")
MOVEMENT_TYPES = ("repack", "transfer", "quarantine", "release", "ship")
STATUS_AVAILABLE = "available"
STATUS_QUARANTINED = "quarantined"
STATUSES = (STATUS_AVAILABLE, STATUS_QUARANTINED)


@dataclass(frozen=True, order=True)
class SourceRef:
    """Where one input row was read from (1-based JSONL line; 0 = file)."""

    file: str
    line: int

    def as_dict(self) -> dict:
        return {"file": self.file, "line": self.line}


@dataclass(frozen=True)
class ReceiptRow:
    seq: int
    receipt_id: str
    location_id: str
    sku: str
    supplier_id: str
    supplier_batch: str
    qty: int
    result_id: str
    document_id: str
    project_id: str
    section_id: str
    source: SourceRef


@dataclass(frozen=True)
class TargetRow:
    target_id: str
    location_id: Optional[str]
    status: Optional[str]
    source: SourceRef


@dataclass(frozen=True)
class MappingRow:
    source_id: str
    from_start: int
    from_end: int
    target_id: str
    to_start: int
    to_end: int

    @property
    def length(self) -> int:
        return self.from_end - self.from_start


@dataclass(frozen=True)
class MovementRow:
    seq: int
    movement_type: str
    sku: str
    targets: tuple[TargetRow, ...]
    mappings: tuple[MappingRow, ...]
    recipient: Optional[str]
    shipment_document: Optional[str]
    source: SourceRef


@dataclass
class WarehousePackage:
    directory: Path
    package_id: str
    files: dict[str, str]
    announcement_result_dir: Path
    manifest_digest: str
    announcement_store_digest: str
    announcement_result_id: str


# --------------------------------------------------------------- utilities
def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _relative_name(value: object, what: str) -> str:
    if (not isinstance(value, str) or not value
            or Path(value).is_absolute() or ".." in Path(value).parts):
        raise PackageError(
            f"{what} must be a relative file name, got {value!r}")
    return value


def read_result_id(store_path: Path) -> str:
    """Open the announcement store read-only and read its result number."""
    try:
        conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise PackageError(
            f"cannot open announcement store {store_path} read-only: {exc}"
        ) from exc
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='result_id'").fetchone()
    except sqlite3.Error as exc:
        raise PackageError(
            f"{store_path} is not a bidfact result store: {exc}") from exc
    finally:
        conn.close()
    if row is None or not row[0]:
        raise PackageError(
            f"{store_path} has no result_id; only bidfact result stores "
            "are accepted as section evidence")
    return row[0]


def load_manifest(directory: Path) -> WarehousePackage:
    """Read the manifest only; row files are streamed later, read-only."""
    directory = Path(directory).resolve()
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise PackageError(f"missing warehouse manifest: {manifest_path}")
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(
            f"{manifest_path}: not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PackageError(f"{manifest_path}: manifest must be a JSON object")

    package_id = manifest.get("package_id")
    if not isinstance(package_id, str) or not package_id:
        raise PackageError("manifest.package_id must be a non-empty string")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise PackageError("manifest.files must be an object kind -> file")
    for kind, name in files.items():
        if kind not in ("receipts", "movements"):
            raise PackageError(f"manifest.files has unknown kind {kind!r}")
        _relative_name(name, f"manifest.files[{kind!r}]")
    for kind in ("receipts", "movements"):
        if kind not in files:
            raise PackageError(f"manifest.files must declare the {kind} file")

    ann = manifest.get("announcement_result_dir")
    if not isinstance(ann, str) or not ann:
        raise PackageError(
            "manifest.announcement_result_dir must be a path")
    ann_path = (directory / ann).resolve()
    store_path = ann_path / "store.sqlite3"
    if not store_path.is_file():
        raise PackageError(
            f"announcement_result_dir {ann!r} has no store.sqlite3")

    return WarehousePackage(
        directory=directory, package_id=package_id, files=files,
        announcement_result_dir=ann_path,
        manifest_digest=hashlib.sha256(raw).hexdigest(),
        announcement_store_digest=hash_file(store_path),
        announcement_result_id=read_result_id(store_path))


# --------------------------------------------------------------- streaming
def make_issue(code: str, ref: SourceRef, message: str,
               detail: Optional[dict] = None) -> dict:
    payload = {"code": code, "message": message, "source": ref.as_dict()}
    if detail:
        payload["detail"] = detail
    return payload


# ----------------------------------------------------------------- parsing
def _string(row: dict, key: str, fail) -> Optional[str]:
    value = row.get(key)
    if not isinstance(value, str) or value == "":
        fail("schema_bad_field",
             f"{key} must be a non-empty string, got {value!r}",
             {"field": key, "value": value})
        return None
    return value


def _require(row: dict, keys, kind: str, fail) -> bool:
    for key in keys:
        if key not in row:
            fail("schema_missing_field",
                 f"{kind} row is missing {key}", {"field": key})
            return False
    return True


def _parse_seq(row: dict, fail) -> Optional[int]:
    seq = row.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
        fail("bad_seq", f"seq must be a positive integer, got {seq!r}",
             {"field": "seq", "value": seq})
        return None
    return seq


def parse_receipt(row: dict, ref: SourceRef, sink) -> Optional[ReceiptRow]:
    def fail(code: str, message: str, detail: Optional[dict] = None):
        sink(make_issue(code, ref, message, detail))

    if not _require(row, RECEIPT_KEYS, "receipt", fail):
        return None
    if row.get("kind") != "receipt":
        fail("row_kind_mismatch",
             f"receipt row has kind {row.get('kind')!r}, expected 'receipt'")
        return None
    seq = _parse_seq(row, fail)
    receipt_id = _string(row, "receipt_id", fail)
    location_id = _string(row, "location_id", fail)
    sku = _string(row, "sku", fail)
    supplier_id = _string(row, "supplier_id", fail)
    supplier_batch = _string(row, "supplier_batch", fail)
    result_id = _string(row, "result_id", fail)
    document_id = _string(row, "document_id", fail)
    project_id = _string(row, "project_id", fail)
    section_id = _string(row, "section_id", fail)
    try:
        qty = parse_positive_quantity(row.get("qty"), "qty")
    except MoneyError as exc:
        fail("bad_qty", str(exc), {"field": "qty", "value": row.get("qty")})
        qty = None
    if None in (seq, receipt_id, location_id, sku, supplier_id,
                supplier_batch, result_id, document_id, project_id,
                section_id, qty):
        return None
    return ReceiptRow(seq, receipt_id, location_id, sku, supplier_id,
                      supplier_batch, qty, result_id, document_id,
                      project_id, section_id, ref)


def _parse_target(obj: object, ref: SourceRef, index: int, sink
                  ) -> Optional[TargetRow]:
    def fail(code: str, message: str, detail: Optional[dict] = None):
        sink(make_issue(code, ref, message, detail))

    if not isinstance(obj, dict):
        fail("target_not_object",
             f"targets[{index}] must be an object, got {obj!r}",
             {"index": index})
        return None
    target_id = obj.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        fail("target_bad_id",
             f"targets[{index}].target_id must be a non-empty string",
             {"index": index})
        return None
    location_id = obj.get("location_id")
    if location_id is not None and (
            not isinstance(location_id, str) or not location_id):
        fail("target_bad_location",
             f"targets[{index}].location_id must be a non-empty string",
             {"index": index})
        return None
    status = obj.get("status")
    if status is not None and status not in STATUSES:
        fail("target_bad_status",
             f"targets[{index}].status must be one of {list(STATUSES)}",
             {"index": index, "status": status})
        return None
    return TargetRow(target_id, location_id, status, ref)


def _parse_mapping(obj: object, ref: SourceRef, index: int, sink
                   ) -> Optional[MappingRow]:
    def fail(code: str, message: str, detail: Optional[dict] = None):
        sink(make_issue(code, ref, message, detail))

    if not isinstance(obj, dict):
        fail("mapping_not_object",
             f"mappings[{index}] must be an object, got {obj!r}",
             {"index": index})
        return None
    source_id = obj.get("source_id")
    target_id = obj.get("target_id")
    if not isinstance(source_id, str) or not source_id:
        fail("mapping_bad_source",
             f"mappings[{index}].source_id must be a non-empty string",
             {"index": index})
        return None
    if not isinstance(target_id, str) or not target_id:
        fail("mapping_bad_target",
             f"mappings[{index}].target_id must be a non-empty string",
             {"index": index})
        return None
    try:
        from_start, from_end = check_interval(
            obj.get("from_start"), obj.get("from_end"),
            f"mappings[{index}].from")
        to_start, to_end = check_interval(
            obj.get("to_start"), obj.get("to_end"),
            f"mappings[{index}].to")
    except MoneyError as exc:
        fail("bad_mapping_interval", str(exc), {"index": index})
        return None
    if from_end - from_start != to_end - to_start:
        fail("mapping_length_mismatch",
             f"mappings[{index}] maps [{from_start}, {from_end}) to "
             f"[{to_start}, {to_end}) of different length",
             {"index": index})
        return None
    return MappingRow(source_id, from_start, from_end, target_id,
                      to_start, to_end)


def parse_movement(row: dict, ref: SourceRef, sink) -> Optional[MovementRow]:
    def fail(code: str, message: str, detail: Optional[dict] = None):
        sink(make_issue(code, ref, message, detail))

    if not _require(row, MOVEMENT_KEYS, "movement", fail):
        return None
    if row.get("kind") != "movement":
        fail("row_kind_mismatch",
             f"movement row has kind {row.get('kind')!r}, expected "
             "'movement'")
        return None
    seq = _parse_seq(row, fail)
    movement_type = row.get("movement_type")
    if movement_type not in MOVEMENT_TYPES:
        fail("unknown_movement_type",
             f"movement_type must be one of {list(MOVEMENT_TYPES)}, got "
             f"{movement_type!r}",
             {"field": "movement_type", "value": movement_type})
        return None
    sku = _string(row, "sku", fail)

    raw_targets = row.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        fail("targets_missing", "targets must be a non-empty list",
             {"field": "targets"})
        return None
    targets: list[TargetRow] = []
    seen_targets: set[str] = set()
    for index, obj in enumerate(raw_targets):
        parsed = _parse_target(obj, ref, index, sink)
        if parsed is None:
            return None
        if parsed.target_id in seen_targets:
            fail("duplicate_target_id",
                 f"target {parsed.target_id!r} is declared twice in this "
                 "movement", {"target_id": parsed.target_id})
            return None
        seen_targets.add(parsed.target_id)
        targets.append(parsed)

    raw_mappings = row.get("mappings")
    if not isinstance(raw_mappings, list) or not raw_mappings:
        fail("mappings_missing", "mappings must be a non-empty list",
             {"field": "mappings"})
        return None
    mappings: list[MappingRow] = []
    for index, obj in enumerate(raw_mappings):
        parsed = _parse_mapping(obj, ref, index, sink)
        if parsed is None:
            return None
        mappings.append(parsed)

    referenced = {m.target_id for m in mappings}
    for target in targets:
        if target.target_id not in referenced:
            fail("target_without_mapping",
                 f"target {target.target_id!r} is declared but no mapping "
                 "fills it", {"target_id": target.target_id})
            return None
    by_id = {t.target_id: t for t in targets}
    for mapping in mappings:
        if mapping.target_id not in by_id:
            fail("mapping_to_undeclared_target",
                 f"mapping fills {mapping.target_id!r}, which is not "
                 "declared in targets", {"target_id": mapping.target_id})
            return None

    # Per-type declaration rules.
    for target in targets:
        if movement_type == "transfer":
            if not target.location_id:
                fail("transfer_location_required",
                     f"transfer target {target.target_id!r} must declare "
                     "location_id", {"target_id": target.target_id})
                return None
            if target.status is not None:
                fail("movement_status_forbidden",
                     "transfer keeps the isolation status; target status "
                     "must be absent", {"target_id": target.target_id})
                return None
        elif movement_type == "repack":
            if target.location_id is not None:
                fail("movement_location_forbidden",
                     "repack keeps the source location; target "
                     "location_id must be absent",
                     {"target_id": target.target_id})
                return None
            if target.status is not None:
                fail("movement_status_forbidden",
                     "repack keeps the isolation status; target status "
                     "must be absent", {"target_id": target.target_id})
                return None
        elif movement_type == "quarantine":
            if target.location_id is not None:
                fail("movement_location_forbidden",
                     "quarantine keeps the source location; target "
                     "location_id must be absent",
                     {"target_id": target.target_id})
                return None
            if target.status not in (None, STATUS_QUARANTINED):
                fail("movement_status_mismatch",
                     "quarantine must set target status 'quarantined'",
                     {"target_id": target.target_id})
                return None
        elif movement_type == "release":
            if target.location_id is not None:
                fail("movement_location_forbidden",
                     "release keeps the source location; target "
                     "location_id must be absent",
                     {"target_id": target.target_id})
                return None
            if target.status not in (None, STATUS_AVAILABLE):
                fail("movement_status_mismatch",
                     "release must set target status 'available'",
                     {"target_id": target.target_id})
                return None
        elif movement_type == "ship":
            if target.location_id is not None:
                fail("movement_location_forbidden",
                     "a shipment leaves the warehouse; target "
                     "location_id must be absent",
                     {"target_id": target.target_id})
                return None
            if target.status is not None:
                fail("movement_status_forbidden",
                     "shipment pieces are terminal; target status must be "
                     "absent", {"target_id": target.target_id})
                return None

    recipient = row.get("recipient")
    shipment_document = row.get("shipment_document")
    if movement_type == "ship":
        if not isinstance(recipient, str) or not recipient:
            fail("ship_missing_recipient", "ship must name the recipient")
            return None
        if not isinstance(shipment_document, str) or not shipment_document:
            fail("ship_missing_document",
                 "ship must name shipment_document")
            return None
    else:
        if recipient is not None or shipment_document is not None:
            fail("shipment_fields_forbidden",
                 "recipient/shipment_document are only valid on ship",
                 {"movement_type": movement_type})
            return None
        recipient = shipment_document = None

    if None in (seq, sku):
        return None
    return MovementRow(seq, movement_type, sku, tuple(targets),
                       tuple(mappings), recipient, shipment_document, ref)
