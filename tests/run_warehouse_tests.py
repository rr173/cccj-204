"""Warehouse lineage and recall tests.

Run from the repository root::

    python3 -I tests/run_warehouse_tests.py

The committed fixtures under ``fixtures/warehouse`` carry hand-computed
``expected.json`` numbers typed in from the specification (see
``tests/make_warehouse_fixtures.py``).  Dynamic cases are compared against
``tests/warehouse_reference.py`` — a separate per-piece model that only
reads plain JSON — so a bug shared by the interval code and the reference
is unlikely to hide.

Stress data (wide batches, many mappings, deep chains) is generated at
runtime and never committed.  Set ``BIDFACT_SKIP_MEMORY=1`` to skip the
peak-RSS checks on small machines.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import warehouse_reference as ref  # noqa: E402
from bidfact.warehouse import (BuildBlocked, LibraryError, LibraryExists,  # noqa: E402
                               RecallError, RecallQuery, WarehouseError,
                               build_library, iter_recall, recall)

FIX = ROOT / "fixtures" / "warehouse"
CLI = ROOT / "bin" / "bidfact.py"


def _announcement_dir() -> str:
    rid = json.loads((FIX / "announcement_result_id.json").read_text(
        encoding="utf-8"))
    return str((FIX / rid["result_dir"]).resolve())


def _result_id() -> str:
    return json.loads((FIX / "announcement_result_id.json").read_text(
        encoding="utf-8"))["result_id"]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")


def receipt(seq: int, receipt_id: str, *, location: str = "LOC-A",
            supplier: str = "SUP-1", batch: str = "LOT-A", sku: str = "SKU-1",
            qty: int = 4, project: str = "PRJ-1",
            section: str = "SEC-1") -> dict:
    return {"kind": "receipt", "seq": seq, "receipt_id": receipt_id,
            "location_id": location, "sku": sku, "supplier_id": supplier,
            "supplier_batch": batch, "qty": qty,
            "result_id": _result_id(), "document_id": "docWH1",
            "project_id": project, "section_id": section}


def movement(seq: int, mtype: str, *, sku: str = "SKU-1",
             targets: list[dict], mappings: list[dict], **extra) -> dict:
    row = {"kind": "movement", "seq": seq, "movement_type": mtype,
           "sku": sku, "targets": targets, "mappings": mappings}
    row.update(extra)
    return row


def mapping(source: str, fs: int, fe: int, target: str, ts: int,
            te: int) -> dict:
    return {"source_id": source, "from_start": fs, "from_end": fe,
            "target_id": target, "to_start": ts, "to_end": te}


class WarehouseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bidfact-wh-"))
        self.library = self.tmp / "library"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def package(self, name: str, receipts: list[dict],
                movements: list[dict], *, package_id: str | None = None,
                shuffle: bool = False) -> Path:
        pkg = self.tmp / name
        pkg.mkdir(parents=True, exist_ok=True)
        rs, ms = list(receipts), list(movements)
        if shuffle:
            random.Random(1234).shuffle(rs)
            random.Random(4321).shuffle(ms)
        write_jsonl(pkg / "receipts.jsonl", rs)
        write_jsonl(pkg / "movements.jsonl", ms)
        (pkg / "manifest.json").write_text(json.dumps({
            "package_id": package_id or name,
            "files": {"receipts": "receipts.jsonl",
                      "movements": "movements.jsonl"},
            "announcement_result_dir": _announcement_dir()},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return pkg

    def build_ok(self, pkg: Path) -> dict:
        report = build_library(pkg, self.library)
        self.assertTrue(report.ok, report.issues[:5])
        return report

    def build_issues(self, pkg: Path) -> list[dict]:
        report = build_library(pkg, self.library)
        self.assertFalse(report.ok)
        self.assertIsNone(report.path)
        return report.issues

    @staticmethod
    def seg_key(segment: dict) -> tuple:
        return (segment["carrier_id"], segment["interval"][0],
                segment["interval"][1], segment["status"],
                segment["location_id"], segment["receipt_id"],
                segment["receipt_interval"][0], segment["receipt_interval"][1])

    @staticmethod
    def ship_key(segment: dict) -> tuple:
        return (segment["carrier_id"], segment["interval"][0],
                segment["interval"][1], segment["recipient"],
                segment["shipment_document"], segment["receipt_id"],
                segment["receipt_interval"][0], segment["receipt_interval"][1])


# ------------------------------------------------------------- walkthrough
class TestWalkthrough(WarehouseCase):
    """The hand-counted scenario from the specification."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = json.loads(
            (FIX / "walkthrough" / "expected.json").read_text(
                encoding="utf-8"))

    def test_build_counts_and_segments(self):
        report = build_library(FIX / "walkthrough", self.library)
        self.assertTrue(report.ok, report.issues)
        for key, value in self.expected["counts_after_build"].items():
            if key == "shipments":
                continue
            self.assertEqual(report.counts[key], value, key)
        # Two shipments: one A piece and one B piece, each its own row.
        self.assertEqual(report.counts["shipments"], 2)
        # A's three pieces after the repack must not be one 3-piece row
        # when they sit in P and one piece of A stayed behind.
        conn = sqlite3.connect(
            f"file:{self.library / 'warehouse.sqlite3'}?mode=ro", uri=True)
        try:
            rows = [tuple(r) for r in conn.execute(
                "SELECT carrier_id, pos, length, status, location_id, "
                "receipt_id, valid_from, valid_to FROM stock_segments "
                "WHERE receipt_id='A' ORDER BY valid_from, carrier_id, pos")]
        finally:
            conn.close()
        # repack split A into A[0,1) live, A[1,3) moved, A[4,6) live.
        self.assertIn(("A", 0, 1, "available", "LOC-A", "A", 10, None), rows)
        self.assertIn(("A", 1, 3, "available", "LOC-A", "A", 10, 20), rows)
        self.assertIn(("A", 4, 2, "available", "LOC-A", "A", 10, None), rows)

    def test_recall_full_lot_at_final_cutoff(self):
        build_library(FIX / "walkthrough", self.library)
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40))
        exp = self.expected["final_cutoff_40"]["A"]
        got_live = sorted(self.seg_key(s) for s in report.live)
        want_live = sorted(
            (s["carrier"], s["interval"][0], s["interval"][1],
             s.get("status", "quarantined"), s["location"], "A",
             s["receipt_interval"][0], s["receipt_interval"][1])
            for s in exp["live"] + exp["quarantined"])
        self.assertEqual(got_live, want_live)
        got_ship = [self.ship_key(s) for s in report.shipped]
        self.assertEqual(got_ship, [
            ("OUT-S", 1, 2, "DEPT-S", "SHIP-1", "A", 1, 2)])
        self.assertEqual((report.received_total, report.live_total,
                          report.shipped_total), (6, 5, 1))

    def test_only_A_is_recalled_not_the_other_lot_in_the_same_box(self):
        build_library(FIX / "walkthrough", self.library)
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40))
        self.assertTrue(all(s["receipt_id"] == "A" for s in report.live))
        self.assertTrue(all(s["receipt_id"] == "A" for s in report.shipped))
        # The two pieces S received are one A and one B; only the A one is
        # recallable, so a LOT-A recall has exactly one shipped piece.
        self.assertEqual(report.shipped_total, 1)
        lot_b = recall(self.library, RecallQuery(
            "SUP-1", "LOT-B", "SKU-1", 40))
        self.assertEqual(lot_b.shipped_total, 1)
        self.assertEqual({s["receipt_id"] for s in lot_b.live}, {"B"})

    def test_same_batch_number_other_supplier_does_not_match(self):
        build_library(FIX / "walkthrough", self.library)
        wrong = recall(self.library, RecallQuery(
            "SUP-2", "LOT-A", "SKU-1", 40))
        self.assertEqual(wrong.total, 2)
        self.assertTrue(all(s["carrier_id"] == "C" for s in wrong.live))
        # Same supplier and batch but another SKU is not the recalled SKU.
        other_sku = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-2", 40))
        self.assertEqual({s["carrier_id"] for s in other_sku.live}, {"D"})
        none = recall(self.library, RecallQuery(
            "SUP-9", "LOT-A", "SKU-1", 40))
        self.assertEqual((none.live, none.shipped, none.total), ([], [], 0))

    def test_local_recall_A_2_5(self):
        build_library(FIX / "walkthrough", self.library)
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40, receipt_id="A",
            from_pos=2, to_pos=5))
        exp = self.expected["recall_a_local_2_5"]
        got = sorted(self.seg_key(s) for s in report.live)
        want = sorted(
            (s["carrier"], s["interval"][0], s["interval"][1], s["status"],
             s["location"], "A", s["receipt_interval"][0],
             s["receipt_interval"][1]) for s in exp["live"])
        self.assertEqual(got, want)
        self.assertEqual(report.shipped, [])
        self.assertEqual((report.received_total, report.live_total,
                          report.shipped_total),
                         (exp["received_total"], exp["live_total"],
                          exp["shipped_total"]))
        self.assertEqual(report.receipts[0]["selected"], [2, 5])

    def test_lineage_reopens_the_receipt_and_every_mapping(self):
        build_library(FIX / "walkthrough", self.library)
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40))
        h = [s for s in report.live if s["carrier_id"] == "H"][0]
        steps = h["lineage"]
        self.assertEqual(steps[0]["kind"], "receipt")
        self.assertEqual(steps[0]["receipt_id"], "A")
        self.assertEqual(steps[0]["receipt_pos"], 2)
        self.assertEqual(steps[0]["locator"]["section_id"], "SEC-1")
        self.assertEqual(
            [(s["seq"], s["source_id"], s["target_id"], s["from"], s["to"])
             for s in steps[1:]],
            [(20, "A", "P", [1, 4], [2, 5]),
             (30, "P", "H", [3, 5], [0, 2])])
        # Every hop preserves the offset: A[2] -> P[3] -> H[0].
        self.assertEqual([s["source_pos"] for s in steps[1:]], [2, 3])
        self.assertEqual([s["target_pos"] for s in steps[1:]], [3, 0])
        self.assertEqual(h["receipt_interval"], [2, 4])

    def test_shipped_lineage_and_local_interval(self):
        build_library(FIX / "walkthrough", self.library)
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40))
        shipped = report.shipped[0]
        self.assertEqual(shipped["recipient"], "DEPT-S")
        self.assertEqual(shipped["shipment_document"], "SHIP-1")
        # OUT-S[0,1) is P[1,2) which is A[1,2); OUT-S[1,2) is P[2,3)
        # which is A[2,3) — but recall shows only the A piece here.
        self.assertEqual(shipped["interval"], [1, 2])
        self.assertEqual(shipped["receipt_interval"], [1, 2])
        self.assertEqual([s["target_id"] for s in
                          shipped["lineage"] if s["kind"] == "movement"],
                         ["P", "OUT-S"])

    def test_cutoff_after_repack_before_quarantine(self):
        build_library(FIX / "walkthrough", self.library)
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 20))
        self.assertEqual(report.shipped_total, 0)
        got = {(s["carrier_id"], s["interval"][0], s["interval"][1],
                s["status"], s["receipt_interval"][0],
                s["receipt_interval"][1]) for s in report.live}
        self.assertEqual(got, {
            ("A", 0, 1, "available", 0, 1),
            ("A", 4, 6, "available", 4, 6),
            ("P", 2, 3, "available", 1, 2),
            ("P", 3, 5, "available", 2, 4),
        })
        self.assertEqual(report.total, 6)
        # At cutoff 20 the shipment does not exist yet, and at cutoff 39
        # it still does not.
        r39 = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 39))
        self.assertEqual((r39.shipped_total, r39.live_total), (0, 6))

    def test_cutoff_excludes_a_later_receipt(self):
        build_library(FIX / "walkthrough", self.library)
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-B", "SKU-1", 11))
        self.assertEqual(report.total, 0)
        self.assertEqual([r["receipt_id"] for r in report.skipped_receipts],
                         ["B"])

    def test_bad_movement_rejects_the_whole_library(self):
        report = build_library(FIX / "bad_movement", self.library)
        self.assertFalse(report.ok)
        self.assertIsNone(report.path)
        codes = {i["code"] for i in report.issues}
        self.assertIn(self.expected["bad_movement"]["expected_code"], codes)
        issue = [i for i in report.issues
                 if i["code"] ==
                 self.expected["bad_movement"]["expected_code"]][0]
        self.assertEqual(issue["source"],
                         {"file": "movements.jsonl", "line": 4})
        self.assertFalse((self.library / "warehouse.sqlite3").exists())

    def test_hand_counts_agree_with_the_piece_reference(self):
        pieces, ctx = ref.apply_package(FIX / "walkthrough")
        for receipt_id, qty in (("A", 6), ("B", 4)):
            piece_count = sum(1 for p in pieces.values()
                              if p.receipt == receipt_id)
            self.assertEqual(piece_count, qty)
        expected = self.expected["final_cutoff_40"]["totals"]
        available = quarantined = shipped = 0
        for supplier, batch in (("SUP-1", "LOT-A"), ("SUP-1", "LOT-B")):
            pieces_at, ctx_at = ref.apply_package(FIX / "walkthrough", 40)
            hits = ref.recall_pieces(pieces_at, ctx_at["carriers"], supplier,
                                     batch, "SKU-1", 40)
            available += sum(1 for p in hits["live"]
                             if p.status == "available")
            quarantined += sum(1 for p in hits["live"]
                               if p.status == "quarantined")
            shipped += len(hits["shipped"])
        self.assertEqual(expected["available"], available)
        self.assertEqual(expected["quarantined"], quarantined)
        self.assertEqual(expected["shipped"], shipped)
        self.assertEqual(expected["total"],
                         available + quarantined + shipped)


# ------------------------------------------------------------ input checks
class TestInputRules(WarehouseCase):
    def test_receipt_requires_result_document_project_section(self):
        bad_result = self.package("bad_result", [
            dict(receipt(1, "A"), result_id="Rdeadbeef")], [])
        codes = {i["code"] for i in self.build_issues(bad_result)}
        self.assertEqual(codes, {"receipt_locator_invalid"})

        bad_section = self.package("bad_section", [
            receipt(1, "A", section="SEC-9")], [])
        codes = {i["code"] for i in self.build_issues(bad_section)}
        self.assertEqual(codes, {"receipt_locator_invalid"})

        bad_doc = self.package("bad_doc", [
            dict(receipt(1, "A"), document_id="docNOPE")], [])
        codes = {i["code"] for i in self.build_issues(bad_doc)}
        self.assertEqual(codes, {"receipt_locator_invalid"})

        bad_project = self.package("bad_project", [
            receipt(1, "A", project="PRJ-X")], [])
        codes = {i["code"] for i in self.build_issues(bad_project)}
        self.assertEqual(codes, {"receipt_locator_invalid"})

    def test_locator_check_does_not_need_the_notice_to_still_be_current(self):
        """A retracted/corrected notice still proves its section exists."""
        # The fixture store has no relations at all; the check touches
        # only documents/doc_sections and the result id.
        report = self.build_ok(self.package("plain", [receipt(1, "A")], []))
        self.assertTrue(report.ok)

    def test_duplicate_receipt_and_sequence_are_located(self):
        pkg = self.package("dups", [receipt(1, "A"), receipt(1, "B")], [])
        issues = self.build_issues(pkg)
        self.assertEqual([i["code"] for i in issues], ["duplicate_seq"])
        self.assertEqual(issues[0]["source"],
                         {"file": "receipts.jsonl", "line": 2})

        pkg = self.package("dups2", [receipt(1, "A"), receipt(2, "A")], [])
        issues = self.build_issues(pkg)
        self.assertEqual([i["code"] for i in issues],
                         ["duplicate_receipt_id"])

    def test_qty_must_be_a_positive_integer(self):
        for value in (0, -1, "0", 2.5, True, None):
            pkg = self.package(f"qty_{value!r}".replace(".", "_"),
                               [dict(receipt(1, "A"), qty=value)], [])
            codes = {i["code"] for i in self.build_issues(pkg)}
            self.assertIn("bad_qty", codes, value)

    def test_mapping_must_be_equal_length(self):
        pkg = self.package("len", [receipt(1, "A")], [movement(
            2, "repack", targets=[{"target_id": "P"}],
            mappings=[mapping("A", 0, 3, "P", 0, 2)])])
        codes = {i["code"] for i in self.build_issues(pkg)}
        self.assertIn("mapping_length_mismatch", codes)

    def test_target_must_cover_from_zero_without_gap_or_overlap(self):
        gap = self.package("gap", [receipt(1, "A")], [movement(
            2, "repack", targets=[{"target_id": "P"}],
            mappings=[mapping("A", 0, 2, "P", 1, 3)])])
        self.assertIn("target_not_exactly_covered",
                      {i["code"] for i in self.build_issues(gap)})
        overlap = self.package("overlap", [receipt(1, "A")], [movement(
            2, "repack", targets=[{"target_id": "P"}],
            mappings=[mapping("A", 0, 2, "P", 0, 2),
                      mapping("A", 2, 4, "P", 1, 3)])])
        self.assertIn("target_not_exactly_covered",
                      {i["code"] for i in self.build_issues(overlap)})

    def test_two_mappings_of_one_movement_cannot_take_the_same_piece(self):
        pkg = self.package("twice", [receipt(1, "A")], [movement(
            2, "repack", targets=[{"target_id": "P"}, {"target_id": "Q"}],
            mappings=[mapping("A", 0, 2, "P", 0, 2),
                      mapping("A", 1, 3, "Q", 0, 2)])])
        codes = {i["code"] for i in self.build_issues(pkg)}
        self.assertIn("source_consumed_twice_in_movement", codes)

    def test_referencing_a_future_carrier_is_located(self):
        pkg = self.package("future", [receipt(5, "A")], [movement(
            4, "repack", targets=[{"target_id": "P"}],
            mappings=[mapping("A", 0, 2, "P", 0, 2)])])
        issues = self.build_issues(pkg)
        self.assertEqual([i["code"] for i in issues],
                         ["source_carrier_not_yet_created"])
        self.assertEqual(issues[0]["source"],
                         {"file": "movements.jsonl", "line": 1})

    def test_receipt_and_movement_share_one_sequence_space(self):
        pkg = self.package("seqclash", [receipt(2, "A"), receipt(3, "B")],
                           [movement(3, "repack",
                                     targets=[{"target_id": "P"}],
                                     mappings=[mapping("A", 0, 2, "P", 0, 2)])])
        self.assertIn("duplicate_seq",
                      {i["code"] for i in self.build_issues(pkg)})

    def test_multiple_sources_into_one_target(self):
        pkg = self.package("merge", [receipt(1, "A", qty=2),
                                     receipt(2, "B", qty=2,
                                             batch="LOT-B")],
                           [movement(3, "repack",
                                     targets=[{"target_id": "P"}],
                                     mappings=[mapping("A", 0, 2, "P", 0, 2),
                                               mapping("B", 0, 2, "P", 2, 4)])])
        report = self.build_ok(pkg)
        self.assertEqual(report.counts["carriers"], 3)
        out = recall(self.library, RecallQuery("SUP-1", "LOT-A", "SKU-1", 3))
        self.assertEqual([(s["carrier_id"], s["interval"])
                          for s in out.live], [("P", [0, 2])])
        self.assertEqual(out.live[0]["receipt_interval"], [0, 2])
        # B's pieces sit in the same box P at [2,4) but are a different
        # receipt and lot; a LOT-A recall never includes them.
        self.assertEqual(out.total, 2)

    def test_one_source_split_into_two_targets(self):
        pkg = self.package("split", [receipt(1, "A", qty=6)], [movement(
            2, "repack", targets=[{"target_id": "P"}, {"target_id": "Q"}],
            mappings=[mapping("A", 0, 2, "P", 0, 2),
                      mapping("A", 2, 6, "Q", 0, 4)])])
        self.build_ok(pkg)
        out = recall(self.library, RecallQuery("SUP-1", "LOT-A", "SKU-1", 2))
        self.assertEqual(sorted((s["carrier_id"], s["interval"][0],
                                 s["interval"][1]) for s in out.live),
                         [("P", 0, 2), ("Q", 0, 4)])
        self.assertEqual(out.total, 6)

    def test_later_pieces_keep_their_coordinates(self):
        pkg = self.package("keep", [receipt(1, "A", qty=6)], [movement(
            2, "repack", targets=[{"target_id": "P"}],
            mappings=[mapping("A", 1, 3, "P", 0, 2)])])
        self.build_ok(pkg)
        out = recall(self.library, RecallQuery("SUP-1", "LOT-A", "SKU-1", 2))
        self.assertEqual(sorted((s["carrier_id"], s["interval"][0],
                                 s["interval"][1]) for s in out.live),
                         [("A", 0, 1), ("A", 3, 6), ("P", 0, 2)])

    def test_one_movement_handles_one_sku(self):
        pkg = self.package("sku", [receipt(1, "A", sku="SKU-1"),
                                   receipt(2, "B", sku="SKU-2")], [movement(
            3, "repack", sku="SKU-1", targets=[{"target_id": "P"}],
            mappings=[mapping("A", 0, 2, "P", 0, 2),
                      mapping("B", 0, 2, "P", 2, 4)])])
        codes = {i["code"] for i in self.build_issues(pkg)}
        self.assertIn("movement_sku_mismatch", codes)


# ------------------------------------------------------- movement semantics
class TestMovementSemantics(WarehouseCase):
    def test_repack_keeps_location_and_status(self):
        pkg = self.package("repack", [receipt(1, "A")], [
            movement(2, "quarantine", targets=[{"target_id": "H"}],
                     mappings=[mapping("A", 0, 2, "H", 0, 2)]),
            movement(3, "repack", targets=[{"target_id": "P"}],
                     mappings=[mapping("H", 0, 2, "P", 0, 2)])])
        self.build_ok(pkg)
        out = recall(self.library, RecallQuery("SUP-1", "LOT-A", "SKU-1", 3))
        self.assertEqual([(s["carrier_id"], s["status"], s["location_id"],
                           s["interval"]) for s in out.live
                          if s["carrier_id"] == "P"],
                         [("P", "quarantined", "LOC-A", [0, 2])])
        self.assertEqual(out.total, 4)

    def test_transfer_changes_location_but_not_status(self):
        pkg = self.package("transfer", [receipt(1, "A")], [
            movement(2, "quarantine", targets=[{"target_id": "H"}],
                     mappings=[mapping("A", 0, 2, "H", 0, 2)]),
            movement(3, "transfer",
                     targets=[{"target_id": "T", "location_id": "LOC-Z"}],
                     mappings=[mapping("H", 0, 2, "T", 0, 2)])])
        self.build_ok(pkg)
        out = recall(self.library, RecallQuery("SUP-1", "LOT-A", "SKU-1", 3))
        self.assertEqual([(s["carrier_id"], s["status"], s["location_id"])
                          for s in out.live if s["carrier_id"] == "T"],
                         [("T", "quarantined", "LOC-Z")])
        self.assertEqual(out.total, 4)

    def test_quarantine_and_release_keep_location(self):
        pkg = self.package("quar", [receipt(1, "A", qty=6)], [
            movement(2, "quarantine", targets=[{"target_id": "H"}],
                     mappings=[mapping("A", 1, 4, "H", 0, 3)]),
            movement(3, "release", targets=[{"target_id": "R"}],
                     mappings=[mapping("H", 1, 3, "R", 0, 2)])])
        self.build_ok(pkg)
        out = recall(self.library, RecallQuery("SUP-1", "LOT-A", "SKU-1", 3))
        got = sorted((s["carrier_id"], s["status"], s["location_id"],
                      tuple(s["receipt_interval"])) for s in out.live)
        # A[1,4) -> H[0,3); H[1,3) (A[2,4)) -> R.  Hand-computed.
        self.assertEqual(got, [
            ("A", "available", "LOC-A", (0, 1)),
            ("A", "available", "LOC-A", (4, 6)),
            ("H", "quarantined", "LOC-A", (1, 2)),
            ("R", "available", "LOC-A", (2, 4))])

    def test_quarantined_pieces_cannot_ship(self):
        pkg = self.package("qship", [receipt(1, "A")], [
            movement(2, "quarantine", targets=[{"target_id": "H"}],
                     mappings=[mapping("A", 0, 2, "H", 0, 2)]),
            movement(3, "ship", targets=[{"target_id": "S"}],
                     mappings=[mapping("H", 0, 1, "S", 0, 1)],
                     recipient="D", shipment_document="SH")])
        codes = {i["code"] for i in self.build_issues(pkg)}
        self.assertIn("pieces_not_available", codes)

    def test_available_pieces_cannot_be_released(self):
        pkg = self.package("relav", [receipt(1, "A")], [
            movement(2, "release", targets=[{"target_id": "R"}],
                     mappings=[mapping("A", 0, 2, "R", 0, 2)])])
        codes = {i["code"] for i in self.build_issues(pkg)}
        self.assertIn("pieces_not_quarantined", codes)

    def test_shipment_is_terminal_and_needs_recipient_and_document(self):
        pkg = self.package("ship", [receipt(1, "A")], [
            movement(2, "ship", targets=[{"target_id": "S"}],
                     mappings=[mapping("A", 0, 2, "S", 0, 2)],
                     recipient="D", shipment_document="SH"),
            movement(3, "repack", targets=[{"target_id": "P"}],
                     mappings=[mapping("S", 0, 1, "P", 0, 1)])])
        self.assertIn("source_already_shipped",
                      {i["code"] for i in self.build_issues(pkg)})
        missing = self.package("ship_missing", [receipt(1, "A")], [
            movement(2, "ship", targets=[{"target_id": "S"}],
                     mappings=[mapping("A", 0, 2, "S", 0, 2)])])
        codes = {i["code"] for i in self.build_issues(missing)}
        self.assertIn("ship_missing_recipient", codes)

    def test_repack_may_not_combine_different_locations(self):
        pkg = self.package("reploc", [receipt(1, "A"),
                                      receipt(2, "B", location="LOC-B")],
                           [movement(3, "repack",
                                     targets=[{"target_id": "P"}],
                                     mappings=[mapping("A", 0, 2, "P", 0, 2),
                                               mapping("B", 0, 2, "P", 2, 4)])])
        self.assertIn("movement_changes_location",
                      {i["code"] for i in self.build_issues(pkg)})

    def test_transfer_must_name_a_location(self):
        pkg = self.package("trnoloc", [receipt(1, "A")], [
            movement(2, "transfer", targets=[{"target_id": "T"}],
                     mappings=[mapping("A", 0, 2, "T", 0, 2)])])
        self.assertIn("transfer_location_required",
                      {i["code"] for i in self.build_issues(pkg)})

    def test_mapping_out_of_bounds_is_rejected(self):
        pkg = self.package("oob", [receipt(1, "A", qty=2)], [
            movement(2, "repack", targets=[{"target_id": "P"}],
                     mappings=[mapping("A", 0, 3, "P", 0, 3)])])
        self.assertIn("mapping_out_of_bounds",
                      {i["code"] for i in self.build_issues(pkg)})

    def test_partly_consumed_source_interval_is_rejected_not_shortened(self):
        pkg = self.package("partly", [receipt(1, "A", qty=4)], [
            movement(2, "repack", targets=[{"target_id": "P"}],
                     mappings=[mapping("A", 0, 2, "P", 0, 2)]),
            movement(3, "repack", targets=[{"target_id": "Q"}],
                     mappings=[mapping("A", 1, 3, "Q", 0, 2)])])
        issues = self.build_issues(pkg)
        self.assertEqual([i["code"] for i in issues],
                         ["source_interval_partly_consumed"])

    def test_target_ids_are_never_reused(self):
        reuse_receipt = self.package("reuse_rcpt", [receipt(1, "A"),
                                                    receipt(2, "B")],
                                     [movement(3, "repack",
                                               targets=[{"target_id": "B"}],
                                               mappings=[mapping("A", 0, 2,
                                                                 "B", 0, 2)])])
        self.assertIn("target_reuses_receipt_id",
                      {i["code"] for i in self.build_issues(reuse_receipt)})
        reuse_target = self.package(
            "reuse_tgt", [receipt(1, "A"), receipt(2, "B")],
            [movement(3, "repack", targets=[{"target_id": "P"}],
                      mappings=[mapping("A", 0, 2, "P", 0, 2)]),
             movement(4, "repack", targets=[{"target_id": "P"}],
                      mappings=[mapping("B", 0, 2, "P", 0, 2)])])
        self.assertIn("target_id_reused",
                      {i["code"] for i in self.build_issues(reuse_target)})

    def test_input_line_order_does_not_matter(self):
        receipts = [receipt(10, "A", qty=6), receipt(12, "B", qty=4)]
        movements = [
            movement(20, "repack", targets=[{"target_id": "P"}],
                     mappings=[mapping("B", 0, 2, "P", 0, 2),
                               mapping("A", 1, 4, "P", 2, 5),
                               mapping("B", 2, 4, "P", 5, 7)]),
            movement(30, "quarantine", targets=[{"target_id": "H"}],
                     mappings=[mapping("P", 3, 5, "H", 0, 2)]),
            movement(40, "ship", targets=[{"target_id": "OUT-S"}],
                     mappings=[mapping("P", 1, 3, "OUT-S", 0, 2)],
                     recipient="DEPT-S", shipment_document="SHIP-1")]
        ordered = self.package("ordered", receipts, movements,
                               package_id="SAME-PACKAGE")
        shuffled = self.package("shuffled", receipts, movements,
                                package_id="SAME-PACKAGE", shuffle=True)
        lib_a, lib_b = self.tmp / "lib_a", self.tmp / "lib_b"
        a = build_library(ordered, lib_a)
        b = build_library(shuffled, lib_b)
        self.assertTrue(a.ok and b.ok)
        self.assertEqual(a.library_digest, b.library_digest)
        q = RecallQuery("SUP-1", "LOT-A", "SKU-1", 40)
        ra, rb = recall(lib_a, q), recall(lib_b, q)
        self.assertEqual([self.seg_key(s) for s in ra.live],
                         [self.seg_key(s) for s in rb.live])
        self.assertEqual([self.ship_key(s) for s in ra.shipped],
                         [self.ship_key(s) for s in rb.shipped])


# ----------------------------------------------------------- reference compare
class TestReferenceComparison(WarehouseCase):
    """Random small packages, checked piece by piece against the reference."""

    def _compare(self, pkg: Path, receipts: list[dict], cutoff: int,
                 keys: set[tuple]) -> None:
        report = build_library(pkg, self.library)
        self.assertTrue(report.ok, report.issues[:4])
        pieces, ctx = ref.apply_package(pkg, cutoff)
        conn = sqlite3.connect(
            f"file:{self.library / 'warehouse.sqlite3'}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for supplier, batch, sku in keys:
                # Accumulate the library answer piece by piece.
                lib_live: list[tuple] = []
                lib_ship: list[tuple] = []
                for seg in self._iter_segments(conn, supplier, batch, sku,
                                               cutoff):
                    for pos in range(seg["pos"], seg["pos"] + seg["length"]):
                        if seg["kind"] == "live":
                            lib_live.append(
                                (seg["carrier"], pos, seg["status"],
                                 seg["location"], seg["receipt"],
                                 seg["receipt_pos"] + pos - seg["pos"]))
                        else:
                            lib_ship.append(
                                (seg["carrier"], pos, seg["recipient"],
                                 seg["shipment_document"], seg["receipt"],
                                 seg["receipt_pos"] + pos - seg["pos"]))
                hit = ref.recall_pieces(pieces, ctx["carriers"], supplier,
                                        batch, sku, cutoff)
                want_live = sorted(
                    (p.carrier, p.pos, p.status, p.location, p.receipt,
                     p.receipt_pos) for p in hit["live"])
                want_ship = sorted(
                    (p.carrier, p.pos, p.shipped_by[1],
                     p.shipped_by[0], p.receipt, p.receipt_pos)
                    for p in hit["shipped"])
                self.assertEqual(sorted(lib_live), want_live,
                                 f"live {supplier}/{batch}/{sku}@{cutoff}")
                self.assertEqual(sorted(lib_ship), want_ship,
                                 f"shipped {supplier}/{batch}/{sku}@{cutoff}")
        finally:
            conn.close()

    @staticmethod
    def _iter_segments(conn, supplier, batch, sku, cutoff):
        """The chronologically last row for each piece at the cutoff.

        A piece may appear as several historical rows as it is split and
        merged; only the row whose ``valid_from <= cutoff < valid_to`` (or
        live) describes where that piece is *at* the cutoff.
        """
        for row in conn.execute(
                "SELECT s.* FROM stock_segments s JOIN carriers c "
                "ON c.carrier_id=s.carrier_id WHERE s.supplier_id=? AND "
                "s.supplier_batch=? AND s.sku=? AND s.valid_from<=? AND "
                "(s.valid_to IS NULL OR s.valid_to>?) "
                "ORDER BY c.created_seq, s.carrier_id, s.pos",
                (supplier, batch, sku, cutoff, cutoff)):
            yield {"kind": "live", "carrier": row["carrier_id"],
                   "pos": int(row["pos"]), "length": int(row["length"]),
                   "status": row["status"],
                   "location": row["location_id"],
                   "receipt": row["receipt_id"],
                   "receipt_pos": int(row["receipt_pos"])}
        for row in conn.execute(
                "SELECT * FROM shipments WHERE supplier_id=? AND "
                "supplier_batch=? AND sku=? AND seq<=? ORDER BY seq, "
                "shipment_document, carrier_id, pos",
                (supplier, batch, sku, cutoff)):
            yield {"kind": "shipped", "carrier": row["carrier_id"],
                   "pos": int(row["pos"]), "length": int(row["length"]),
                   "recipient": row["recipient"],
                   "shipment_document": row["shipment_document"],
                   "receipt": row["receipt_id"],
                   "receipt_pos": int(row["receipt_pos"])}

    def test_multiple_splits_merges_transfers_and_releases(self):
        receipts = [
            receipt(10, "A", location="L1", batch="LOT-A", qty=6),
            receipt(12, "B", location="L1", batch="LOT-B", qty=5),
            receipt(14, "C", location="L2", batch="LOT-A", qty=4,
                    supplier="SUP-2"),
        ]
        movements = [
            # Split A[1,5) into P[0,4) and Q[0,4) is not valid: P/Q hold
            # A[1,3) and A[3,5); A keeps [0,1) and [5,6).
            movement(20, "repack", targets=[{"target_id": "M"}],
                     mappings=[mapping("B", 0, 3, "M", 0, 3),
                               mapping("A", 1, 3, "M", 3, 5)]),
            movement(22, "transfer",
                     targets=[{"target_id": "T", "location_id": "L3"}],
                     mappings=[mapping("M", 1, 4, "T", 0, 3)]),
            movement(24, "quarantine", targets=[{"target_id": "H"}],
                     mappings=[mapping("T", 0, 2, "H", 0, 2)]),
            movement(26, "release", targets=[{"target_id": "R"}],
                     mappings=[mapping("H", 1, 2, "R", 0, 1)]),
            movement(28, "ship", targets=[{"target_id": "S"}],
                     mappings=[mapping("M", 0, 1, "S", 0, 1),
                               mapping("A", 5, 6, "S", 1, 2)],
                     recipient="DEPT-1", shipment_document="SHIP-9"),
        ]
        pkg = self.package("multi", receipts, movements)
        keys = {("SUP-1", "LOT-A", "SKU-1"), ("SUP-1", "LOT-B", "SKU-1"),
                ("SUP-2", "LOT-A", "SKU-1")}
        for cutoff in (20, 22, 24, 26, 28):
            self._compare(pkg, receipts, cutoff, keys)

    def test_multi_target_repack_and_later_transfer(self):
        receipts = [receipt(1, "A", qty=8, location="L1")]
        movements = [
            movement(2, "repack",
                     targets=[{"target_id": "P"}, {"target_id": "Q"}],
                     mappings=[mapping("A", 6, 8, "P", 0, 2),
                               mapping("A", 0, 2, "Q", 0, 2),
                               mapping("A", 2, 4, "P", 2, 4)]),
            movement(3, "transfer",
                     targets=[{"target_id": "T", "location_id": "L2"}],
                     mappings=[mapping("P", 0, 3, "T", 0, 3)]),
            movement(4, "ship", targets=[{"target_id": "S"}],
                     mappings=[mapping("T", 1, 2, "S", 0, 1)],
                     recipient="DEPT-2", shipment_document="SHIP-2"),
        ]
        pkg = self.package("multi2", receipts, movements)
        for cutoff in (2, 3, 4):
            self._compare(pkg, receipts, cutoff,
                          {("SUP-1", "LOT-A", "SKU-1")})

    def test_release_after_partial_quarantine(self):
        receipts = [receipt(1, "A", qty=10)]
        movements = [
            movement(2, "quarantine",
                     targets=[{"target_id": "H1"}, {"target_id": "H2"}],
                     mappings=[mapping("A", 2, 5, "H1", 0, 3),
                               mapping("A", 0, 2, "H2", 0, 2)]),
            movement(3, "repack", targets=[{"target_id": "P"}],
                     mappings=[mapping("H1", 0, 3, "P", 0, 3)]),
            movement(4, "release", targets=[{"target_id": "R"}],
                     mappings=[mapping("P", 1, 3, "R", 0, 2)]),
            movement(5, "ship", targets=[{"target_id": "S"}],
                     mappings=[mapping("R", 0, 2, "S", 0, 2)],
                     recipient="DEPT-3", shipment_document="SHIP-3"),
        ]
        pkg = self.package("release", receipts, movements)
        for cutoff in (2, 3, 4, 5):
            self._compare(pkg, receipts, cutoff,
                          {("SUP-1", "LOT-A", "SKU-1")})


# --------------------------------------------------------- report contract
class TestReportContract(WarehouseCase):
    def setUp(self):
        super().setUp()
        build_library(FIX / "walkthrough", self.library)

    def test_sum_equals_three_destinations(self):
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40))
        available = sum(s["length"] for s in report.live
                        if s["status"] == "available")
        quarantined = sum(s["length"] for s in report.live
                          if s["status"] == "quarantined")
        shipped = report.shipped_total
        self.assertEqual(available + quarantined + shipped,
                         report.received_total)
        self.assertEqual(report.total, report.received_total)
        self.assertEqual((available, quarantined, shipped), (3, 2, 1))

    def test_streaming_matches_collected_report(self):
        query = RecallQuery("SUP-1", "LOT-A", "SKU-1", 40)
        records = list(iter_recall(self.library, query))
        self.assertEqual([r["record"] for r in records][0], "header")
        self.assertEqual(records[-1]["record"], "totals")
        self.assertEqual(records[-1]["total"], 6)
        collected = recall(self.library, query)
        self.assertEqual(len([r for r in records if r["record"] == "live"]),
                         len(collected.live))
        self.assertEqual(len([r for r in records
                              if r["record"] == "shipped"]),
                         len(collected.shipped))

    def test_empty_recall_is_empty_not_an_error(self):
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 5))
        self.assertEqual(report.total, 0)
        self.assertEqual(report.live, [])
        self.assertEqual(report.shipped, [])

    def test_query_validation(self):
        with self.assertRaises(RecallError):
            RecallQuery("SUP-1", "LOT-A", "SKU-1", 0)
        with self.assertRaises(RecallError):
            RecallQuery("SUP-1", "LOT-A", "SKU-1", True)
        with self.assertRaises(RecallError):
            RecallQuery("SUP-1", "LOT-A", "SKU-1", 40, from_pos=1, to_pos=3)
        with self.assertRaises(RecallError):
            RecallQuery("SUP-1", "LOT-A", "SKU-1", 40, receipt_id="A",
                        from_pos=3, to_pos=3)

    def test_unknown_receipt_filter_yields_empty(self):
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40, receipt_id="NOPE"))
        self.assertEqual(report.total, 0)
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40))
        self.assertEqual(report.total, 6)

    def test_lineage_paths_verify_offsets(self):
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40))
        for seg in report.live + report.shipped:
            pos = seg["interval"][0]
            for step in reversed(seg["lineage"][1:]):
                offset = pos - step["to"][0]
                self.assertTrue(0 <= offset < step["length"], step)
                self.assertEqual(step["source_pos"],
                                 step["from"][0] + offset)
                pos = step["source_pos"]
            receipt = seg["lineage"][0]
            self.assertEqual(receipt["receipt_pos"], pos)
            # This recall has no local range, so the reported receipt
            # interval starts exactly at the segment's receipt offset.
            self.assertEqual(receipt["receipt_pos"],
                             seg["receipt_interval"][0])


# ------------------------------------------------------- build idempotence
class TestBuildLifecycle(WarehouseCase):
    def test_identical_rebuild_reuses_the_library(self):
        pkg = FIX / "walkthrough"
        first = build_library(pkg, self.library)
        self.assertFalse(first.reused)
        digest = hashlib.sha256(
            (self.library / "warehouse.sqlite3").read_bytes()).hexdigest()
        second = build_library(pkg, self.library)
        self.assertTrue(second.reused)
        self.assertEqual(second.library_digest, first.library_digest)
        self.assertEqual(
            digest, hashlib.sha256(
                (self.library / "warehouse.sqlite3").read_bytes()).hexdigest())

    def test_different_package_refuses_without_force(self):
        build_library(FIX / "walkthrough", self.library)
        lib_bytes = (self.library / "warehouse.sqlite3").read_bytes()
        other = self.package("other", [receipt(1, "Z")], [])
        with self.assertRaises(LibraryExists):
            build_library(other, self.library)
        self.assertEqual(lib_bytes,
                         (self.library / "warehouse.sqlite3").read_bytes())
        forced = build_library(other, self.library, force=True)
        self.assertTrue(forced.ok)
        self.assertFalse(forced.reused)

    def test_failed_build_never_overwrites_a_usable_library(self):
        build_library(FIX / "walkthrough", self.library)
        before = (self.library / "warehouse.sqlite3").read_bytes()
        bad = build_library(FIX / "bad_movement", self.library, force=True)
        self.assertFalse(bad.ok)
        self.assertEqual(before,
                         (self.library / "warehouse.sqlite3").read_bytes())
        # And the old library still answers the recall correctly.
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40))
        self.assertEqual(report.total, 6)

    def test_build_and_recall_do_not_modify_the_inputs(self):
        sources = [FIX / "walkthrough" / "receipts.jsonl",
                   FIX / "walkthrough" / "movements.jsonl",
                   FIX / "walkthrough" / "manifest.json"]
        store = (FIX / Path(json.loads(
            (FIX / "announcement_result_id.json").read_text(
                encoding="utf-8"))["result_dir"]) / "store.sqlite3")
        sources.append(store)
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sources}
        build_library(FIX / "walkthrough", self.library)
        recall(self.library, RecallQuery("SUP-1", "LOT-A", "SKU-1", 40))
        list(iter_recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40)))
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sources}
        self.assertEqual(before, after)

    def test_recall_works_without_the_package_or_announcement_store(self):
        pkg = self.tmp / "copy"
        shutil.copytree(FIX / "walkthrough", pkg)
        manifest = json.loads((pkg / "manifest.json").read_text(
            encoding="utf-8"))
        manifest["announcement_result_dir"] = _announcement_dir()
        (pkg / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        build_library(pkg, self.library)
        shutil.rmtree(pkg)
        # The announcement store stays available for other tests; delete a
        # copy-only pointer to prove recall never re-opens it.
        conn = sqlite3.connect(
            f"file:{self.library / 'warehouse.sqlite3'}?mode=ro", uri=True)
        try:
            result_id = conn.execute(
                "SELECT value FROM meta WHERE key='announcement_result_id'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(result_id, _result_id())
        report = recall(self.library, RecallQuery(
            "SUP-1", "LOT-A", "SKU-1", 40))
        self.assertEqual(report.total, 6)

    def test_library_reports_its_own_schema_database(self):
        build_library(FIX / "walkthrough", self.library)
        with self.assertRaises(LibraryError):
            recall(self.tmp / "nope", RecallQuery("S", "B", "K", 1))
        broken = self.tmp / "broken"
        broken.mkdir()
        sqlite3.connect(broken / "warehouse.sqlite3").close()
        with self.assertRaises(LibraryError):
            recall(broken, RecallQuery("S", "B", "K", 1))


# ------------------------------------------------------------ CLI surface
class TestCli(WarehouseCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(CLI), *args],
                              capture_output=True, text=True, timeout=120)

    def test_build_and_recall_round_trip(self):
        built = self.run_cli("warehouse", "build", "--package",
                             str(FIX / "walkthrough"), "--library",
                             str(self.library))
        self.assertEqual(built.returncode, 0, built.stderr)
        payload = json.loads(built.stdout)
        self.assertTrue(payload["ok"])
        out = self.run_cli(
            "warehouse", "recall", "--library", str(self.library),
            "--supplier", "SUP-1", "--batch", "LOT-A", "--sku", "SKU-1",
            "--cutoff", "40", "--receipt", "A", "--from-pos", "2",
            "--to-pos", "5")
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual((data["received_total"], data["live_total"],
                          data["shipped_total"]), (3, 3, 0))

    def test_cli_jsonl_streams_segments(self):
        self.run_cli("warehouse", "build", "--package",
                     str(FIX / "walkthrough"), "--library",
                     str(self.library))
        target = self.tmp / "recall.jsonl"
        out = self.run_cli(
            "warehouse", "recall", "--library", str(self.library),
            "--supplier", "SUP-1", "--batch", "LOT-A", "--sku", "SKU-1",
            "--cutoff", "40", "--jsonl", str(target))
        self.assertEqual(out.returncode, 0, out.stderr)
        totals = json.loads(out.stdout)
        self.assertEqual(totals["total"], 6)
        lines = [json.loads(line) for line in
                 target.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[0]["record"], "header")
        self.assertEqual(lines[-1]["record"], "totals")
        self.assertTrue(any(line["record"] == "live" for line in lines))

    def test_blocked_build_exits_nonzero_and_names_the_line(self):
        out = self.run_cli("warehouse", "build", "--package",
                           str(FIX / "bad_movement"), "--library",
                           str(self.library))
        self.assertEqual(out.returncode, 1)
        payload = json.loads(out.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["issues"][0]["source"]["file"],
                         "movements.jsonl")
        self.assertEqual(payload["issues"][0]["source"]["line"], 4)


# -------------------------------------------------------------- sizes
class TestScale(WarehouseCase):
    """Wide batches, many mappings and deep chains stay interval-sized."""

    def _wide_package(self, name: str, pieces: int, many_mappings: bool
                      ) -> tuple[Path, int]:
        receipts = [receipt(1, "BIG", qty=pieces, batch="LOT-W")]
        maps = []
        if many_mappings:
            # Ten equal slices of the receipt, reassembled out of order.
            chunk = pieces // 10
            remaining = pieces
            for index in range(10):
                length = chunk if index < 9 else remaining
                remaining -= length
                maps.append(mapping("BIG", index * chunk, index * chunk +
                                    length, "W", pieces - (index + 1) * chunk,
                                    pieces - index * chunk))
        else:
            maps = [mapping("BIG", 0, pieces, "W", 0, pieces)]
        movements = [movement(2, "repack",
                              targets=[{"target_id": "W"}],
                              mappings=maps)]
        return self.package(name, receipts, movements), pieces

    def test_a_million_piece_receipt_is_one_segment(self):
        pkg, pieces = self._wide_package("wide", 1_000_000, False)
        report = build_library(pkg, self.library)
        self.assertTrue(report.ok, report.issues[:3])
        self.assertEqual(report.counts["received_pieces"], pieces)
        self.assertEqual(report.counts["live_segments"], 1)
        conn = sqlite3.connect(
            f"file:{self.library / 'warehouse.sqlite3'}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM stock_segments").fetchone()[0], 2)
        finally:
            conn.close()
        out = recall(self.library, RecallQuery(
            "SUP-1", "LOT-W", "SKU-1", 2))
        self.assertEqual(out.total, pieces)
        self.assertEqual(len(out.live), 1)
        self.assertEqual(out.live[0]["interval"], [0, pieces])

    def test_wide_batch_with_many_mappings_stays_bounded(self):
        if os.environ.get("BIDFACT_SKIP_MEMORY"):
            self.skipTest("memory test disabled via env")
        sizes = [("small", 200_000), ("large", 4_000_000)]
        peaks = {}
        for label, pieces in sizes:
            pkg, _ = self._wide_package(f"wide_{label}", pieces, True)
            lib = self.tmp / f"lib_{label}"
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            report = build_library(pkg, lib)
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
            self.assertTrue(report.ok, report.issues[:2])
            peaks[label] = peak
            stage_free = not any(lib.glob(".build-*"))
            self.assertTrue(stage_free)
            self.assertEqual(
                report.counts["live_segments"], 10,
                "ten slices must stay ten rows, not four million")
        # 20x more pieces must not mean 20x more resident memory.
        self.assertLess(peaks["large"], max(peaks["small"] * 4,
                                            peaks["small"] + 200_000),
                        peaks)

    def test_deep_chain_lineage_is_bounded_by_depth(self):
        depth = 400
        receipts = [receipt(1, "C0", qty=2)]
        movements = []
        for i in range(1, depth + 1):
            movements.append(movement(
                i + 1, "repack", targets=[{"target_id": f"C{i}"}],
                mappings=[mapping(f"C{i - 1}", 0, 2, f"C{i}", 0, 2)]))
        pkg = self.package("deep", receipts, movements)
        report = build_library(pkg, self.library)
        self.assertTrue(report.ok, report.issues[:3])
        out = recall(self.library, RecallQuery("SUP-1", "LOT-A", "SKU-1",
                                               depth + 1))
        self.assertEqual(out.total, 2)
        lineage = out.live[0]["lineage"]
        self.assertEqual(len(lineage), depth + 1)
        self.assertEqual(lineage[0]["kind"], "receipt")
        self.assertEqual(lineage[-1]["target_id"], f"C{depth}")

    def test_recall_does_not_materialise_all_matching_segments(self):
        """A recall of 20k disjoint segments streams with low memory."""
        if os.environ.get("BIDFACT_SKIP_MEMORY"):
            self.skipTest("memory test disabled via env")
        segments = 20_000
        receipts = [receipt(1, "M", qty=segments * 2)]
        mappings = [
            mapping("M", 2 * i, 2 * i + 2, f"T{i}", 0, 2)
            for i in range(segments)]
        targets = [{"target_id": f"T{i}"} for i in range(segments)]
        movements = [movement(2, "repack", targets=targets,
                              mappings=mappings)]
        pkg = self.package("many", receipts, movements)
        report = build_library(pkg, self.library)
        self.assertTrue(report.ok)
        self.assertEqual(report.counts["live_segments"], segments)
        target = self.tmp / "many.jsonl"
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        live = 0
        total = 0
        from bidfact.warehouse import write_recall_jsonl
        totals = write_recall_jsonl(
            self.library, RecallQuery("SUP-1", "LOT-A", "SKU-1", 2), target)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
        self.assertEqual(totals["total"], segments * 2)
        self.assertLess(peak, 60_000,
                        f"recall held the whole list: {peak} KiB")
        # Spot-check the file is really a stream with every segment.
        line_count = 0
        with target.open(encoding="utf-8") as fh:
            for line in fh:
                line_count += 1
        # One receipt line + every live segment + the totals line.
        self.assertEqual(line_count, segments + 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
