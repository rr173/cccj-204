#!/usr/bin/env python3
"""Build the committed warehouse walkthrough fixture.

The scenario is typed in from the specification and **hand-computed**;
``expected.json`` next to the package records those numbers by hand, never
by running the code under test.

Story
-----
* Receipt A: 6 pieces, supplier SUP-1, batch LOT-A, SKU SKU-1, location
  LOC-A, purchase PRJ-1 / SEC-1, business sequence 10.
* Receipt B: 4 pieces, supplier SUP-1, batch LOT-B, same SKU, location
  LOC-B, purchase PRJ-1 / SEC-2, business sequence 12.
* Box P (repack, seq 20) loads B [0,2), A [1,4), B [2,4): P is six pieces,
  P[0,2) is B[0,2), P[2,5) is A[1,4), P[5,6) is B[2,4).
* Box H (quarantine, seq 30) takes P[3,5): 2 pieces of A, now quarantined
  at LOC-A.
* Shipment S (ship, seq 40) takes P[1,3): that is one piece of A (P[1,2)
  ← A[1,2)) and one piece of B (P[2,3) ← B[2,3)), sent to recipient
  DEPT-S under shipment document SHIP-1.

Hand-computed results (see expected.json):

* After repack (cutoff 20): A live = A[0,1) at LOC-A (1) and P[2,5) at
  LOC-A (3).
* Final (cutoff 40):
    A: 3 live pieces at LOC-A (A[0,1), A[4,6)); 2 quarantined in H;
       1 shipped to DEPT-S.
    B: 1 live at LOC-B (B[3,4)); 2 in H (quarantined); 1 shipped.
* Recall only A[2,5) (receipt A, local [2,5)): H[0,2) (2 pieces) and
  A[2,3) (1 piece); the shipment is *not* in the list, and it is
  P[2,3) ← B[2,3) that S received, not A.
* A wrong sample movement P[4,6) → X must be rejected as a whole even
  though its second half (P[5,6), B) is still live.

Run from the repository root::

    python3 -I tests/make_warehouse_fixtures.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bidfact.pipeline import run_pipeline  # noqa: E402
from bidfact.storage import Store  # noqa: E402

FIX = ROOT / "fixtures" / "warehouse"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")


# ------------------------------------------------------------------ notice
def build_announcement() -> None:
    """A minimal bidfact corpus naming PRJ-1 / SEC-1 and SEC-2."""
    col = FIX / "_announcement_corpus"
    if col.exists():
        shutil.rmtree(col)
    (col / "pages").mkdir(parents=True)
    text = (
        "某采购项目招标公告\n"
        "发布时间：2024-01-05T09:00:00\n"
        "标段编号：SEC-1\n中标单位：甲公司\n中标金额：人民币 100 元\n"
        "标段编号：SEC-2\n中标单位：乙公司\n中标金额：人民币 50 元\n")
    payload = {"document_id": "docWH1",
               "pages": [{"page_no": 1, "text": text}]}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    (col / "pages" / "docWH1.json").write_bytes(data)
    manifest = {"collection": "warehouse-announcement", "documents": [{
        "document_id": "docWH1", "notice_id": "N-WH-1", "site": "site1",
        "project_id": "PRJ-1", "section_ids": ["SEC-1", "SEC-2"],
        "published_at": "2024-01-05T09:00:00",
        "source": {"file": "pages/docWH1.json",
                   "sha256": sha256_bytes(data)}}]}
    write_json(col / "manifest.json", manifest)
    shutil.rmtree(FIX / "announcement_results", ignore_errors=True)
    out = run_pipeline(col, FIX / "announcement_results", None,
                       force_rebuild=True)
    store = Store(Path(out["path"]) / "store.sqlite3")
    try:
        result_id = store.get_meta("result_id")
    finally:
        store.close()
    write_json(FIX / "announcement_result_id.json",
               {"result_dir": f"announcement_results/{result_id}",
                "result_id": result_id})


def result_rel() -> str:
    return json.loads((FIX / "announcement_result_id.json").read_text(
        encoding="utf-8"))["result_dir"]


def result_id() -> str:
    return json.loads((FIX / "announcement_result_id.json").read_text(
        encoding="utf-8"))["result_id"]


# ---------------------------------------------------------------- scenario
def receipt(seq: int, receipt_id: str, location: str, supplier: str,
            batch: str, sku: str, qty: int, project: str,
            section: str) -> dict:
    return {"kind": "receipt", "seq": seq, "receipt_id": receipt_id,
            "location_id": location, "sku": sku, "supplier_id": supplier,
            "supplier_batch": batch, "qty": qty,
            "result_id": result_id(), "document_id": "docWH1",
            "project_id": project, "section_id": section}


def movement(seq: int, mtype: str, sku: str, targets: list[dict],
             mappings: list[dict], **extra) -> dict:
    row = {"kind": "movement", "seq": seq, "movement_type": mtype,
           "sku": sku, "targets": targets, "mappings": mappings}
    row.update(extra)
    return row


def mapping(source: str, fs: int, fe: int, target: str, ts: int,
            te: int) -> dict:
    return {"source_id": source, "from_start": fs, "from_end": fe,
            "target_id": target, "to_start": ts, "to_end": te}


def build_walkthrough() -> None:
    pkg = FIX / "walkthrough"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir(parents=True)

    receipts = [
        receipt(10, "A", "LOC-A", "SUP-1", "LOT-A", "SKU-1", 6,
                "PRJ-1", "SEC-1"),
        receipt(12, "B", "LOC-A", "SUP-1", "LOT-B", "SKU-1", 4,
                "PRJ-1", "SEC-2"),
        # A distractor: same batch number LOT-A but a different supplier.
        receipt(14, "C", "LOC-C", "SUP-2", "LOT-A", "SKU-1", 2,
                "PRJ-1", "SEC-1"),
        # A distractor: same supplier batch but a different SKU.
        receipt(16, "D", "LOC-D", "SUP-1", "LOT-A", "SKU-2", 2,
                "PRJ-1", "SEC-2"),
    ]
    # Repack into P (7 pieces): B[0,2) -> P[0,2); A[1,4) -> P[2,5);
    # B[2,4) -> P[5,7).  A and B share LOC-A so the repack keeps location.
    repack = movement(20, "repack", "SKU-1",
                      [{"target_id": "P"}],
                      [mapping("B", 0, 2, "P", 0, 2),
                       mapping("A", 1, 4, "P", 2, 5),
                       mapping("B", 2, 4, "P", 5, 7)])
    # Quarantine P[3,5): P[3,5) is inside A[1,4)->P[2,5), i.e. A[2,4).
    quarantine = movement(30, "quarantine", "SKU-1",
                          [{"target_id": "H", "status": "quarantined"}],
                          [mapping("P", 3, 5, "H", 0, 2)])
    # Ship P[1,3): one A piece (A[1,2)) and one B piece (B[2,3)).
    ship = movement(40, "ship", "SKU-1",
                    [{"target_id": "OUT-S"}],
                    [mapping("P", 1, 3, "OUT-S", 0, 2)],
                    recipient="DEPT-S", shipment_document="SHIP-1")

    files = {"receipts": "receipts.jsonl", "movements": "movements.jsonl"}
    write_jsonl(pkg / files["receipts"], receipts)
    write_jsonl(pkg / files["movements"], [repack, quarantine, ship])
    write_json(pkg / "manifest.json", {
        "package_id": "WH-WALKTHROUGH",
        "files": files,
        # Relative to the package directory so the fixture is portable.
        "announcement_result_dir": "../" + result_rel()})
    write_json(pkg / "expected.json", EXPECTED)


EXPECTED = {
    "note": "Hand-computed from the specification; not generated by code.",
    "counts_after_build": {
        "receipts": 4,
        "movements": 3,
        "carriers": 7,          # A B C D P H OUT-S
        "shipments": 1,
        "received_pieces": 14,
    },
    "after_repack_cutoff_20": {
        "A/live": [[[0, 1], 1], [[4, 6], 2]],
        "P/live": [[[2, 5], 3, "A"], [[0, 2], 2, "B"], [[5, 7], 2, "B"]],
        "B/live_in_own_carrier": [],
        "P/carriers": {"P": [0, 7]},
    },
    "final_cutoff_40": {
        "A": {"live": [{"carrier": "A", "interval": [0, 1], "location": "LOC-A",
                        "status": "available", "receipt_interval": [0, 1]},
                       {"carrier": "A", "interval": [4, 6], "location": "LOC-A",
                        "status": "available", "receipt_interval": [4, 6]}],
              "quarantined": [{"carrier": "H", "interval": [0, 2],
                               "location": "LOC-A",
                               "receipt_interval": [2, 4]}],
              "shipped": [{"carrier": "OUT-S", "interval": [0, 1],
                           "shipment_document": "SHIP-1",
                           "recipient": "DEPT-S", "receipt_interval": [1, 2]}]},
        "B": {"live": [{"carrier": "P", "interval": [0, 1],
                        "location": "LOC-A", "status": "available",
                        "receipt_interval": [0, 1]},
                       {"carrier": "P", "interval": [5, 7],
                        "location": "LOC-A", "status": "available",
                        "receipt_interval": [2, 4]}],
              "quarantined": [],
              "shipped": [{"carrier": "OUT-S", "interval": [1, 2],
                           "shipment_document": "SHIP-1",
                           "recipient": "DEPT-S", "receipt_interval": [1, 2]}]},
        "totals": {"received": 10, "available": 6, "quarantined": 2,
                   "shipped": 2, "total": 10},
    },
    "recall_a_local_2_5": {
        "receipt": "A", "from_pos": 2, "to_pos": 5, "cutoff": 40,
        "live": [{"carrier": "A", "interval": [4, 5], "receipt_interval": [4, 5],
                  "location": "LOC-A", "status": "available"},
                 {"carrier": "H", "interval": [0, 2], "receipt_interval": [2, 4],
                  "location": "LOC-A", "status": "quarantined"}],
        "shipped": [],
        "received_total": 3, "live_total": 3, "shipped_total": 0,
    },
    "bad_movement": {
        "seq": 50, "movement_type": "repack", "target": "X",
        "mappings": [{"source_id": "P", "from_start": 4, "from_end": 6,
                      "target_id": "X", "to_start": 0, "to_end": 2}],
        "why": "P[4,6) is half consumed (P[4,5) = A[3,4) was quarantined "
               "into H) and half live (P[5,6) = B[2,3) is still in P); "
               "the whole movement is "
               "rejected, so the still-live half is never silently taken.",
        "expected_code": "source_interval_partly_consumed",
    },
}


def build_bad_sample() -> None:
    """A package identical to walkthrough plus the rejected movement."""
    bad = FIX / "bad_movement"
    if bad.exists():
        shutil.rmtree(bad)
    bad.mkdir(parents=True)
    src = FIX / "walkthrough"
    for name in ("receipts.jsonl", "movements.jsonl", "manifest.json"):
        shutil.copy(src / name, bad / name)
    with (bad / "movements.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(movement(
            50, "repack", "SKU-1", [{"target_id": "X"}],
            [mapping("P", 4, 6, "X", 0, 2)]),
            ensure_ascii=False, sort_keys=True) + "\n")
    write_json(bad / "expected.json", {
        "ok": False,
        "issue_code": EXPECTED["bad_movement"]["expected_code"],
        "source_line": 4,
        "note": "Even though P[5,6) is still live, the half-consumed "
                "interval rejects the movement and the whole package."})


if __name__ == "__main__":
    build_announcement()
    build_walkthrough()
    build_bad_sample()
    print(f"warehouse fixtures written under {FIX}")
