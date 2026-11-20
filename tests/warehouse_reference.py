"""Independent piece-level reference model, used only by tests.

The warehouse library is **interval based**: one row per live/historical
half-open segment, never one row per piece.  To check it on small data
without trusting the implementation, this module keeps an explicit list of
individual pieces.  Its rules are written from the specification, not from
``bidfact.warehouse``:

* one SKU per movement, only live pieces of earlier carriers;
* equal-length mappings, order preserved by offset;
* ``repack`` keeps location and isolation; ``transfer`` changes location
  and keeps isolation; ``quarantine``/``release`` keep location and flip
  the status; ``ship`` takes available pieces and is terminal;
* a movement is applied whole or not at all.

It is deliberately O(pieces) — do not use it on generated stress data.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Piece:
    carrier: str
    pos: int
    sku: str
    supplier: str
    batch: str
    receipt: str
    receipt_pos: int
    location: str
    status: str          # available | quarantined | shipped
    shipped_by: tuple | None = None  # (shipment_document, recipient, seq)


def apply_package(package_dir: Path, cutoff: int | None = None
                  ) -> tuple[dict, dict]:
    """Return ``(pieces, context)`` after applying rows up to ``cutoff``.

    Rows with ``seq > cutoff`` are skipped, so the returned pieces are the
    exact state at that business sequence.  ``pieces`` maps
    ``(carrier, pos)`` to :class:`Piece`.  Raises ``ValueError`` with a
    reason when the package is invalid; the caller decides whether that
    package was expected to be valid.
    """
    pkg = Path(package_dir)
    receipts = [json.loads(line) for line in
                (pkg / "receipts.jsonl").read_text(encoding="utf-8")
                .splitlines() if line.strip()]
    movements = [json.loads(line) for line in
                 (pkg / "movements.jsonl").read_text(encoding="utf-8")
                 .splitlines() if line.strip()]
    carriers: dict[str, dict] = {}
    pieces: dict[tuple[str, int], Piece] = {}
    events = [("receipt", r) for r in receipts] + \
             [("movement", m) for m in movements]
    events.sort(key=lambda e: e[1]["seq"])
    if cutoff is not None:
        events = [e for e in events if e[1]["seq"] <= cutoff]
    for kind, row in events:
        if kind == "receipt":
            if row["receipt_id"] in carriers:
                raise ValueError("duplicate receipt")
            carriers[row["receipt_id"]] = {
                "kind": "receipt", "seq": row["seq"], "sku": row["sku"],
                "receipt": row["receipt_id"]}
            for pos in range(row["qty"]):
                pieces[(row["receipt_id"], pos)] = Piece(
                    carrier=row["receipt_id"], pos=pos, sku=row["sku"],
                    supplier=row["supplier_id"],
                    batch=row["supplier_batch"],
                    receipt=row["receipt_id"], receipt_pos=pos,
                    location=row["location_id"], status="available")
            continue
        seq = row["seq"]
        targets = {t["target_id"]: t for t in row["targets"]}
        taken: set[tuple[str, int]] = set()
        moved: list[tuple[Piece, str, int]] = []
        for mp in row["mappings"]:
            sid = mp["source_id"]
            info = carriers.get(sid)
            if info is None or info["seq"] >= seq:
                raise ValueError(f"bad source {sid}")
            if info["sku"] != row["sku"]:
                raise ValueError("sku mismatch")
            if mp["from_end"] - mp["from_start"] != \
                    mp["to_end"] - mp["to_start"]:
                raise ValueError("length mismatch")
            for off in range(mp["from_end"] - mp["from_start"]):
                key = (sid, mp["from_start"] + off)
                piece = pieces.get(key)
                if piece is None or piece.carrier != sid:
                    raise ValueError(f"missing piece {key}")
                if piece.status == "shipped":
                    raise ValueError("consumes shipped")
                if key in taken:
                    raise ValueError("double take")
                taken.add(key)
                if row["movement_type"] in ("quarantine", "ship") and \
                        piece.status != "available":
                    raise ValueError("needs available")
                if row["movement_type"] == "release" and \
                        piece.status != "quarantined":
                    raise ValueError("needs quarantined")
                moved.append((piece, mp["target_id"],
                              mp["to_start"] + off))
        if row["movement_type"] != "transfer":
            locations = {p.location for p, _, _ in moved}
            if len(locations) > 1:
                raise ValueError("location change")
        for tid, tinfo in targets.items():
            if tid in carriers:
                raise ValueError(f"target {tid} exists")
            carriers[tid] = {"kind": "movement", "seq": seq,
                             "sku": row["sku"], "receipt": None}
        for piece, tid, tpos in moved:
            del pieces[(piece.carrier, piece.pos)]
            target = targets[tid]
            status = piece.status
            if row["movement_type"] == "quarantine":
                status = "quarantined"
            elif row["movement_type"] == "release":
                status = "available"
            elif row["movement_type"] == "ship":
                status = "shipped"
            location = piece.location
            if row["movement_type"] == "transfer":
                location = target["location_id"]
            new = Piece(carrier=tid, pos=tpos, sku=piece.sku,
                        supplier=piece.supplier, batch=piece.batch,
                        receipt=piece.receipt,
                        receipt_pos=piece.receipt_pos, location=location,
                        status=status,
                        shipped_by=((row["shipment_document"],
                                     row["recipient"], seq)
                                    if row["movement_type"] == "ship"
                                    else None))
            pieces[(tid, tpos)] = new
    return pieces, {"carriers": carriers}


def recall_pieces(pieces: dict, carriers: dict, supplier: str, batch: str,
                  sku: str, cutoff: int, receipt: str | None = None,
                  local: tuple[int, int] | None = None) -> dict:
    """Hand-style recall over the piece map at one cutoff sequence."""
    live, shipped = [], []
    for piece in pieces.values():
        if (piece.supplier, piece.batch, piece.sku) != (supplier, batch, sku):
            continue
        if receipt is not None and piece.receipt != receipt:
            continue
        if local is not None and not (
                local[0] <= piece.receipt_pos < local[1]):
            continue
        if piece.status == "shipped":
            shipped.append(piece)
        else:
            live.append(piece)
    return {"live": live, "shipped": shipped}


def compress(rows: list[tuple]) -> list[tuple]:
    """Group per-piece rows into intervals for comparison with the library."""
    rows = sorted(rows)
    out: list[list] = []
    for row in rows:
        if out and row[:-1] == out[-1][:-1] and \
                row[-2] == out[-1][-2] and row[-1] == out[-1][-1] + 1:
            out[-1][-1] = row[-1]
        else:
            out.append(list(row))
    return [tuple(r) for r in out]
