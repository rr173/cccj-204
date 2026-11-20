"""Announcement-basis impact analysis tests (read-only, hand-checked).

The fixtures under ``fixtures/impact`` are built by
``make_impact_fixtures.py`` from an isolated mini corpus/ledger; the
expected statuses and amounts in ``impact_expected.json`` are typed from
the specification, never derived from the implementation.  The dynamic
cases (wrong/unreadable stores, filters, determinism, ordering, bounded
memory) are constructed here at runtime and deleted afterwards.

Run from the repository root::

    python3 -I tests/run_impact_tests.py
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bidfact.fund import (ImpactError, ImpactFilters, analyze_impact,  # noqa: E402
                          render_terminal, write_report_jsonl)
from bidfact.fund import close_month, read_month  # noqa: E402

FIX = ROOT / "fixtures" / "impact"
CLI = ROOT / "bin" / "bidfact.py"
PKG = FIX / "ledger_packages"


def expected() -> dict:
    return json.loads((FIX / "impact_expected.json").read_text(
        encoding="utf-8"))


def store(name: str) -> Path:
    return Path(expected()["stores"][name])


class ImpactCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bidfact-impact-test-"))
        self.ledger = self.tmp / "ledger"
        close_month(PKG / "2024-01", self.ledger)
        close_month(PKG / "2024-02", self.ledger)
        self.exp = expected()
        self.cutoff = self.exp["cutoff_late"]
        self.ledger_bytes_before = (
            self.ledger / "fund.sqlite3").read_bytes()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------
    def analyze(self, store_name: str, **filter_kw):
        return analyze_impact(self.ledger, store(store_name), self.cutoff,
                              ImpactFilters(**filter_kw))

    def line(self, report, voucher_id, section):
        hits = [l for l in report.lines
                if l["payment_voucher_id"] == voucher_id
                and l["section_id"] == section]
        self.assertEqual(len(hits), 1)
        return hits[0]

    def statuses(self, report) -> dict:
        return {(l["payment_voucher_id"], l["section_id"]):
                l["later_status"]["status"] for l in report.lines}


class TestHandComputedWalkthrough(ImpactCase):
    """The independently computed mini-ledger scenarios."""

    def test_split_payment_one_replace_one_unchanged(self):
        # S1: A's relied assertion is replaced; B's stays valid. Only the
        # A allocation lines (PX-A, PY-A) are closed.
        report = self.analyze("S1")
        want = self.exp["expected_status"]["S1"]
        got = self.statuses(report)
        for voucher, sections in want.items():
            for section, status in sections.items():
                self.assertEqual(got[(voucher, section)], status)
        closed = [l for l in report.lines
                  if l["later_status"]["status"] == "closed"]
        self.assertEqual({(l["payment_voucher_id"], l["section_id"])
                          for l in closed}, {("PX", "A"), ("PY", "A")})
        for line in closed:
            evidence = line["later_status"]["closure_evidence"]
            self.assertEqual(len(evidence), 1)
            ev = evidence[0]
            self.assertEqual(ev["verb"], "replace")
            self.assertEqual(ev["relation_id"], "rel-replace-a")
            self.assertEqual(ev["acting_document_id"], "docFix1")
            self.assertEqual(ev["published_at"],
                             self.exp["replace_published_at"])
            self.assertEqual(ev["target_assertion_id"],
                             line["original_assertion"]["assertion_id"])

    def test_payment_and_refund_amounts_match_the_monthly_close(self):
        report = self.analyze("S1")
        px_a = self.line(report, "PX", "A")
        px_b = self.line(report, "PX", "B")
        # Exactly the hand-checked January allocation / February refund.
        self.assertEqual((px_a["allocated_minor"], px_a["refunded_minor"],
                          px_a["outstanding_minor"]), (3, 0, 3))
        self.assertEqual((px_b["allocated_minor"], px_b["refunded_minor"],
                          px_b["outstanding_minor"]), (3, 2, 1))
        # Refund lineage comes from the saved refund_allocations rows.
        self.assertEqual(px_a["refund_lineage"], [])
        self.assertEqual(px_b["refund_lineage"], [{
            "refund_month": "2024-02", "refund_voucher_id": "RX1",
            "refund_rank": 0, "amount_minor": 2}])
        payload = read_month(self.ledger, "2024-01")
        ledger_amounts = sorted(
            (r["voucher_id"], r["section_id"], r["amount_minor"])
            for r in payload["rows"]["payment_allocations"])
        report_amounts = sorted(
            (l["payment_voucher_id"], l["section_id"], l["allocated_minor"])
            for l in report.lines)
        self.assertEqual(ledger_amounts, report_amounts)

    def test_contested_lists_both_values_and_never_picks_newer(self):
        report = self.analyze("S2")
        px_b = self.line(report, "PX", "B")
        block = px_b["later_status"]
        self.assertEqual(block["status"], "contested")
        values = sorted(v["raw_value"] for v in block["open_values"])
        self.assertEqual(values, sorted(self.exp["s2_contest_values"]))
        # Both provenance rows are retained, each with its own document
        # and publication time; nothing selects the March value.
        docs = {v["document_id"] for v in block["open_values"]}
        self.assertEqual(docs, {"docBase", "docFix2"})
        self.assertEqual(
            sorted(v["published_at"] for v in block["open_values"]),
            ["2024-01-05T09:00:00", self.exp["supplement_published_at"]])
        self.assertEqual(
            sorted(d["normalized"] for d in block["distinct_values"]),
            ["乙公司", "乙补充公司"])
        # A is still closed (replace happened before the supplement).
        self.assertEqual(self.line(report, "PX", "A")[
            "later_status"]["status"], "closed")
        # C is untouched.
        self.assertEqual(self.line(report, "PY", "C")[
            "later_status"]["status"], "unchanged")

    def test_retracting_new_assertion_does_not_revive_closed_one(self):
        report = self.analyze("S3")
        px_a = self.line(report, "PX", "A")
        self.assertEqual(px_a["later_status"]["status"], "closed")
        verbs = {e["verb"] for e in
                 px_a["later_status"]["closure_evidence"]}
        self.assertEqual(verbs, {"replace"})
        # The retraction exists in the store but targets the new assertion.
        conn = sqlite3.connect(store("S3"))
        try:
            retracted = [r[0] for r in conn.execute(
                "SELECT target_assertion_id FROM closures WHERE "
                "verb='retract'")]
        finally:
            conn.close()
        self.assertEqual(len(retracted), 1)
        self.assertNotIn(retracted[0],
                         [e["target_assertion_id"] for e in
                          px_a["later_status"]["closure_evidence"]])

    def test_missing_assertion_is_unverifiable_not_unchanged(self):
        report = self.analyze("S4")
        px_b = self.line(report, "PX", "B")
        self.assertEqual(px_b["later_status"]["status"], "unverifiable")
        self.assertEqual(px_b["later_status"]["reason"],
                         self.exp["expected_reasons"]["S4"]["B"])
        # Other lines remain assessable.
        self.assertEqual(self.line(report, "PX", "A")[
            "later_status"]["status"], "unchanged")

    def test_wrong_result_id_marks_every_line_unverifiable(self):
        report = self.analyze("S5")
        for line in report.lines:
            self.assertEqual(line["later_status"]["status"], "unverifiable")
            self.assertEqual(line["later_status"]["reason"],
                             "result_id_mismatch")
            self.assertEqual(
                line["later_status"]["detail"]["stored_result_id"],
                "Rwrong0000000000")
            self.assertEqual(
                line["later_status"]["detail"]["relied_result_id"],
                self.exp["result_id"])

    def test_empty_result_store_marks_assertions_missing(self):
        report = self.analyze("S6")
        self.assertTrue(report.lines)
        for line in report.lines:
            self.assertEqual(line["later_status"]["status"], "unverifiable")
            self.assertEqual(line["later_status"]["reason"],
                             "assertion_missing_in_later_store")

    def test_scope_mismatch_is_unverifiable_with_reason(self):
        report = self.analyze("S7")
        px_b = self.line(report, "PX", "B")
        self.assertEqual(px_b["later_status"]["status"], "unverifiable")
        self.assertEqual(px_b["later_status"]["reason"],
                         "assertion_scope_mismatch")
        detail = px_b["later_status"]["detail"]
        self.assertEqual(detail["assertion_section_id"], "Z")
        self.assertEqual(detail["line_section_id"], "B")
        # The other lines are unaffected and still assessable.
        self.assertEqual(self.line(report, "PX", "A")[
            "later_status"]["status"], "unchanged")
        self.assertEqual(self.line(report, "PY", "C")[
            "later_status"]["status"], "unchanged")

    def test_pending_assertion_is_unverifiable_not_confirmed(self):
        report = self.analyze("S8")
        px_b = self.line(report, "PX", "B")
        self.assertEqual(px_b["later_status"]["status"], "unverifiable")
        self.assertEqual(px_b["later_status"]["reason"],
                         "assertion_not_confirmed")
        self.assertEqual(
            px_b["later_status"]["detail"]["assertion_status"], "pending")

    def test_assertion_published_after_cutoff_is_unverifiable(self):
        # Rebind B's relied assertion to a future publication inside a
        # throwaway store: at the cutoff it simply does not exist yet,
        # which must not be read as "unchanged".
        import shutil as _shutil
        import sqlite3 as _sqlite3
        future_dir = self.tmp / "S-future"
        future_dir.mkdir()
        db = future_dir / "store.sqlite3"
        _shutil.copyfile(store("S0"), db)
        b_id = next(p["assertion_id"] for p in self.exp["payment_lines"]
                    if p["section_id"] == "B")
        conn = _sqlite3.connect(str(db))
        conn.execute(
            "UPDATE assertions SET published_at='2099-01-01T00:00:00' "
            "WHERE assertion_id=?", (b_id,))
        conn.commit()
        conn.close()
        report = analyze_impact(self.ledger, db, self.cutoff)
        px_b = self.line(report, "PX", "B")
        self.assertEqual(px_b["later_status"]["status"], "unverifiable")
        self.assertEqual(px_b["later_status"]["reason"],
                         "assertion_not_published_at_cutoff")

    def test_cutoff_before_correction_leaves_closed_line_open(self):
        # At 2024-01-20 the February replacement has not happened yet, so
        # the A lines read unchanged against S1; closure evidence is empty.
        report = analyze_impact(self.ledger, store("S1"),
                                self.exp["cutoff_early"])
        for line in report.lines:
            self.assertEqual(line["later_status"]["status"], "unchanged")
            self.assertEqual(line["later_status"]["closure_evidence"], [])
        # The same store at the late cutoff shows the closure.
        late = self.analyze("S1")
        self.assertEqual(self.line(late, "PX", "A")[
            "later_status"]["status"], "closed")

    def test_same_unit_text_different_sections_stay_separate(self):
        # A and C both say 甲公司, but they are different scopes and must
        # not be merged; replacing A never closes C.
        report = self.analyze("S1")
        self.assertEqual(self.line(report, "PY", "C")[
            "later_status"]["status"], "unchanged")
        c = self.line(report, "PY", "C")
        self.assertEqual(
            c["original_assertion"]["assertion_id"],
            next(p["assertion_id"] for p in
                 self.exp["payment_lines"]
                 if p["voucher_id"] == "PY" and p["section_id"] == "C"))
        self.assertNotEqual(
            self.line(report, "PY", "A")["original_assertion"][
                "assertion_id"],
            c["original_assertion"]["assertion_id"])

    def test_one_assertion_used_by_two_payments_keeps_both_amounts(self):
        report = self.analyze("S1")
        px_a = self.line(report, "PX", "A")
        py_a = self.line(report, "PY", "A")
        self.assertEqual(px_a["original_assertion"]["assertion_id"],
                         py_a["original_assertion"]["assertion_id"])
        self.assertEqual(px_a["allocated_minor"], 3)
        self.assertEqual(py_a["allocated_minor"], 2)
        # Status summaries count each allocation line/amount once, never
        # the shared assertion twice or the two lines as one.
        closed_rows = [r for r in report.summary_by_status
                       if r["status"] == "closed"]
        self.assertEqual(sum(r["line_count"] for r in closed_rows), 2)
        self.assertEqual(sum(r["allocated_minor"] for r in closed_rows), 5)
        section_a = next(r for r in report.summary_by_section
                         if r["section_id"] == "A")
        self.assertEqual(section_a["line_count"], 2)
        self.assertEqual(section_a["allocated_minor"], 5)

    def test_anchors_match_on_ledger_period_voucher_and_rank(self):
        # Two vouchers never merge: each line keeps its own natural key
        # built from month, ledger, voucher and allocation rank.
        report = self.analyze("S1")
        ledger_rows = read_month(self.ledger, "2024-01")[
            "rows"]["payment_allocations"]
        by_key = {(r["month"], r["ledger_id"], r["voucher_id"], r["rank"]): r
                  for r in ledger_rows}
        self.assertEqual(len(by_key), len(ledger_rows))
        for line in report.lines:
            key = (line["closed_month"], line["ledger_id"],
                   line["payment_voucher_id"], line["allocation_rank"])
            self.assertIn(key, by_key)
            stored = by_key[key]
            self.assertEqual(line["original_assertion"]["assertion_id"],
                             stored["assertion_id"])
            self.assertEqual(line["original_assertion"]["result_id"],
                             stored["result_id"])
            self.assertEqual(line["project_id"], stored["project_id"])
            self.assertEqual(line["section_id"], stored["section_id"])


class TestLineShapeAndProvenance(ImpactCase):
    def test_every_line_carries_locators_sources_and_primary_key(self):
        report = self.analyze("S1")
        keys = set()
        for line in report.lines:
            keys.add(line["line_key"])
            anchor = line["original_assertion"]
            self.assertEqual(anchor["result_id"], self.exp["result_id"])
            self.assertTrue(anchor["assertion_id"].startswith("ast_"))
            self.assertTrue(anchor["document_id"])
            self.assertTrue(anchor["span"]["text"])
            self.assertEqual(anchor["source"]["file"],
                             "allocation_plan.jsonl")
            self.assertGreaterEqual(anchor["source"]["line"], 1)
            self.assertEqual(line["closed_month"], "2024-01")
            self.assertEqual(line["ledger_id"], "BOOK-IMP")
            self.assertTrue(line["payment_voucher_id"])
            self.assertGreaterEqual(line["allocation_rank"], 0)
            self.assertEqual(
                line["allocated_minor"],
                line["refunded_minor"] + line["outstanding_minor"])
        # Natural keys are unique and line_seq is a stable 1..n sequence.
        self.assertEqual(len(keys), len(report.lines))
        self.assertEqual([l["line_seq"] for l in report.lines],
                         list(range(1, len(report.lines) + 1)))

    def test_stable_sorting_across_stores(self):
        order = None
        for name in ("S1", "S2", "S3", "S4"):
            report = self.analyze(name)
            keys = [(l["closed_month"], l["ledger_id"],
                     l["payment_voucher_id"], l["allocation_rank"])
                    for l in report.lines]
            self.assertEqual(keys, sorted(keys))
            if order is None:
                order = keys
            else:
                self.assertEqual(keys, order)


class TestFilters(ImpactCase):
    def test_month_filter_window_and_refund_cutoff(self):
        # Only January allocation lines; the February refund is outside
        # the window, so refunded_minor must read 0 even though the
        # closed ledger knows the refund.
        report = analyze_impact(self.ledger, store("S1"), self.cutoff,
                                ImpactFilters(month_from="2024-01",
                                              month_to="2024-01"))
        self.assertTrue(report.lines)
        self.assertTrue(all(l["closed_month"] == "2024-01"
                            for l in report.lines))
        self.assertTrue(all(l["refunded_minor"] == 0
                            for l in report.lines))
        # With the full window the B refund is visible again.
        full = self.analyze("S1")
        self.assertEqual(self.line(full, "PX", "B")["refunded_minor"], 2)

    def test_project_and_section_filters(self):
        only_b = analyze_impact(self.ledger, store("S1"), self.cutoff,
                                ImpactFilters(section_id="B"))
        self.assertEqual({l["section_id"] for l in only_b.lines}, {"B"})
        only_prj = analyze_impact(self.ledger, store("S1"), self.cutoff,
                                  ImpactFilters(project_id="PRJ-X"))
        self.assertEqual(len(only_prj.lines), 4)
        other = analyze_impact(self.ledger, store("S1"), self.cutoff,
                               ImpactFilters(project_id="PRJ-OTHER"))
        self.assertEqual(other.line_count, 0)
        self.assertEqual(other.lines, [])

    def test_inverted_month_window_is_rejected(self):
        with self.assertRaises(ImpactError):
            analyze_impact(self.ledger, store("S1"), self.cutoff,
                           ImpactFilters(month_from="2024-03",
                                         month_to="2024-01"))


class TestDeterminismAndImmutability(ImpactCase):
    def test_repeat_runs_have_identical_business_content(self):
        first = self.analyze("S2").as_dict()
        second = self.analyze("S2").as_dict()
        self.assertEqual(first, second)

    def test_unordered_input_rows_do_not_change_output(self):
        # Build a second ledger whose JSONL rows are shuffled; the close
        # order rules and the impact ordering must absorb it.
        other = self.tmp / "ledger2"
        rid = self.exp["result_id"]
        abs_store = (FIX / "results" / rid).resolve()
        for month in ("2024-01", "2024-02"):
            src = PKG / month
            dst = self.tmp / f"pkg-{month}"
            shutil.copytree(src, dst)
            for name in ("bank_flows.jsonl", "vouchers.jsonl",
                         "clearing.jsonl", "allocation_plan.jsonl"):
                p = dst / name
                lines = [l for l in p.read_text(
                    encoding="utf-8").splitlines() if l.strip()]
                p.write_text("\n".join(reversed(lines)) + "\n",
                             encoding="utf-8")
            manifest = json.loads((dst / "manifest.json").read_text(
                encoding="utf-8"))
            # The copied package sits at a different depth; point it at
            # the same evidence store by absolute path.
            manifest["announcement_result_dir"] = str(abs_store)
            (dst / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            close_month(dst, other)
        a = self.analyze("S2").as_dict()
        b = analyze_impact(other, store("S2"), self.cutoff).as_dict()

        def business(d):
            # Source JSONL line numbers legitimately move when the input
            # rows are reordered; compare everything except that locator.
            out = dict(d)
            out["lines"] = [{k: v for k, v in l.items()
                             if k not in ("line_seq",)} for l in d["lines"]]
            for l in out["lines"]:
                l["original_assertion"] = dict(l["original_assertion"])
                l["original_assertion"]["source"] = {
                    "file": l["original_assertion"]["source"]["file"]}
            return out

        ba, bb = business(a), business(b)
        # Compare as order-independent business sets keyed by the natural
        # business identity (voucher + section); ranks follow plan-line
        # order and therefore legitimately move when lines are reversed.
        def keyed(lines):
            return sorted(
                ((l["payment_voucher_id"], l["section_id"],
                  l["allocated_minor"], l["refunded_minor"],
                  l["outstanding_minor"], l["later_status"]["status"],
                  l["original_assertion"]["assertion_id"])
                 for l in lines))
        self.assertEqual(keyed(ba["lines"]), keyed(bb["lines"]))
        for key in ("summary_by_status", "summary_by_section",
                    "line_count"):
            self.assertEqual(ba[key], bb[key], key)

    def test_source_stores_are_byte_identical_after_runs(self):
        digests = {}
        for name in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"):
            digests[store(name)] = store(name).read_bytes()
        for name in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"):
            self.analyze(name)
        for path, before in digests.items():
            self.assertEqual(path.read_bytes(), before)
        # The fund ledger itself is untouched by the read-only analysis.
        self.analyze("S1")
        self.analyze("S2")
        self.assertEqual((self.ledger / "fund.sqlite3").read_bytes(),
                         self.ledger_bytes_before)

    def test_report_records_input_digests_and_result_id(self):
        report = self.analyze("S1")
        import hashlib
        h = hashlib.sha256()
        h.update((self.ledger / "fund.sqlite3").read_bytes())
        self.assertEqual(report.ledger_store_digest, h.hexdigest())
        h = hashlib.sha256()
        h.update(store("S1").read_bytes())
        self.assertEqual(report.announcement_store_digest, h.hexdigest())
        self.assertEqual(report.result_id, self.exp["result_id"])
        self.assertEqual(report.cutoff, self.cutoff)


class TestOutputs(ImpactCase):
    def test_jsonl_streaming_and_full_report_agree(self):
        full = self.analyze("S2")
        jsonl_path = self.tmp / "lines.jsonl"
        streamed = analyze_impact(self.ledger, store("S2"), self.cutoff,
                                  jsonl_path=jsonl_path)
        self.assertEqual(streamed.lines, [])
        rows = [json.loads(l) for l in
                jsonl_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), full.line_count)
        self.assertEqual(rows, full.lines)
        # Streaming keeps the same summaries and counts.
        self.assertEqual(streamed.summary_by_status, full.summary_by_status)
        self.assertEqual(streamed.summary_by_section,
                         full.summary_by_section)

    def test_report_jsonl_bundles_header_summaries_and_lines(self):
        report = self.analyze("S1")
        path = self.tmp / "report.jsonl"
        write_report_jsonl(report, path)
        rows = [json.loads(l) for l in
                path.read_text(encoding="utf-8").splitlines()]
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds[0], "announcement_impact_report")
        self.assertIn("announcement_impact_summary_status", kinds)
        self.assertIn("announcement_impact_summary_section", kinds)
        self.assertEqual(kinds.count("announcement_impact_line"),
                         report.line_count)
        # Drill-down: every summary bucket maps back to concrete lines.
        status_rows = [r for r in rows
                       if r["kind"] == "announcement_impact_summary_status"]
        lines = [r for r in rows
                 if r["kind"] == "announcement_impact_line"]
        for sr in status_rows:
            members = [l for l in lines
                       if l["later_status"]["status"] == sr["status"]
                       and l["currency"] == sr["currency"]]
            self.assertEqual(len(members), sr["line_count"])
            self.assertEqual(sum(m["allocated_minor"] for m in members),
                             sr["allocated_minor"])
            self.assertEqual(sum(m["outstanding_minor"] for m in members),
                             sr["outstanding_minor"])

    def test_terminal_render_is_deterministic(self):
        a = render_terminal(self.analyze("S1"))
        b = render_terminal(self.analyze("S1"))
        self.assertEqual(a, b)
        self.assertIn("公告依据影响分析", a)
        self.assertIn("closed", a)


class TestBadInputs(ImpactCase):
    def test_missing_ledger_is_an_error_not_unverifiable_lines(self):
        with self.assertRaises(ImpactError):
            analyze_impact(self.tmp / "no-ledger", store("S1"), self.cutoff)

    def test_corrupt_ledger_raises_impact_error_not_raw_sqlite_error(self):
        bad = self.tmp / "corrupt-ledger"
        bad.mkdir()
        conn = sqlite3.connect(str(bad / "fund.sqlite3"))
        conn.execute("CREATE TABLE wrong(x)")
        conn.commit()
        conn.close()
        with self.assertRaises(ImpactError):
            analyze_impact(bad, store("S1"), self.cutoff)

    def test_unreadable_announcement_store_is_rejected(self):
        # A SQLite-looking file without the bidfact meta/assertions shape.
        bogus = self.tmp / "bogus.sqlite3"
        conn = sqlite3.connect(str(bogus))
        conn.execute("CREATE TABLE unrelated(x)")
        conn.commit()
        conn.close()
        with self.assertRaises(ImpactError):
            analyze_impact(self.ledger, bogus, self.cutoff)
        with self.assertRaises(ImpactError):
            analyze_impact(self.ledger, self.tmp / "missing-dir",
                           self.cutoff)

    def test_bad_cutoff_shape_is_rejected(self):
        with self.assertRaises(ImpactError):
            analyze_impact(self.ledger, store("S1"), "not-a-timestamp")

    def test_non_bidfact_sqlite_is_rejected(self):
        bogus_dir = self.tmp / "fake-result"
        bogus_dir.mkdir()
        db = bogus_dir / "store.sqlite3"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE meta(key TEXT, value TEXT)")
        conn.commit()
        conn.close()
        with self.assertRaises(ImpactError):
            analyze_impact(self.ledger, bogus_dir, self.cutoff)


class TestBoundedMemory(ImpactCase):
    """A runtime-generated large closed ledger must not pull rows resident."""

    def _build_large_ledger(self, n: int) -> Path:
        # Reuse the impact base store as evidence; generate n payments,
        # each split A/B, inside one big January package.
        big_ledger = self.tmp / f"big-ledger-{n}"
        pkg = self.tmp / f"big-pkg-{n}"
        pkg.mkdir(parents=True)
        ev = {s: next(a for a in self.exp["payment_lines"]
                      if a["section_id"] == s) for s in ("A", "B")}
        rid = self.exp["result_id"]
        store_dir = FIX / "results" / rid
        flows, vouchers, clearings, plans = [], [], [], []
        total = 0
        for i in range(n):
            amount = 100 + (i % 50)
            total += amount
            vid = f"V{i:07d}"
            fid = f"F{i:07d}"
            flows.append({"kind": "bank_flow", "ledger_id": "BOOK-BIG",
                          "account_id": "ACC-1", "bank_flow_id": fid,
                          "month": "2024-01", "currency": "CNY",
                          "direction": "outflow", "amount_minor": amount})
            vouchers.append({"kind": "voucher", "ledger_id": "BOOK-BIG",
                             "voucher_id": vid, "month": "2024-01",
                             "currency": "CNY", "direction": "outflow",
                             "amount_minor": amount,
                             "voucher_type": "payment"})
            clearings.append({"kind": "clearing", "ledger_id": "BOOK-BIG",
                              "account_id": "ACC-1", "bank_flow_id": fid,
                              "voucher_id": vid, "amount_minor": amount})
            for section in ("A", "B"):
                plans.append({
                    "kind": "allocation_plan", "ledger_id": "BOOK-BIG",
                    "plan_id": f"PLAN-{vid}", "voucher_id": vid,
                    "project_id": "PRJ-X", "section_id": section,
                    "weight": 1, "result_id": rid,
                    "result_unit_assertion_id":
                        self._assertion_id(section)})

        def w(name, rows):
            with (pkg / name).open("w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False,
                                        sort_keys=True) + "\n")

        w("bank_flows.jsonl", flows)
        w("vouchers.jsonl", vouchers)
        w("clearing.jsonl", clearings)
        w("allocation_plan.jsonl", plans)
        w("control_bank.jsonl", [
            {"kind": "control", "side": "bank", "account_id": "ACC-1",
             "currency": "CNY", "direction": "outflow", "count": n,
             "amount_minor": total}])
        w("control_voucher.jsonl", [
            {"kind": "control", "side": "voucher", "currency": "CNY",
             "direction": "outflow", "count": n, "amount_minor": total}])
        (pkg / "manifest.json").write_text(json.dumps({
            "ledger_id": "BOOK-BIG", "month": "2024-01",
            "announcement_result_dir": str(store_dir.resolve()),
            "files": {"bank_flow": "bank_flows.jsonl",
                      "voucher": "vouchers.jsonl",
                      "clearing": "clearing.jsonl",
                      "allocation_plan": "allocation_plan.jsonl"},
            "control_files": {"bank": "control_bank.jsonl",
                              "voucher": "control_voucher.jsonl"}},
            ensure_ascii=False), encoding="utf-8")
        close_month(pkg, big_ledger)
        return big_ledger

    def _assertion_id(self, section: str) -> str:
        return next(p["assertion_id"] for p in self.exp["payment_lines"]
                    if p["section_id"] == section)

    def test_peak_memory_flat_and_content_complete(self):
        if os.environ.get("BIDFACT_SKIP_MEMORY"):
            self.skipTest("memory test disabled via env")
        peaks = {}
        for label, n in (("small", 200), ("large", 4000)):
            led = self._build_large_ledger(n)
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            report = analyze_impact(led, store("S1"), self.cutoff,
                                    jsonl_path=self.tmp / f"out-{label}.jsonl")
            peaks[label] = (resource.getrusage(
                resource.RUSAGE_SELF).ru_maxrss - before)
            self.assertEqual(report.line_count, 2 * n)
            # Half the lines (all A) are closed in S1, none missed.
            closed = sum(r["line_count"] for r in report.summary_by_status
                         if r["status"] == "closed")
            self.assertEqual(closed, n)
        self.assertLess(peaks["large"],
                        max(peaks["small"] * 4, peaks["small"] + 300000),
                        peaks)


class TestCli(ImpactCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(CLI), "fund",
                               "impact", "--ledger", str(self.ledger), *args],
                              capture_output=True, text=True, timeout=300)

    def test_impact_json_and_terminal_modes(self):
        out = self.run_cli("--result", str(store("S1")), "--at",
                           self.cutoff)
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["kind"], "announcement_impact_report")
        self.assertEqual(payload["line_count"], 4)
        term = self.run_cli("--result", str(store("S1")), "--at",
                            self.cutoff, "--terminal")
        self.assertEqual(term.returncode, 0, term.stderr)
        self.assertIn("公告依据影响分析", term.stdout)

    def test_impact_jsonl_output_and_filters(self):
        path = self.tmp / "cli-lines.jsonl"
        out = self.run_cli("--result", str(store("S2")), "--at",
                           self.cutoff, "--section", "B", "--jsonl",
                           str(path))
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = [json.loads(l) for l in
                path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(rows)
        self.assertEqual({r["section_id"] for r in rows}, {"B"})

    def test_impact_bad_store_exits_nonzero(self):
        out = self.run_cli("--result", str(self.tmp / "nope"), "--at",
                           self.cutoff)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("fund_error", out.stdout)

    def test_report_jsonl_includes_details_even_with_jsonl_flag(self):
        lines_path = self.tmp / "stream.jsonl"
        report_path = self.tmp / "bundle.jsonl"
        out = self.run_cli("--result", str(store("S1")), "--at",
                           self.cutoff, "--jsonl", str(lines_path),
                           "--report-jsonl", str(report_path))
        self.assertEqual(out.returncode, 0, out.stderr)
        # When both are requested the bundled report still contains all
        # detail lines (streaming-only mode is suppressed).
        rows = [json.loads(l) for l in
                report_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            sum(1 for r in rows
                if r["kind"] == "announcement_impact_line"), 4)
        # And the standalone stream file was not produced in that mode.
        self.assertFalse(lines_path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
