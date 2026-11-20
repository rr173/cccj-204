"""Ingestion: read-only snapshot loading with byte and slice verification.

Nothing here performs network access, OCR or business-system calls.
The corpus on disk is treated as untrusted input:

* each file's sha256 must match the digest declared in the manifest;
* the same immutable ``document_id`` with different bytes is a material
  conflict and aborts the run (no result number is produced);
* every cell span must equal ``page_text[start:end]`` — an extractor
  misalignment is recorded as an ingestion issue and the cell is barred
  from becoming evidence instead of being trusted.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Optional

from .errors import ManifestError, MaterialConflict
from .models import Cell, Page, Span, Table
from .util import digest_bytes, stable_id


def _read_json(path: Path) -> dict:
    with path.open("rb") as fh:
        data = fh.read()
    return json.loads(data.decode("utf-8")), digest_bytes(data)


def load_manifest(collection_dir: Path) -> tuple[dict, str]:
    manifest_path = collection_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ManifestError(f"missing manifest: {manifest_path}")
    manifest, digest = _read_json(manifest_path)
    if not isinstance(manifest.get("documents"), list) or not manifest["documents"]:
        raise ManifestError("manifest.documents must be a non-empty list")
    seen_ids: dict[str, str] = {}
    for i, doc in enumerate(manifest["documents"]):
        for key in ("document_id", "notice_id", "site", "published_at", "source"):
            if key not in doc:
                raise ManifestError(f"documents[{i}] missing {key}")
        src = doc["source"]
        if not isinstance(src, dict) or "file" not in src or "sha256" not in src:
            raise ManifestError(
                f"documents[{i}].source needs file+sha256")
        did = doc["document_id"]
        if did in seen_ids and seen_ids[did] != src["sha256"]:
            raise MaterialConflict(
                f"document_id {did} appears with different byte digests")
        seen_ids[did] = src["sha256"]
    return manifest, digest


def verify_span(pages: dict[int, str], page_no: int, start: int, end: int,
                text: str) -> bool:
    """True only when the claimed span slices exactly out of the page."""
    page = pages.get(page_no)
    if page is None:
        return False
    if start < 0 or end > len(page) or start >= end:
        return False
    return page[start:end] == text


def load_pages(collection_dir: Path, doc: dict) -> tuple[list[Page], str]:
    path = collection_dir / doc["source"]["file"]
    payload, actual_digest = _read_json(path)
    if actual_digest != doc["source"]["sha256"]:
        raise MaterialConflict(
            f"{doc['document_id']}: {path} digest differs from manifest "
            f"(declared {doc['source']['sha256'][:12]}…, "
            f"actual {actual_digest[:12]}…)")
    if payload.get("document_id") != doc["document_id"]:
        raise ManifestError(
            f"{path}: document_id {payload.get('document_id')!r} != manifest")
    pages = [Page(int(p["page_no"]), p["text"]) for p in payload["pages"]]
    return pages, actual_digest


def load_tables(collection_dir: Path, doc: dict, pages: dict[int, str],
                issue_sink) -> list[Table]:
    """Parse cell files; bad spans become issues and are dropped, never trusted."""
    tables: list[Table] = []
    for tref in doc.get("tables", []):
        path = collection_dir / tref["file"]
        payload, actual_digest = _read_json(path)
        if actual_digest != tref["sha256"]:
            raise MaterialConflict(
                f"{doc['document_id']}: {path} digest differs from manifest")
        if payload.get("document_id") != doc["document_id"]:
            raise ManifestError(f"{path}: document_id mismatch")
        good_cells: list[Cell] = []
        for c in payload.get("cells", []):
            cell = Cell(
                page_no=int(c["page_no"]), row=int(c["row"]), col=int(c["col"]),
                text=c["text"], start=int(c["start"]), end=int(c["end"]),
                rowspan=int(c.get("rowspan", 1)), colspan=int(c.get("colspan", 1)))
            if not verify_span(pages, cell.page_no, cell.start, cell.end,
                               cell.text):
                issue_sink(
                    stable_id("iss", doc["document_id"], "slice",
                              cell.page_no, cell.row, cell.col),
                    doc["document_id"], "slice_mismatch",
                    f"table {payload.get('table_id')} cell r{cell.row}"
                    f"c{cell.col} span does not match page text",
                    {"page_no": cell.page_no, "start": cell.start,
                     "end": cell.end, "claimed": cell.text})
                continue
            good_cells.append(cell)
        marker = payload.get("continuation_marker")
        marker_span: Optional[tuple[int, int]] = None
        continuation = payload.get("continuation")
        if continuation and marker:
            page_no = int(marker["page_no"])
            start, end = int(marker["start"]), int(marker["end"])
            if verify_span(pages, page_no, start, end,
                           pages[page_no][start:end]):
                marker_span = (start, end)
            else:
                issue_sink(
                    stable_id("iss", doc["document_id"], "marker",
                              payload.get("table_id")),
                    doc["document_id"], "slice_mismatch",
                    f"table {payload.get('table_id')} continuation marker span "
                    "does not match page text")
                continuation = None  # no structural evidence -> not continued
        tables.append(Table(
            table_id=payload["table_id"], page_no=int(payload["page_no"]),
            continuation=continuation, continuation_marker_span=marker_span,
            cells=tuple(good_cells)))
    return tables


def span_from_match(page_no: int, start: int, end: int, pages: dict[int, str],
                    kind: str = "page", table_id: Optional[str] = None,
                    row: Optional[int] = None, col: Optional[int] = None,
                    header: Optional[str] = None) -> Optional[Span]:
    """Build a verified Span from absolute offsets, or None on mismatch."""
    text = pages.get(page_no, "")[start:end]
    if not text or len(text) != end - start:
        return None
    return Span(page_no=page_no, start=start, end=end, text=text,
                source_kind=kind, table_id=table_id, row=row, col=col,
                header=header)


def iter_collection(collection_dir: Path, manifest: dict,
                    issue_sink) -> Iterator[tuple[dict, list[Page], list[Table]]]:
    """Yield one document's inputs at a time so pages never accumulate."""
    for doc in manifest["documents"]:
        pages, _ = load_pages(collection_dir, doc)
        page_map = {p.page_no: p.text for p in pages}
        tables = load_tables(collection_dir, doc, page_map, issue_sink)
        yield doc, pages, tables
