"""Fund-close tests: hand-checked walkthrough, failure modes, concurrency.

Run from the repository root::

    python3 -I tests/run_fund_tests.py

The committed fixtures under ``fixtures/fund`` carry hand-written
``expected.json`` files; the tests here compare the ledger against those
numbers and never derive expectations from the implementation.
Dynamic packages (control mismatches, currency errors, big months, the
64-bit overflow case) are written at runtime with a builder that only
emits plain JSON.
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
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fund_fixtures as fx  # noqa: E402
from bidfact.fund import (CloseBlocked, FundError, MonthAlreadyClosed,  # noqa: E402
                          close_month, months_closed, read_cumulative,
                          read_month, trial_close)
from bidfact.fund.money import (MoneyError, format_rate, parse_rate,  # noqa: E402
                                parse_positive_int, split_by_weights)

FIX = ROOT / "fixtures" / "fund"
CLI = ROOT / "bin" / "bidfact.py"


class FundCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bidfact-fund-test-"))
        self.ledger = self.tmp / "ledger"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------
    def close_ok(self, package: Path, ledger: Path | None = None) -> dict:
        out = close_month(package, ledger or self.ledger)
        return out

    def summary(self, month: str, ledger: Path | None = None) -> dict:
        payload = read_month(ledger or self.ledger, month)
        return {(r["section_id"], r["currency"]): r
                for r in payload["rows"]["month_summary"]}

    def package(self, name: str, **kwargs) -> Path:
        return fx.build_package(self.tmp / name, **kwargs)

    def closing_issues(self, package: Path) -> list[dict]:
        report = trial_close(package, self.ledger)
        return report.issues


class TestWalkthrough(FundCase):
    """The three-month example from the specification, hand-checked."""

    def test_january_payment_split_and_flow_attribution(self):
        self.assertEqual(self.close_ok(FIX / "january")["month"], "2024-01")
        expected = json.loads((FIX / "january" / "expected.json").read_text(
            encoding="utf-8"))
        summary = self.summary("2024-01")
        for section, amount in expected["payment"].items():
            self.assertEqual(summary[(section, "CNY")]["payment_minor"],
                             amount, f"payment {section}")
            self.assertEqual(summary[(section, "CNY")]["refund_minor"], 0)
            self.assertEqual(summary[(section, "CNY")]["net_minor"], amount)

        payload = read_month(self.ledger, "2024-01")
        lines = {}
        for row in payload["rows"]["payment_allocations"]:
            lines.setdefault(row["voucher_id"], []).append(
                (row["section_id"], row["amount_minor"], row["rank"],
                 row["weight"], row["sum_weights"]))
        self.assertEqual(lines["P"], [("A", 3, 0, 1, 2), ("B", 2, 1, 1, 2)])
        self.assertEqual(lines["Q"], [("A", 4, 0, 1, 1)])

        # B1's 4 selects A2/B2 of P; B2's 1 selects the A line only.
        flow_lines = sorted(
            (r["voucher_id"], r["bank_flow_id"], r["section_id"],
             r["amount_minor"])
            for r in payload["rows"]["payment_flow_lines"]
            if r["voucher_id"] == "P")
        self.assertEqual(flow_lines,
                         [("P", "B1", "A", 2), ("P", "B1", "B", 2),
                          ("P", "B2", "A", 1)])

    def test_february_refund_hits_b_only(self):
        self.close_ok(FIX / "january")
        self.assertEqual(self.close_ok(FIX / "february")["month"], "2024-02")
        payload = read_month(self.ledger, "2024-02")
        refunds = payload["rows"]["refund_allocations"]
        self.assertEqual([(r["section_id"], r["amount_minor"], r["rank"])
                          for r in refunds], [("B", -1, 0)])
        self.assertEqual(refunds[0]["original_voucher_id"], "P")
        self.assertEqual(refunds[0]["original_month"], "2024-01")
        # Which bank flow returned the unit, and where it came from.
        flows = payload["rows"]["refund_flow_lines"]
        self.assertEqual([(r["account_id"], r["bank_flow_id"],
                           r["amount_minor"]) for r in flows],
                         [("ACC-1", "B3", 1)])
        summary = self.summary("2024-02")
        self.assertEqual(summary[("B", "CNY")]["refund_minor"], 1)
        self.assertNotIn(("A", "CNY"), summary)

    def test_march_refund_ignores_new_plan_and_january_stays(self):
        for month in ("january", "february"):
            self.close_ok(FIX / month)
        self.close_ok(FIX / "march")
        payload = read_month(self.ledger, "2024-03")
        refunds = payload["rows"]["refund_allocations"]
        self.assertEqual(
            [(r["section_id"], r["amount_minor"], r["original_rank"])
             for r in refunds], [("B", -1, 1), ("A", -1, 0)])
        # March's own plan (B only) must not have been consulted; the
        # January allocation is what the refund follows.
        self.assertEqual(
            {(r["voucher_id"], r["section_id"])
             for r in payload["rows"]["payment_allocations"]}, set())
        january = self.summary("2024-01")
        self.assertEqual(january[("A", "CNY")]["payment_minor"], 7)
        self.assertEqual(january[("B", "CNY")]["payment_minor"], 2)
        cumulative = {(r["section_id"]): r for r in read_cumulative(
            self.ledger, "2024-03")}
        self.assertEqual(cumulative["A"]["net_minor"], 6)
        self.assertEqual(cumulative["B"]["net_minor"], 0)

    def test_over_refund_blocks_trial_and_close_and_keeps_ledger(self):
        for month in ("january", "february"):
            self.close_ok(FIX / month)
        before = {m["month"]: m["input_digest"]
                  for m in months_closed(self.ledger)}
        report = trial_close(FIX / "march_over", self.ledger)
        self.assertFalse(report.ok)
        blocking = next(i for i in report.issues
                        if i["code"] == "refund_exceeds_remaining")
        self.assertEqual(blocking["detail"]["remaining"], 4)
        self.assertEqual(blocking["detail"]["refund"], 5)
        self.assertEqual(blocking["source"],
                         {"file": "vouchers.jsonl", "line": 1})
        with self.assertRaises(CloseBlocked) as ctx:
            close_month(FIX / "march_over", self.ledger)
        self.assertEqual(ctx.exception.month, "2024-03")
        self.assertEqual({m["month"]: m["input_digest"]
                          for m in months_closed(self.ledger)}, before)
        # March's capacity is untouched: the correct 2 still closes.
        self.close_ok(FIX / "march")
        march = self.summary("2024-03")
        self.assertEqual(march[("A", "CNY")]["refund_minor"], 1)
        self.assertEqual(march[("B", "CNY")]["refund_minor"], 1)
        # January is immutable.
        january = self.summary("2024-01")
        self.assertEqual(january[("A", "CNY")]["payment_minor"], 7)
        self.assertEqual(january[("B", "CNY")]["payment_minor"], 2)

    def test_two_debits_one_voucher_is_not_rounded_per_debit(self):
        self.close_ok(FIX / "wiring", ledger=self.tmp / "wiring-ledger")
        payload = read_month(self.tmp / "wiring-ledger", "2024-04")
        summary = {r["section_id"]: r["payment_minor"]
                   for r in payload["rows"]["month_summary"]}
        self.assertEqual(summary, {"A": 1, "B": 1})
        flows = {(r["bank_flow_id"], r["section_id"]): r["amount_minor"]
                 for r in payload["rows"]["payment_flow_lines"]}
        self.assertEqual(flows, {("W1", "A"): 1, ("W2", "B"): 1})
        # Each flow's attributed shares add up to that flow's own amount.
        for flow_id, amount in (("W1", 1), ("W2", 1)):
            self.assertEqual(sum(v for (f, _), v in flows.items()
                                 if f == flow_id), amount)

    def test_every_amount_reopens_at_its_source_line(self):
        for month in ("january", "february", "march"):
            self.close_ok(FIX / month)
        packages = {"2024-01": FIX / "january", "2024-02": FIX / "february",
                    "2024-03": FIX / "march"}
        for month, package in packages.items():
            payload = read_month(self.ledger, month)
            for row in payload["rows"]["payment_allocations"]:
                src = _source_line(package, row["source_file"],
                                   row["source_line"])
                self.assertEqual(src["kind"], "allocation_plan")
                self.assertEqual(src["voucher_id"], row["voucher_id"])
                self.assertEqual(src["project_id"], row["project_id"])
                self.assertEqual(src["section_id"], row["section_id"])
                self.assertEqual(src["weight"], row["weight"])
                self.assertEqual(src["result_unit_assertion_id"],
                                 row["assertion_id"])
            for row in payload["rows"]["refund_allocations"]:
                src = _source_line(package, row["source_file"],
                                   row["source_line"])
                self.assertEqual(src["kind"], "voucher")
                self.assertEqual(src["voucher_id"], row["refund_voucher_id"])
                self.assertEqual(src["payment_voucher_id"],
                                 row["original_voucher_id"])
                self.assertEqual(src["currency"], "CNY")
            for row in payload["rows"]["clearings"]:
                src = _source_line(package, row["source_file"],
                                   row["source_line"])
                self.assertEqual(src["kind"], "clearing")
                self.assertEqual(src["voucher_id"], row["voucher_id"])
                self.assertEqual(src["amount_minor"], row["amount_minor"])

    def test_announcement_span_reopens_in_the_source_page(self):
        self.close_ok(FIX / "january")
        payload = read_month(self.ledger, "2024-01")
        corpus = FIX / "_announcement_corpus"
        for row in payload["rows"]["payment_allocations"]:
            span = json.loads(row["ev_span_json"])
            doc = json.loads((corpus / "pages" /
                              f"{row['ev_document_id']}.json").read_text(
                                  encoding="utf-8"))
            text = next(p["text"] for p in doc["pages"]
                        if p["page_no"] == span["page_no"])
            self.assertEqual(text[span["start"]:span["end"]], span["text"])
            self.assertEqual(span["text"], row["ev_unit_raw"])

    def test_month_plan_and_evidence_lineage_round_trip(self):
        self.close_ok(FIX / "january")
        payload = read_month(self.ledger, "2024-01")
        expected_result = json.loads(
            (FIX / "announcement_result_id.json").read_text(
                encoding="utf-8"))["result_id"]
        plan = payload["rows"]["plan_lines"]
        self.assertTrue(plan)
        for row in plan:
            self.assertEqual(row["result_id"], expected_result)
            self.assertTrue(row["ev_document_id"])
            span = json.loads(row["ev_span_json"])
            self.assertTrue(span["text"])
            self.assertEqual(row["source_file"], "allocation_plan.jsonl")
            self.assertGreaterEqual(row["source_line"], 1)
        for row in payload["rows"]["payment_allocations"]:
            self.assertEqual(row["result_id"], expected_result)
            self.assertTrue(row["assertion_id"].startswith("ast_"))
            self.assertEqual(json.loads(row["ev_span_json"])["text"],
                             row["ev_unit_raw"])


def _source_line(package: Path, rel: str, line: int) -> dict:
    """Re-open one JSONL source line named by a closed ledger row."""
    with (package / rel).open("r", encoding="utf-8") as fh:
        for lineno, text in enumerate(fh, 1):
            if lineno == line:
                return json.loads(text)
    raise AssertionError(f"{package}/{rel}:{line} does not exist")


class TestReconciliationFailures(FundCase):
    def test_control_group_count_mismatch_on_one_side_only(self):
        pkg = self.package(
            "ctl", flows=[fx.flow("ACC-1", "F1", "2024-01", 5)],
            vouchers=[fx.voucher("V1", "2024-01", 5)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 5)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", 1)],
            bank_control=[fx.control("bank", "CNY", "outflow", 2, 5),
                          fx.control("bank", "CNY", "outflow", 1, 1,
                                     account="ACC-2")],
            voucher_control=[fx.control("voucher", "CNY", "inflow", 1, 1)])
        issues = self.closing_issues(pkg)
        codes = {i["code"] for i in issues}
        self.assertIn("control_count_mismatch", codes)
        self.assertIn("control_group_without_data", codes)
        missing = next(i for i in issues
                       if i["code"] == "control_group_missing")
        self.assertEqual(missing["source"],
                         {"file": "vouchers.jsonl", "line": 1})
        # Both sides are checked; the voucher side's own sum cannot stand
        # in for the bank side's control.
        by_code = {}
        for issue in issues:
            by_code.setdefault(issue["code"], []).append(issue)
        self.assertEqual(by_code["control_count_mismatch"][0]["detail"][
            "control"], 2)
        self.assertEqual(by_code["control_count_mismatch"][0]["detail"][
            "actual"], 1)
        with self.assertRaises(CloseBlocked):
            close_month(pkg, self.ledger)

    def test_equal_month_totals_but_per_item_short_and_over(self):
        # Month totals agree on both sides (bank 5+5 = 10, vouchers 5+5 =
        # 10, count 2 = 2) yet F1 is over-allocated by 5 while F2 is short
        # by 5.  Neither may cancel the other; the per-item defects block.
        pkg = self.package(
            "net", flows=[fx.flow("ACC-1", "F1", "2024-01", 5),
                          fx.flow("ACC-1", "F2", "2024-01", 5)],
            vouchers=[fx.voucher("V1", "2024-01", 5),
                      fx.voucher("V2", "2024-01", 5)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 5),
                       fx.clearing("ACC-1", "F1", "V2", 5)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", 1),
                   fx.plan("PL2", "V2", "PRJ-A", "B", 1)],
            bank_control=[fx.control("bank", "CNY", "outflow", 2, 10)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 2, 10)])
        issues = self.closing_issues(pkg)
        for code in ("bank_flow_short", "bank_flow_over_allocated"):
            hits = [i for i in issues if i["code"] == code]
            self.assertEqual(len(hits), 1, code)
            self.assertEqual(hits[0]["detail"]["difference"], 5)
        self.assertEqual(
            {i["detail"]["key"] for i in issues
             if i["code"] in ("bank_flow_short",
                              "bank_flow_over_allocated")},
            {"ACC-1/F1", "ACC-1/F2"})
        # Control totals themselves still agree; the defect is per item.
        self.assertFalse([i for i in issues
                          if i["code"].startswith("control_")])
        with self.assertRaises(CloseBlocked) as ctx:
            close_month(pkg, self.ledger)
        messages = " ".join(i["message"] for i in ctx.exception.issues)
        self.assertIn("ACC-1/F2", messages)
        self.assertIn("ACC-1/F1", messages)

    def test_unknown_voucher_reference_is_located(self):
        pkg = self.package(
            "unknown", flows=[fx.flow("ACC-1", "F1", "2024-01", 5)],
            vouchers=[fx.voucher("V1", "2024-01", 5)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 4),
                       fx.clearing("ACC-1", "GHOST", "V1", 1)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", 1)],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 5)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 1, 5)])
        issues = self.closing_issues(pkg)
        unknown = next(i for i in issues
                       if i["code"] == "clearing_unknown_bank_flow")
        self.assertEqual(unknown["source"],
                         {"file": "clearing.jsonl", "line": 2})
        self.assertIn("GHOST", unknown["message"])

    def test_one_flow_split_across_three_vouchers_counts_once(self):
        pkg = self.package(
            "split3", flows=[fx.flow("ACC-1", "F1", "2024-01", 6)],
            vouchers=[fx.voucher("V1", "2024-01", 1),
                      fx.voucher("V2", "2024-01", 2),
                      fx.voucher("V3", "2024-01", 3)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 1),
                       fx.clearing("ACC-1", "F1", "V2", 2),
                       fx.clearing("ACC-1", "F1", "V3", 3)],
            plans=[fx.plan("PLA", "V1", "PRJ-A", "A", 1),
                   fx.plan("PLB", "V2", "PRJ-A", "B", 1),
                   fx.plan("PLC", "V3", "PRJ-A", "A", 1)],
            # Bank control counts the ONE flow, not its three clearings;
            # voucher control counts the three vouchers.
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 6)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 3, 6)])
        report = trial_close(pkg, self.ledger)
        self.assertTrue(report.ok, report.issues)
        self.assertEqual(report.counts["clearings"], 3)
        self.assertEqual(report.counts["payment_allocations"], 3)
        self.assertFalse([i for i in report.issues
                          if i["code"].startswith("control_")])
        # Control totals as reported upstream are now the wrong shape:
        # counting clearings instead of flows must be caught.
        bad = self.package(
            "split3bad", flows=[fx.flow("ACC-1", "F1", "2024-01", 6)],
            vouchers=[fx.voucher("V1", "2024-01", 1),
                      fx.voucher("V2", "2024-01", 2),
                      fx.voucher("V3", "2024-01", 3)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 1),
                       fx.clearing("ACC-1", "F1", "V2", 2),
                       fx.clearing("ACC-1", "F1", "V3", 3)],
            plans=[fx.plan("PLA", "V1", "PRJ-A", "A", 1),
                   fx.plan("PLB", "V2", "PRJ-A", "B", 1),
                   fx.plan("PLC", "V3", "PRJ-A", "A", 1)],
            bank_control=[fx.control("bank", "CNY", "outflow", 3, 6)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 3, 6)])
        issues = trial_close(bad, self.ledger).issues
        hit = next(i for i in issues
                   if i["code"] == "control_count_mismatch")
        self.assertEqual(hit["detail"]["control"], 3)
        self.assertEqual(hit["detail"]["actual"], 1)

    def test_partial_coverage_reports_both_ends(self):
        pkg = self.package(
            "partial", flows=[fx.flow("ACC-1", "F1", "2024-01", 9)],
            vouchers=[fx.voucher("V1", "2024-01", 5)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 4)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", 1)],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 9)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 1, 5)])
        issues = self.closing_issues(pkg)
        diff = {i["code"]: i["detail"]["difference"] for i in issues
                if i["code"] in ("bank_flow_short", "voucher_short")}
        self.assertEqual(diff, {"bank_flow_short": 5, "voucher_short": 1})

    def test_cross_currency_link_is_refused_without_fx(self):
        pkg = self.package(
            "fx", flows=[fx.flow("ACC-1", "F1", "2024-01", 5)],
            vouchers=[fx.voucher("V1", "2024-01", 5, currency="USD")],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 5)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", 1)],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 5)],
            voucher_control=[fx.control("voucher", "USD", "outflow", 1, 5)])
        issues = self.closing_issues(pkg)
        cross = next(i for i in issues
                     if i["code"] == "clearing_cross_currency")
        self.assertEqual(cross["detail"]["bank_currency"], "CNY")
        self.assertEqual(cross["detail"]["voucher_currency"], "USD")

    def test_cross_month_and_direction_links_are_refused(self):
        pkg = self.package(
            "xm", flows=[fx.flow("ACC-1", "F1", "2023-12", 5)],
            vouchers=[fx.voucher("V1", "2024-01", 5, direction="inflow")],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 5)],
            plans=[],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 5)],
            voucher_control=[fx.control("voucher", "CNY", "inflow", 1, 5)])
        codes = {i["code"] for i in self.closing_issues(pkg)}
        self.assertIn("foreign_bank_flow", codes)
        self.assertIn("clearing_cross_month", codes)
        self.assertIn("clearing_cross_direction", codes)

    def test_duplicate_business_keys_are_reported(self):
        pkg = self.package(
            "dupe",
            flows=[fx.flow("ACC-1", "F1", "2024-01", 5),
                   fx.flow("ACC-1", "F1", "2024-01", 5)],
            vouchers=[fx.voucher("V1", "2024-01", 5),
                      fx.voucher("V1", "2024-01", 5)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 5),
                       fx.clearing("ACC-1", "F1", "V1", 5)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", 1)],
            bank_control=[fx.control("bank", "CNY", "outflow", 2, 10)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 2, 10)])
        codes = {i["code"] for i in self.closing_issues(pkg)}
        self.assertIn("duplicate_bank_flow", codes)
        self.assertIn("duplicate_voucher", codes)
        self.assertIn("duplicate_clearing_pair", codes)

    def test_wrong_announcement_anchor_blocks_the_plan(self):
        wrong = "ast_" + "0" * 16
        pkg = self.package(
            "anchor", flows=[fx.flow("ACC-1", "F1", "2024-01", 5)],
            vouchers=[fx.voucher("V1", "2024-01", 5)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 5)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", 1, assertion=wrong)],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 5)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 1, 5)])
        issue = next(i for i in self.closing_issues(pkg)
                     if i["code"] == "plan_evidence_invalid")
        self.assertIn(wrong, issue["message"])
        self.assertEqual(issue["source"]["file"], "allocation_plan.jsonl")

    def test_anchor_for_another_section_is_rejected(self):
        ev = fx.evidence("PRJ-A", "B")
        pkg = self.package(
            "anchor2", flows=[fx.flow("ACC-1", "F1", "2024-01", 5)],
            vouchers=[fx.voucher("V1", "2024-01", 5)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 5)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", 1,
                           assertion=ev["assertion_id"])],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 5)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 1, 5)])
        issue = next(i for i in self.closing_issues(pkg)
                     if i["code"] == "plan_evidence_invalid")
        self.assertIn("section", issue["message"])

    def test_identifiers_compare_case_sensitively(self):
        # The store proves "prj-a/a", but the plan line asks for "A" of
        # the same project: the assertion belongs to a different section
        # key and must not be accepted.
        lower = fx.evidence("prj-a", "a")
        pkg = self.package(
            "case", flows=[fx.flow("ACC-1", "F1", "2024-01", 5)],
            vouchers=[fx.voucher("V1", "2024-01", 5)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 5)],
            plans=[fx.plan("PL1", "V1", "prj-a", "A", 1, proof=lower)],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 5)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 1, 5)])
        issue = next(i for i in self.closing_issues(pkg)
                     if i["code"] == "plan_evidence_invalid")
        self.assertIn("section", issue["message"])
        # With the exact lower-case key the same assertion is accepted.
        ok = self.package(
            "case_ok", flows=[fx.flow("ACC-2", "F2", "2024-01", 5)],
            vouchers=[fx.voucher("V2", "2024-01", 5)],
            clearings=[fx.clearing("ACC-2", "F2", "V2", 5)],
            plans=[fx.plan("PL2", "V2", "prj-a", "a", 1, proof=lower)],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 5,
                                     account="ACC-2")],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 1, 5)])
        self.assertTrue(trial_close(ok, self.ledger).ok)

    def test_duplicate_section_in_one_plan_blocks(self):
        pkg = self.package(
            "dupsec", flows=[fx.flow("ACC-1", "F1", "2024-01", 5)],
            vouchers=[fx.voucher("V1", "2024-01", 5)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 5)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", 1),
                   fx.plan("PL1", "V1", "PRJ-A", "A", 2)],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 5)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 1, 5)])
        codes = {i["code"] for i in self.closing_issues(pkg)}
        self.assertIn("plan_duplicate_section", codes)

    def test_malformed_line_is_located_and_not_dropped_silently(self):
        pkg = self.package(
            "badline", flows=[fx.flow("ACC-1", "F1", "2024-01", 5)],
            vouchers=[fx.voucher("V1", "2024-01", 5)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 5)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", 1)],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 5)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 1, 5)])
        with (pkg / "vouchers.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("{this is not json\n")
            fh.write(json.dumps({"kind": "voucher", "ledger_id": "BOOK-1",
                                 "voucher_id": "V2", "month": "2024-01",
                                 "currency": "CNY", "direction": "outflow",
                                 "amount_minor": 0,
                                 "voucher_type": "payment"}) + "\n")
        issues = self.closing_issues(pkg)
        by_code = {i["code"]: i for i in issues}
        self.assertEqual(by_code["line_not_json"]["source"]["line"], 2)
        self.assertEqual(by_code["bad_amount"]["source"]["line"], 3)

    def test_refund_of_current_month_and_of_refund_are_refused(self):
        for month in ("january",):
            self.close_ok(FIX / month)
        pkg = self.package(
            "refund_bad", month="2024-02",
            flows=[fx.flow("ACC-1", "F9", "2024-02", 1,
                           direction="inflow")],
            vouchers=[fx.voucher("RX", "2024-02", 1, kind="refund",
                                 direction="inflow", original="R1",
                                 rate="1"),
                      fx.voucher("RY", "2024-02", 1, kind="refund",
                                 direction="inflow", original="RX",
                                 rate="1")],
            clearings=[fx.clearing("ACC-1", "F9", "RX", 1)],
            plans=[],
            bank_control=[fx.control("bank", "CNY", "inflow", 1, 1)],
            voucher_control=[fx.control("voucher", "CNY", "inflow", 2, 2)])
        codes = {i["code"] for i in self.closing_issues(pkg)}
        self.assertIn("refund_original_not_closed", codes)


class TestMonthSequenceAndIdempotence(FundCase):
    def test_rerun_reuses_and_different_inputs_never_overwrite(self):
        first = close_month(FIX / "january", self.ledger)
        second = close_month(FIX / "january", self.ledger)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["input_digest"], second["input_digest"])

        # A one-unit change to a source file must not overwrite January.
        tampered = self.tmp / "jan_copy"
        shutil.copytree(FIX / "january", tampered)
        manifest = json.loads((tampered / "manifest.json").read_text(
            encoding="utf-8"))
        manifest["announcement_result_dir"] = str(
            (FIX / fx.announcement_rel()).resolve())
        (tampered / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        rows = (tampered / "bank_flows.jsonl").read_text(
            encoding="utf-8").splitlines()
        # A memo change leaves the business figures intact but makes the
        # submitted inputs different from the closed ones.
        rows[0] = rows[0].replace('"amount_minor": 4',
                                  '"amount_minor": 4, "memo": "clarified"')
        (tampered / "bank_flows.jsonl").write_text("\n".join(rows) + "\n",
                                                   encoding="utf-8")
        with self.assertRaises(MonthAlreadyClosed):
            close_month(tampered, self.ledger)
        report = trial_close(tampered, self.ledger)
        self.assertTrue(any(i["code"] == "month_already_closed"
                            for i in report.issues))
        self.assertEqual(read_month(self.ledger, "2024-01")["rows"][
            "month_summary"][0]["payment_minor"], 7)

    def test_voucher_number_reused_in_another_month_is_refused(self):
        close_month(FIX / "january", self.ledger)
        dup = self.package(
            "dupmonth", month="2024-02",
            flows=[fx.flow("ACC-1", "FB", "2024-02", 4)],
            vouchers=[fx.voucher("P", "2024-02", 4)],
            clearings=[fx.clearing("ACC-1", "FB", "P", 4)],
            plans=[fx.plan("PLB", "P", "PRJ-A", "A", 1)],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 4)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 1, 4)])
        issues = trial_close(dup, self.ledger).issues
        hit = next(i for i in issues
                   if i["code"] == "voucher_already_in_ledger")
        self.assertEqual(hit["source"], {"file": "vouchers.jsonl", "line": 1})
        self.assertEqual(hit["detail"]["closed_month"], "2024-01")
        with self.assertRaises(CloseBlocked):
            close_month(dup, self.ledger)

    def test_out_of_order_and_second_first_month_are_refused(self):
        close_month(FIX / "january", self.ledger)
        with self.assertRaises(FundError):
            close_month(FIX / "march", self.ledger)
        with self.assertRaises(FundError):
            close_month(self.package(
                "dec", month="2023-12",
                flows=[fx.flow("ACC-1", "FD", "2023-12", 1)],
                vouchers=[fx.voucher("VD", "2023-12", 1)],
                clearings=[fx.clearing("ACC-1", "FD", "VD", 1)],
                plans=[fx.plan("PLD", "VD", "PRJ-A", "A", 1)],
                bank_control=[fx.control("bank", "CNY", "outflow", 1, 1)],
                voucher_control=[fx.control("voucher", "CNY", "outflow",
                                            1, 1)]),
                self.ledger)
        self.assertEqual([m["month"] for m in months_closed(self.ledger)],
                         ["2024-01"])

    def test_first_month_may_start_anywhere_then_must_advance(self):
        pkg = self.package(
            "start", month="2024-05",
            flows=[fx.flow("ACC-1", "F1", "2024-05", 1)],
            vouchers=[fx.voucher("V1", "2024-05", 1)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", 1)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", 1)],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, 1)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 1, 1)])
        close_month(pkg, self.ledger)
        with self.assertRaises(FundError):
            close_month(self.package(
                "skip", month="2024-07",
                flows=[fx.flow("ACC-2", "F2", "2024-07", 1)],
                vouchers=[fx.voucher("V2", "2024-07", 1)],
                clearings=[fx.clearing("ACC-2", "F2", "V2", 1)],
                plans=[fx.plan("PL2", "V2", "PRJ-A", "A", 1)],
                bank_control=[fx.control("bank", "CNY", "outflow", 1, 1,
                                         account="ACC-2")],
                voucher_control=[fx.control("voucher", "CNY", "outflow",
                                            1, 1)]), self.ledger)

    def test_failed_close_leaves_no_partial_month_or_refund_use(self):
        for month in ("january", "february"):
            close_month(FIX / month, self.ledger)
        before = months_closed(self.ledger)
        with self.assertRaises(CloseBlocked):
            close_month(FIX / "march_over", self.ledger)
        self.assertEqual(months_closed(self.ledger), before)
        conn = sqlite3.connect(str(self.ledger / "fund.sqlite3"))
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM refund_allocations WHERE month="
                "'2024-03'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rows, 0)
        # The failed attempt did not eat P's capacity.
        close_month(FIX / "march", self.ledger)
        cumulative = {r["section_id"]: r for r in read_cumulative(
            self.ledger, "2024-03")}
        self.assertEqual(cumulative["B"]["refund_minor"], 2)
        self.assertEqual(cumulative["A"]["refund_minor"], 1)

    def test_trial_of_identical_inputs_reports_reuse_not_a_block(self):
        close_month(FIX / "january", self.ledger)
        report = trial_close(FIX / "january", self.ledger)
        self.assertTrue(report.ok)
        codes = {i["code"] for i in report.issues}
        self.assertIn("informational_already_closed", codes)
        # The trial only read: the closed month is still exactly one.
        self.assertEqual(len(months_closed(self.ledger)), 1)

    def test_trial_does_not_touch_the_ledger(self):
        trial_close(FIX / "january", self.ledger)
        self.assertFalse((self.ledger / "fund.sqlite3").exists())
        close_month(FIX / "january", self.ledger)
        before = (self.ledger / "fund.sqlite3").read_bytes()
        trial_close(FIX / "february", self.ledger)
        self.assertEqual((self.ledger / "fund.sqlite3").read_bytes(), before)


class TestConcurrentClose(FundCase):
    def test_two_processes_closing_same_refund_month_decrement_once(self):
        for month in ("january", "february"):
            close_month(FIX / month, self.ledger)
        script = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            f"sys.path.insert(0, {str(ROOT / 'tests')!r})\n"
            "from bidfact.fund import close_month\n"
            "try:\n"
            f"    out = close_month({str(FIX / 'march')!r}, "
            f"{str(self.ledger)!r})\n"
            "    print(json.dumps({'ok': True, 'reused': out['reused']}))\n"
            "except Exception as exc:\n"
            "    print(json.dumps({'ok': False, 'type': "
            "type(exc).__name__, 'message': str(exc)}))\n")
        procs = [subprocess.Popen([sys.executable, "-I", "-c", script],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
                 for _ in range(2)]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out.strip().splitlines()[-1]))
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual(sorted(r["reused"] for r in results),
                         [False, True])
        # A brand-new process reads exactly one deduction.
        check = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from bidfact.fund import read_cumulative\n"
            f"print(json.dumps(read_cumulative({str(self.ledger)!r}, "
            "'2024-03')))\n")
        out = subprocess.run([sys.executable, "-I", "-c", check],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = {r["section_id"]: r for r in json.loads(out.stdout)}
        self.assertEqual(rows["B"]["refund_minor"], 2)
        self.assertEqual(rows["A"]["refund_minor"], 1)
        conn = sqlite3.connect(str(self.ledger / "fund.sqlite3"))
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM closed_months").fetchone()[0]
            refunds = conn.execute(
                "SELECT COUNT(*) FROM refund_allocations WHERE month="
                "'2024-03'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual((n, refunds), (3, 2))


class TestIntegerPrecision(FundCase):
    def test_split_beyond_64_bits_is_exact(self):
        # Two weights whose products exceed 2**64 but whose allocation is
        # still exactly representable in Python integers.
        big = 2 ** 63 + 123456789
        total = 10 ** 25 + 7
        amounts, remainders = split_by_weights(
            total, [big, big * 3 - 1],
            [("PRJ", "A"), ("PRJ", "B")])
        self.assertEqual(sum(amounts), total)
        self.assertTrue(all(remainders[i] < sum([big, big * 3 - 1])
                            for i in range(2)))
        # Verify with the definition of the allocation.
        denom = big + (big * 3 - 1)
        self.assertEqual(amounts[0],
                         total * big // denom
                         + (1 if (total * big % denom)
                            > (total * (big * 3 - 1) % denom) else 0))

    def test_close_with_huge_amounts_and_weights(self):
        huge = 2 ** 62 + 12345           # fits SQLite INTEGER; products do not
        weight_a = 2 ** 40 + 1
        weight_b = 2 ** 41 - 3
        pkg = self.package(
            "huge", flows=[fx.flow("ACC-1", "F1", "2024-01", huge)],
            vouchers=[fx.voucher("V1", "2024-01", huge)],
            clearings=[fx.clearing("ACC-1", "F1", "V1", huge)],
            plans=[fx.plan("PL1", "V1", "PRJ-A", "A", weight_a),
                   fx.plan("PL1", "V1", "PRJ-A", "B", weight_b)],
            bank_control=[fx.control("bank", "CNY", "outflow", 1, huge)],
            voucher_control=[fx.control("voucher", "CNY", "outflow", 1,
                                        huge)])
        close_month(pkg, self.ledger)
        payload = read_month(self.ledger, "2024-01")
        lines = payload["rows"]["payment_allocations"]
        self.assertEqual(sum(r["amount_minor"] for r in lines), huge)
        denom = weight_a + weight_b
        base_a = huge * weight_a // denom
        base_b = huge * weight_b // denom
        rem_a = huge * weight_a % denom
        rem_b = huge * weight_b % denom
        leftover = huge - base_a - base_b
        extra_a = 1 if (leftover == 2
                        or (leftover == 1 and rem_a > rem_b)) else 0
        self.assertEqual(lines[0]["amount_minor"], base_a + extra_a)
        self.assertEqual(lines[1]["amount_minor"], base_b + (1 - extra_a))
        # The stored remainder is the exact product's remainder, not a
        # float residue.
        self.assertEqual(lines[0]["remainder"], rem_a)

    def test_rates_stay_exact(self):
        self.assertEqual(parse_rate("1/3"), Fraction(1, 3))
        self.assertEqual(parse_rate("0.25"), Fraction(1, 4))
        self.assertEqual(format_rate(Fraction(1, 3)), "1/3")
        self.assertEqual(format_rate(Fraction(1, 8)), "0.125")
        with self.assertRaises(MoneyError):
            parse_rate("0")
        with self.assertRaises(MoneyError):
            parse_positive_int(0)
        with self.assertRaises(MoneyError):
            parse_positive_int("1,000")

    def test_ratio_residue_blocks_instead_of_rounding(self):
        close_month(FIX / "january", self.ledger)
        pkg = self.package(
            "residue", month="2024-02",
            flows=[fx.flow("ACC-1", "F1", "2024-02", 1,
                           direction="inflow")],
            vouchers=[fx.voucher("RZ", "2024-02", 1, kind="refund",
                                 direction="inflow", original="P",
                                 rate="1/3")],
            clearings=[fx.clearing("ACC-1", "F1", "RZ", 1)],
            plans=[],
            bank_control=[fx.control("bank", "CNY", "inflow", 1, 1)],
            voucher_control=[fx.control("voucher", "CNY", "inflow", 1, 1)])
        codes = {i["code"] for i in self.closing_issues(pkg)}
        self.assertIn("refund_ratio_residue", codes)

    def test_refund_amount_must_equal_the_exact_ratio(self):
        close_month(FIX / "january", self.ledger)
        pkg = self.package(
            "ratemismatch", month="2024-02",
            flows=[fx.flow("ACC-1", "F1", "2024-02", 2,
                           direction="inflow")],
            vouchers=[fx.voucher("RM", "2024-02", 2, kind="refund",
                                 direction="inflow", original="P",
                                 rate="0.2")],
            clearings=[fx.clearing("ACC-1", "F1", "RM", 2)],
            plans=[],
            bank_control=[fx.control("bank", "CNY", "inflow", 1, 2)],
            voucher_control=[fx.control("voucher", "CNY", "inflow", 1, 2)])
        codes = {i["code"] for i in self.closing_issues(pkg)}
        self.assertIn("refund_amount_mismatch", codes)


class TestRefundOrder(FundCase):
    def _refund_package(self, name: str, refunds: list[tuple[str, int]],
                        april_flow: int) -> Path:
        flows_ = [fx.flow("ACC-1", f"FA{i}", "2024-02", amount,
                          direction="inflow")
                  for i, (_, amount) in enumerate(refunds)]
        vouchers_ = []
        clearings_ = []
        for i, (vid, amount) in enumerate(refunds):
            vouchers_.append(fx.voucher(vid, "2024-02", amount, kind="refund",
                                        direction="inflow", original="P",
                                        rate=f"{amount}/5"))
            clearings_.append(fx.clearing("ACC-1", f"FA{i}", vid, amount))
        return self.package(
            name, month="2024-02", flows=flows_, vouchers=vouchers_,
            clearings=clearings_, plans=[],
            bank_control=[fx.control("bank", "CNY", "inflow",
                                     len(refunds), april_flow)],
            voucher_control=[fx.control("voucher", "CNY", "inflow",
                                        len(refunds), april_flow)])

    def test_two_refunds_apply_in_voucher_number_order(self):
        close_month(FIX / "january", self.ledger)
        # B (2 of P) is exhausted by the first 2; the second 1 goes to A.
        pkg = self._refund_package("order", [("R2", 1), ("R1", 2)], 3)
        close_month(pkg, self.ledger)
        payload = read_month(self.ledger, "2024-02")
        rows = [(r["refund_voucher_id"], r["section_id"], r["amount_minor"])
                for r in payload["rows"]["refund_allocations"]]
        self.assertEqual(rows, [("R1", "B", -2), ("R2", "A", -1)])
        summary = {r["section_id"]: r["refund_minor"]
                   for r in payload["rows"]["month_summary"]}
        self.assertEqual(summary, {"A": 1, "B": 2})

    def test_read_order_does_not_change_the_result(self):
        close_month(FIX / "january", self.ledger)
        pkg_a = self._refund_package("order_a", [("R2", 1), ("R1", 2)], 3)
        close_month(pkg_a, self.ledger)
        first = read_month(self.ledger, "2024-02")["rows"][
            "refund_allocations"]

        other = self.tmp / "ledger2"
        close_month(FIX / "january", other)
        # Same rows, reversed line order in every file.
        base = self.tmp / "order_b"
        pkg_b = self._refund_package("order_b", [("R1", 2), ("R2", 1)], 3)
        for name in ("vouchers.jsonl", "clearing.jsonl",
                     "bank_flows.jsonl"):
            lines = (pkg_b / name).read_text(encoding="utf-8").splitlines()
            (pkg_b / name).write_text("\n".join(reversed(lines)) + "\n",
                                      encoding="utf-8")
        close_month(pkg_b, other)
        second = read_month(other, "2024-02")["rows"]["refund_allocations"]
        self.assertEqual(
            [(r["refund_voucher_id"], r["section_id"], r["amount_minor"])
             for r in first],
            [(r["refund_voucher_id"], r["section_id"], r["amount_minor"])
             for r in second])


class TestDiskStaging(FundCase):
    """A large month must not be held in memory while it is staged."""

    def _build_big(self, n: int) -> Path:
        flows_, vouchers_, clearings_, plans = [], [], [], []
        for i in range(n):
            acc = f"ACC-{i % 4:02d}"
            flow_id = f"F{i:06d}"
            vid = f"V{i:06d}"
            amount = 100 + i
            flows_.append(fx.flow(acc, flow_id, "2024-01", amount))
            vouchers_.append(fx.voucher(vid, "2024-01", amount))
            clearings_.append(fx.clearing(acc, flow_id, vid, amount))
            plans.append(fx.plan(f"PL{i:06d}", vid, "PRJ-A", "A", 1))
        controls_bank = []
        for i in range(4):
            acc = f"ACC-{i:02d}"
            group = [r for r in flows_ if r["account_id"] == acc]
            controls_bank.append(fx.control(
                "bank", "CNY", "outflow", len(group),
                sum(r["amount_minor"] for r in group), account=acc))
        controls_voucher = [fx.control(
            "voucher", "CNY", "outflow", len(vouchers_),
            sum(r["amount_minor"] for r in vouchers_), account=None)]
        return self.package("big", flows=flows_, vouchers=vouchers_,
                            clearings=clearings_, plans=plans,
                            bank_control=controls_bank,
                            voucher_control=controls_voucher)

    def test_peak_memory_does_not_grow_with_month_size(self):
        if os.environ.get("BIDFACT_SKIP_MEMORY"):
            self.skipTest("memory test disabled via env")
        peaks = {}
        for label, n in (("small", 200), ("large", 4000)):
            pkg = self._build_big(n)
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            report = trial_close(pkg, self.tmp / f"led-{label}")
            peaks[label] = resource.getrusage(
                resource.RUSAGE_SELF).ru_maxrss - before
            self.assertTrue(report.ok, report.issues[:3])
            self.assertGreaterEqual(report.counts["clearings"], n)
        # 20x more rows must not mean 20x more resident memory; the
        # staging SQLite file does the holding, not Python objects.
        self.assertLess(peaks["large"], max(peaks["small"] * 4,
                                            peaks["small"] + 300000),
                        peaks)
        self.assertFalse((self.tmp / "led-small" / "fund.sqlite3").exists())


class TestCli(FundCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(CLI), *args],
                              capture_output=True, text=True, timeout=120)

    def test_trial_close_read_round_trip(self):
        trial = self.run_cli("fund", "trial", "--ledger", str(self.ledger),
                             "--package", str(FIX / "january"))
        self.assertEqual(trial.returncode, 0, trial.stderr)
        payload = json.loads(trial.stdout)
        self.assertTrue(payload["ok"])
        self.assertFalse((self.ledger / "fund.sqlite3").exists())
        closed = self.run_cli("fund", "close", "--ledger", str(self.ledger),
                              "--package", str(FIX / "january"))
        self.assertEqual(closed.returncode, 0, closed.stderr)
        month = self.run_cli("fund", "month", "--ledger", str(self.ledger),
                             "--month", "2024-01")
        self.assertEqual(month.returncode, 0, month.stderr)
        data = json.loads(month.stdout)
        self.assertEqual(len(data["rows"]["payment_allocations"]), 3)

    def test_blocked_close_exits_nonzero_with_issues(self):
        for month in ("january", "february"):
            self.run_cli("fund", "close", "--ledger", str(self.ledger),
                         "--package", str(FIX / month))
        out = self.run_cli("fund", "close", "--ledger", str(self.ledger),
                           "--package", str(FIX / "march_over"))
        self.assertEqual(out.returncode, 1)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["error"], "close_blocked")
        self.assertTrue(any(i["code"] == "refund_exceeds_remaining"
                            for i in payload["issues"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
