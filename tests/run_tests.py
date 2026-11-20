"""Standard-library test suite for the bidfact processor.

Run from the repository root with:  python3 -I tests/run_tests.py
Small fixtures are committed under fixtures/; the large synthetic
corpus is generated at runtime (see test_memory_scaling).
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bidfact.export import export_all  # noqa: E402
from bidfact.ingestion import verify_span  # noqa: E402
from bidfact.parse_dates import find_date, parse_date  # noqa: E402
from bidfact.parse_money import parse_amount, wan_to_yuan  # noqa: E402
from bidfact.pipeline import RULE_VERSION, run_pipeline  # noqa: E402
from bidfact.storage import Store  # noqa: E402
from bidfact.timeline import (current_facts, document_statements,  # noqa: E402
                              timeline)
from bidfact.util import is_decimal  # noqa: E402

FIX = ROOT / "fixtures"


class PipelineMixin:
    def setUp(self) -> None:  # noqa: D401 - unittest hook
        self.tmp = Path(tempfile.mkdtemp(prefix="bidfact-test-"))
        self.results = self.tmp / "results"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_collection(self, collection: str, with_relations: bool = True
                       ) -> str:
        col = FIX / collection
        rel = col / "relations.json"
        if not with_relations or not rel.is_file():
            rel = None
        out = run_pipeline(col, self.results, rel, force_rebuild=True)
        return out["path"]

    @staticmethod
    def facts(result_dir: str, t=None, section=None):
        store = Store(Path(result_dir) / "store.sqlite3")
        try:
            return current_facts(store, t=t, section_id=section)
        finally:
            store.close()

    @staticmethod
    def fact_map(result_dir: str, t=None):
        out = {}
        for f in PipelineMixin.facts(result_dir, t):
            out[(f["section_id"], f["field"])] = f
        return out


class TestParsingUnits(unittest.TestCase):
    def test_wan_exact_decimal(self):
        self.assertEqual(wan_to_yuan("100"), "1000000")
        self.assertEqual(wan_to_yuan("120.5"), "1205000")
        self.assertEqual(wan_to_yuan("0.0001"), "1")
        self.assertEqual(wan_to_yuan("200.50"), "2005000")
        self.assertTrue(is_decimal("0.0001"))

    def test_money_forms(self):
        self.assertEqual(parse_amount("100.00 万元").amount, "1000000")
        self.assertEqual(parse_amount("100.00 万元").currency, "CNY")
        bare = parse_amount("12345")
        self.assertEqual(bare.amount, "12345")
        self.assertEqual(bare.currency, "unknown_currency")
        prefixed = parse_amount("人民币 880,000 元")
        self.assertEqual(prefixed.amount, "880000")
        self.assertEqual(prefixed.currency, "CNY")
        self.assertIsNone(parse_amount("壹佰万元整"))
        self.assertIsNone(parse_amount("约 50 万元"))

    def test_header_unit_inheritance(self):
        p = parse_amount("100.00", "预算金额（万元）")
        self.assertEqual(p.amount, "1000000")
        self.assertEqual(p.unit_raw, "header:万元")

    def test_date_year_backfill_uses_publish_year_only(self):
        self.assertEqual(parse_date("8月9日", 2024), "2024-08-09")
        self.assertEqual(parse_date("8月9日", 1999), "1999-08-09")
        self.assertEqual(parse_date("2024年2月15日", 2030), "2024-02-15")
        self.assertEqual(parse_date("2024/2/1", 2030), "2024-02-01")
        self.assertIsNone(parse_date("下月底", 2024))
        s, e, norm = find_date("截止 8月9日 递交", 2024)
        self.assertEqual(norm, "2024-08-09")
        self.assertEqual("截止 8月9日 递交"[s:e], "8月9日")


class TestWalkthrough(PipelineMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expected = json.loads(
            (FIX / "walkthrough" / "expected_walkthrough.json").read_text(
                encoding="utf-8"))

    def test_full_point_in_time_expectations(self):
        rd = self.run_collection("walkthrough")
        for t, sections in self.expected["points_in_time"].items():
            fm = self.fact_map(rd, t=t)
            for sec, fields in sections.items():
                for field, exp in fields.items():
                    got = fm[(sec, field)]
                    self.assertEqual(got["status"], exp["status"],
                                     f"{t} {sec}.{field}")
                    if exp["status"] == "value":
                        self.assertEqual(got["value"]["normalized"],
                                         exp["normalized"])
                        if "currency" in exp:
                            self.assertEqual(got["value"]["currency"],
                                             exp["currency"])
                        # All provenance stays attached; document identity
                        # is compared as a set because one document may
                        # legitimately hold several same-value slices.
                        prov_docs = {p["document_id"]
                                     for p in got["provenance"]}
                        self.assertEqual(prov_docs,
                                         set(exp["provenance_documents"]))
                        self.assertTrue(all(
                            p["span"]["text"] for p in got["provenance"]))
                    elif exp["status"] == "conflict":
                        got_cands = sorted(
                            (c["document_id"], c["normalized"])
                            for c in got["candidates"])
                        exp_cands = sorted(
                            (c["document_id"], c["normalized"])
                            for c in exp["candidates"])
                        self.assertEqual(got_cands, exp_cands)

    def test_document_B_has_no_budget_statement(self):
        rd = self.run_collection("walkthrough")
        store = Store(Path(rd) / "store.sqlite3")
        try:
            stmt = document_statements(store, "docB")
        finally:
            store.close()
        self.assertEqual(sorted(a["field"] for a in stmt["assertions"]),
                         self.expected["document_B_own_fields"])

    def test_closures_target_expected_assertions(self):
        rd = self.run_collection("walkthrough")
        store = Store(Path(rd) / "store.sqlite3")
        try:
            closures = list(store.iter_closures())
            by_rel = {}
            for c in closures:
                target = store.get_assertion(c["target_assertion_id"])
                by_rel[c["relation_id"]] = (c["verb"],
                                            target["document_id"],
                                            target["field"])
        finally:
            store.close()
        for rid, exp in self.expected["closure_targets"].items():
            self.assertIn(rid, by_rel)
            verb, doc, field = by_rel[rid]
            self.assertEqual(verb, exp["verb"])
            self.assertEqual(doc, exp["target_document"])
            self.assertEqual(field, exp["target_field"])

    def test_manual_timeline_matches_engine_events(self):
        rd = self.run_collection("walkthrough")
        store = Store(Path(rd) / "store.sqlite3")
        try:
            events = timeline(store, section_id="L-01")
        finally:
            store.close()
        # 6 assertions; B's single replace closes BOTH deadline slices
        # docA asserted (材料截止日期 and 投标截止时间), then D/E/F each
        # close one -> 5 closures total.
        self.assertEqual(sum(1 for e in events if e["kind"] == "assertion"), 6)
        self.assertEqual(sum(1 for e in events if e["kind"] == "closure"), 5)
        self.assertEqual([e["at"] for e in events],
                         sorted(e["at"] for e in events))
        # B's replace closure references two distinct target assertions.
        b_closures = [e for e in events
                      if e["kind"] == "closure"
                      and e["event"]["relation_id"] == "rel-B-deadline"]
        self.assertEqual(len(b_closures), 2)
        self.assertEqual(
            {e["event"]["target_document_id"] for e in b_closures}, {"docA"})

    def test_evidence_slices_reopen_in_source(self):
        rd = self.run_collection("walkthrough")
        store = Store(Path(rd) / "store.sqlite3")
        try:
            for r in store.iter_assertions():
                span = json.loads(r["span_json"])
                doc = json.loads(store.get_document(
                    r["document_id"])["raw_json"])
                page_file = FIX / "walkthrough" / doc["source"]["file"]
                page_text = json.loads(
                    page_file.read_text(encoding="utf-8"))
                text = next(p["text"] for p in page_text["pages"]
                            if p["page_no"] == span["page_no"])
                self.assertEqual(text[span["start"]:span["end"]],
                                 span["text"], r["assertion_id"])
        finally:
            store.close()


class TestTableScope(PipelineMixin, unittest.TestCase):
    def test_shared_header_three_sections(self):
        rd = self.run_collection("table_scope")
        fm = self.fact_map(rd)
        self.assertEqual(fm[("L-01", "budget")]["value"]["normalized"],
                         "1000000")
        self.assertEqual(fm[("L-02", "budget")]["value"]["normalized"],
                         "2005000")
        # Continuation page joined on explicit marker evidence.
        self.assertEqual(fm[("L-03", "budget")]["value"]["normalized"],
                         "3000000")
        self.assertEqual(fm[("L-03", "result_unit")]["value"]["normalized"],
                         "丙公司")
        for sec in ("L-01", "L-02", "L-03"):
            self.assertEqual(fm[(sec, "result_unit")]["status"], "value")

    def test_continuation_scope_basis_records_marker(self):
        rd = self.run_collection("table_scope")
        store = Store(Path(rd) / "store.sqlite3")
        try:
            rows = list(store.conn.execute(
                "SELECT scope_basis FROM assertions WHERE section_id='L-03'"))
        finally:
            store.close()
        self.assertTrue(any("continuation evidence" in r["scope_basis"]
                            for r in rows))


class TestNoContinuation(PipelineMixin, unittest.TestCase):
    def test_claimed_continuation_without_marker_is_not_chained(self):
        rd = self.run_collection("no_continuation")
        store = Store(Path(rd) / "store.sqlite3")
        try:
            rows = list(store.conn.execute(
                "SELECT section_id, scope_basis FROM assertions"))
        finally:
            store.close()
        # Both sections still parse because each table has its own header;
        # but no assertion may claim cross-page continuation evidence.
        self.assertEqual({r["section_id"] for r in rows}, {"L-01", "L-02"})
        self.assertFalse(any("continuation evidence" in r["scope_basis"]
                             for r in rows))


class TestGrammarFixture(PipelineMixin, unittest.TestCase):
    def test_precision_backfill_and_unknown_currency(self):
        rd = self.run_collection("grammar")
        fm = self.fact_map(rd)
        budget = fm[("G-01", "budget")]
        # Two distinct budget statements (one CNY in 万元, one bare
        # number with no currency) cannot be reconciled -> conflict,
        # and the bare number keeps unknown_currency rather than CNY.
        self.assertEqual(budget["status"], "conflict")
        cands = {(c["normalized"], c["currency"])
                 for c in budget["candidates"]}
        self.assertIn(("1", "CNY"), cands)           # 0.0001 万元 exact
        self.assertIn(("12345", "unknown_currency"), cands)
        self.assertEqual(fm[("G-01", "deadline")]["value"]["normalized"],
                         "2024-08-09")  # publish year 2024, never system year
        self.assertEqual(fm[("G-01", "result_amount")]["value"]["normalized"],
                         "880000")

    def test_unsupported_expressions_kept_as_unparsed(self):
        rd = self.run_collection("grammar")
        store = Store(Path(rd) / "store.sqlite3")
        try:
            rows = list(store.iter_unparsed())
        finally:
            store.close()
        reasons = " ".join(r["reason"] for r in rows)
        raws = " ".join(r["raw_text"] for r in rows)
        self.assertIn("capital-number", reasons)
        self.assertIn("range", reasons)
        self.assertIn("hedged", reasons)
        self.assertIn("relative date", reasons)
        self.assertIn("壹佰万", raws)
        self.assertIn("下月底", raws)

    def test_bare_number_is_unknown_currency(self):
        rd = self.run_collection("grammar")
        store = Store(Path(rd) / "store.sqlite3")
        try:
            rows = list(store.conn.execute(
                "SELECT raw_value, currency FROM assertions "
                "WHERE raw_value='12345'"))
        finally:
            store.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["currency"], "unknown_currency")


class TestRepost(PipelineMixin, unittest.TestCase):
    def test_same_title_without_mapping_not_merged(self):
        rd = self.run_collection("repost", with_relations=False)
        store = Store(Path(rd) / "store.sqlite3")
        try:
            docs = [r["document_id"] for r in store.all_documents()]
            assertions = list(store.iter_assertions())
            links = list(store.conn.execute(
                "SELECT document_id FROM same_notice WHERE notice_id='N-R'"))
        finally:
            store.close()
        self.assertEqual(sorted(docs), ["docR1", "docR2"])
        # Two independent assertions, no cross-document merging.
        self.assertEqual(len(assertions), 2)
        self.assertEqual({r["document_id"] for r in assertions},
                         {"docR1", "docR2"})
        # Explicit same_notice mapping is recorded for provenance linkage.
        self.assertEqual({r["document_id"] for r in links},
                         {"docR1", "docR2"})


class TestRelationEdges(PipelineMixin, unittest.TestCase):
    def test_pending_invalid_and_no_closures(self):
        rd = self.run_collection("relations_edges")
        store = Store(Path(rd) / "store.sqlite3")
        try:
            rels = {r["relation_id"]: r for r in store.all_relations()}
            n_closures = store.count("closures")
        finally:
            store.close()
        self.assertEqual(rels["rel-missing-target"]["status"], "pending")
        self.assertEqual(rels["rel-future"]["status"], "invalid")
        self.assertEqual(rels["rel-x1-x2"]["status"], "invalid")
        self.assertEqual(rels["rel-x2-x1"]["status"], "invalid")
        self.assertIn("cycle", rels["rel-x1-x2"]["detail"])
        self.assertEqual(n_closures, 0)

    def test_conflict_survives_invalid_relations(self):
        rd = self.run_collection("relations_edges")
        fm = self.fact_map(rd)
        fact = fm[("X-01", "budget")]
        self.assertEqual(fact["status"], "conflict")
        self.assertEqual(len(fact["candidates"]), 3)


class TestUnpaired(PipelineMixin, unittest.TestCase):
    def test_unpaired_amounts_are_pending_with_candidates(self):
        rd = self.run_collection("unpaired", with_relations=False)
        fm = self.fact_map(rd)
        fact = fm[(None, "budget")]
        self.assertEqual(fact["status"], "pending")
        self.assertEqual(len(fact["pending"]), 2)
        self.assertEqual(
            sorted(p["raw_value"] for p in fact["pending"]),
            ["100 万元", "200 万元"])
        self.assertTrue(all(
            p["pending_reason"] == "scope_not_unique" for p in fact["pending"]))

    def test_pending_material_distributed_to_candidate_sections(self):
        rd = self.run_collection("unpaired", with_relations=False)
        facts = self.facts(rd)
        for sec in ("U-01", "U-02"):
            f = next(x for x in facts
                     if x["section_id"] == sec and x["field"] == "budget")
            self.assertEqual(f["status"], "pending")
            self.assertEqual(
                sorted(p["raw_value"] for p in f["pending"]),
                ["100 万元", "200 万元"])

    def test_pending_material_respects_point_in_time(self):
        import hashlib
        col = self.tmp / "timefilter"
        (col / "pages").mkdir(parents=True)

        def make_doc(did, pub, text, secs):
            payload = {"document_id": did,
                       "pages": [{"page_no": 1, "text": text}]}
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            rel = Path("pages") / f"{did}.json"
            (col / rel).write_bytes(data)
            return {"document_id": did, "notice_id": f"N-{did}", "site": "s1",
                    "project_id": "P", "section_ids": secs,
                    "published_at": pub,
                    "source": {"file": str(rel),
                               "sha256": hashlib.sha256(data).hexdigest()}}

        early = make_doc(
            "docE", "2024-01-01T09:00:00",
            "含两个标段 U-01、U-02（清单约束）。\n预算金额：100 万元\n"
            "另一预算金额：200 万元\n", ["U-01", "U-02"])
        late = make_doc("docL", "2024-05-01T09:00:00",
                        "标段编号：U-01\n采购预算：50 万元\n", ["U-01"])
        (col / "manifest.json").write_text(
            json.dumps({"collection": "timefilter",
                        "documents": [early, late]}, ensure_ascii=False),
            encoding="utf-8")
        out = run_pipeline(col, self.results, None, force_rebuild=True)
        store = Store(Path(out["path"]) / "store.sqlite3")
        try:
            jan = {f["section_id"]: f for f in current_facts(
                store, t="2024-02-01T00:00:00") if f["field"] == "budget"}
            jun = {f["section_id"]: f for f in current_facts(
                store, t="2024-06-01T00:00:00") if f["field"] == "budget"}
        finally:
            store.close()
        # Before docL: only pending material, never a fabricated fact.
        self.assertEqual(jan["U-01"]["status"], "pending")
        self.assertEqual(len(jan["U-01"]["pending"]), 2)
        self.assertEqual(jan["U-01"]["value"], None)
        # After docL: its confirmed value becomes the current fact.
        self.assertEqual(jun["U-01"]["status"], "value")
        self.assertEqual(jun["U-01"]["value"]["normalized"], "500000")
        # The other section never received a confirmed value.
        self.assertEqual(jun["U-02"]["status"], "pending")


class TestBadSlice(PipelineMixin, unittest.TestCase):
    def test_misaligned_cell_rejected_and_recorded(self):
        rd = self.run_collection("bad_slice", with_relations=False)
        store = Store(Path(rd) / "store.sqlite3")
        try:
            issues = list(store.conn.execute(
                "SELECT kind FROM ingestion_issues"))
            cell_assertions = list(store.conn.execute(
                "SELECT * FROM assertions WHERE json_extract(span_json, "
                "'$.source_kind')='cell'"))
        finally:
            store.close()
        self.assertEqual([i["kind"] for i in issues], ["slice_mismatch"])
        self.assertEqual(cell_assertions, [])

    def test_verify_span_helper(self):
        pages = {1: "abcdefgh"}
        self.assertTrue(verify_span(pages, 1, 2, 5, "cde"))
        self.assertFalse(verify_span(pages, 1, 2, 5, "xxx"))
        self.assertFalse(verify_span(pages, 2, 0, 1, "a"))


class TestDeterminism(PipelineMixin, unittest.TestCase):
    def _run_shuffled(self, order):
        col_src = FIX / "walkthrough"
        work = self.tmp / f"shuf-{order[0]}"
        shutil.copytree(col_src, work)
        manifest = json.loads((work / "manifest.json").read_text("utf-8"))
        manifest["documents"] = [manifest["documents"][i] for i in order]
        (work / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return run_pipeline(work, self.results, work / "relations.json",
                            force_rebuild=True)

    def test_order_independence_and_result_id_stability(self):
        first = run_pipeline(FIX / "walkthrough", self.results,
                             FIX / "walkthrough" / "relations.json",
                             force_rebuild=True)
        shuffled = self._run_shuffled([5, 0, 3, 1, 4, 2])
        # The manifest bytes change when reordered, so a new result number
        # is expected; but the business facts and assertion ids must match.
        self.assertNotEqual(first["result_id"], shuffled["result_id"])
        f_store = Store(Path(first["path"]) / "store.sqlite3")
        s_store = Store(Path(shuffled["path"]) / "store.sqlite3")
        try:
            f_ids = sorted(r["assertion_id"] for r in f_store.iter_assertions())
            s_ids = sorted(r["assertion_id"] for r in s_store.iter_assertions())
            self.assertEqual(f_ids, s_ids)
            ff = current_facts(f_store)
            sf = current_facts(s_store)
            self.assertEqual(
                [(x["section_id"], x["field"], x["status"],
                  x.get("value")) for x in ff],
                [(x["section_id"], x["field"], x["status"],
                  x.get("value")) for x in sf])
        finally:
            f_store.close()
            s_store.close()

    def test_rerun_reuses_result_and_creates_no_duplicates(self):
        first = run_pipeline(FIX / "walkthrough", self.results,
                             FIX / "walkthrough" / "relations.json",
                             force_rebuild=True)
        second = run_pipeline(FIX / "walkthrough", self.results,
                              FIX / "walkthrough" / "relations.json")
        self.assertTrue(second["reused"])
        self.assertEqual(first["result_id"], second["result_id"])
        store = Store(Path(first["path"]) / "store.sqlite3")
        try:
            n1 = store.count("assertions")
        finally:
            store.close()
        third = run_pipeline(FIX / "walkthrough", self.results,
                             FIX / "walkthrough" / "relations.json")
        store = Store(Path(third["path"]) / "store.sqlite3")
        try:
            self.assertEqual(store.count("assertions"), n1)
        finally:
            store.close()

    def test_export_writes_all_read_entrances(self):
        rd = self.run_collection("walkthrough")
        files = export_all(Path(rd))
        for name in ("facts.json", "conflicts.json", "evidence.json",
                     "relations.json", "statements.json", "timeline.json"):
            self.assertIn(name, files)
            payload = json.loads(files[name].read_text(encoding="utf-8"))
            self.assertEqual(payload["result_id"], Path(rd).name)


class TestMaterialConflict(PipelineMixin, unittest.TestCase):
    def test_same_document_id_different_bytes_aborts(self):
        work = self.tmp / "conflict"
        shutil.copytree(FIX / "grammar", work)
        manifest = json.loads((work / "manifest.json").read_text("utf-8"))
        # Tamper a page file without updating the declared digest.
        page = work / "pages" / "docG.json"
        payload = json.loads(page.read_text(encoding="utf-8"))
        payload["pages"][0]["text"] += "被篡改的额外内容"
        page.write_text(json.dumps(payload, ensure_ascii=False),
                        encoding="utf-8")
        from bidfact.errors import MaterialConflict
        with self.assertRaises(MaterialConflict):
            run_pipeline(work, self.results, force_rebuild=True)

    def test_manifest_level_same_id_conflicting_digests_aborts(self):
        work = self.tmp / "id-conflict"
        shutil.copytree(FIX / "repost", work)
        manifest = json.loads((work / "manifest.json").read_text("utf-8"))
        # Two entries sharing document_id but declaring different digests.
        manifest["documents"][1]["document_id"] = \
            manifest["documents"][0]["document_id"]
        manifest["documents"][1]["source"] = dict(
            manifest["documents"][0]["source"])
        manifest["documents"][1]["source"]["sha256"] = "ff" * 32
        (work / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        from bidfact.errors import MaterialConflict
        with self.assertRaises(MaterialConflict):
            run_pipeline(work, self.results, force_rebuild=True)


class TestQuoteBackreference(PipelineMixin, unittest.TestCase):
    def test_bad_quote_slice_makes_relation_pending(self):
        import hashlib
        work = self.tmp / "quote"
        shutil.copytree(FIX / "walkthrough", work)
        rel_path = work / "relations.json"
        rels = json.loads(rel_path.read_text(encoding="utf-8"))
        # Corrupt B's quote so it no longer slices B's own page.
        for item in rels["relations"]:
            if item["relation_id"] == "rel-B-deadline":
                item["quote"][0]["text"] = "原文中根本不存在的措辞"
        rel_path.write_text(json.dumps(rels, ensure_ascii=False),
                            encoding="utf-8")
        # The relations file digest changed, so rebuild explicitly.
        out = run_pipeline(work, self.results, rel_path, force_rebuild=True)
        store = Store(Path(out["path"]) / "store.sqlite3")
        try:
            rel = next(r for r in store.all_relations()
                       if r["relation_id"] == "rel-B-deadline")
            closures = list(store.conn.execute(
                "SELECT * FROM closures WHERE relation_id='rel-B-deadline'"))
        finally:
            store.close()
        self.assertEqual(rel["status"], "pending")
        self.assertIn("quote", rel["detail"])
        self.assertEqual(closures, [])  # nothing closed without backreference


class TestRelationSemantics(PipelineMixin, unittest.TestCase):
    """Relation-authority semantics from the specification, end to end."""

    def _mini_collection(self, name: str, texts: dict, relations: list):
        import hashlib
        col = self.tmp / name
        (col / "pages").mkdir(parents=True)
        docs = []
        for did, text in texts.items():
            payload = {"document_id": did,
                       "pages": [{"page_no": 1, "text": text}]}
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            rel = Path("pages") / f"{did}.json"
            (col / rel).write_bytes(data)
            docs.append({
                "document_id": did, "notice_id": f"N-{did}", "site": "s1",
                "project_id": "P", "section_ids": ["S-1"],
                "published_at": text.split("发布时间：")[1].split("\n")[0],
                "source": {"file": str(rel),
                           "sha256": hashlib.sha256(data).hexdigest()}})
        (col / "manifest.json").write_text(
            json.dumps({"collection": name, "documents": docs},
                       ensure_ascii=False), encoding="utf-8")
        (col / "relations.json").write_text(
            json.dumps({"relations": relations}, ensure_ascii=False),
            encoding="utf-8")
        return col

    @staticmethod
    def _quote(text: str, needle: str) -> dict:
        i = text.index(needle)
        return {"page_no": 1, "start": i, "end": i + len(needle),
                "text": needle}

    def test_pending_item_never_blocks_independent_confirmed_item(self):
        a = ("公告A\n发布时间：2024-01-01T09:00:00\n标段编号：S-1\n"
             "采购预算：100 万元\n材料截止日期：2024年2月1日\n")
        b = ("公告B\n发布时间：2024-02-01T09:00:00\n标段编号：S-1\n"
             "材料截止日期：2024年3月1日\n")
        rels = [
            {"relation_id": "r-bad", "acting_document_id": "docB",
             "verb": "replace", "target_document_id": "docGHOST",
             "target_sections": ["S-1"], "fields": ["budget"],
             "quote": [self._quote(b, "2024年3月1日")]},
            {"relation_id": "r-ok", "acting_document_id": "docB",
             "verb": "replace", "target_document_id": "docA",
             "target_sections": ["S-1"], "fields": ["deadline"],
             "quote": [self._quote(b, "2024年3月1日")]}]
        col = self._mini_collection(
            "indep", {"docA": a, "docB": b}, rels)
        out = run_pipeline(col, self.results, col / "relations.json",
                           force_rebuild=True)
        store = Store(Path(out["path"]) / "store.sqlite3")
        try:
            statuses = {r["relation_id"]: r["status"]
                        for r in store.all_relations()}
            fm = {(f["section_id"], f["field"]): f
                  for f in current_facts(store)}
        finally:
            store.close()
        self.assertEqual(statuses,
                         {"r-bad": "pending", "r-ok": "confirmed"})
        self.assertEqual(fm[("S-1", "budget")]["status"], "value")
        self.assertEqual(fm[("S-1", "deadline")]["value"]["normalized"],
                         "2024-03-01")

    def test_closing_both_conflicting_assertions_yields_withdrawn(self):
        a = ("公告A\n发布时间：2024-01-01T09:00:00\n标段编号：S-1\n"
             "采购预算：100 万元\n")
        c = ("公告C\n发布时间：2024-03-01T09:00:00\n标段编号：S-1\n"
             "采购预算：120 万元\n")
        d = ("公告D\n发布时间：2024-04-01T09:00:00\n标段编号：S-1\n"
             "撤回前述两份预算表述。\n")
        rels = [
            {"relation_id": "r-a", "acting_document_id": "docD",
             "verb": "retract", "target_document_id": "docA",
             "target_sections": ["S-1"], "fields": ["budget"],
             "quote": [self._quote(d, "撤回前述两份预算表述")]},
            {"relation_id": "r-c", "acting_document_id": "docD",
             "verb": "retract", "target_document_id": "docC",
             "target_sections": ["S-1"], "fields": ["budget"],
             "quote": [self._quote(d, "撤回前述两份预算表述")]}]
        col = self._mini_collection("both", {"docA": a, "docC": c,
                                              "docD": d}, rels)
        out = run_pipeline(col, self.results, col / "relations.json",
                           force_rebuild=True)
        store = Store(Path(out["path"]) / "store.sqlite3")
        try:
            fm = {(f["section_id"], f["field"]): f
                  for f in current_facts(store)}
        finally:
            store.close()
        self.assertEqual(fm[("S-1", "budget")]["status"], "withdrawn")

    def test_closing_one_conflict_leaves_other_as_sole_fact(self):
        a = ("公告A\n发布时间：2024-01-01T09:00:00\n标段编号：S-1\n"
             "采购预算：100 万元\n")
        c = ("公告C\n发布时间：2024-03-01T09:00:00\n标段编号：S-1\n"
             "采购预算：120 万元\n")
        d = ("公告D\n发布时间：2024-04-01T09:00:00\n标段编号：S-1\n"
             "撤回补充公告预算。\n")
        rels = [
            {"relation_id": "r-c-only", "acting_document_id": "docD",
             "verb": "retract", "target_document_id": "docC",
             "target_sections": ["S-1"], "fields": ["budget"],
             "quote": [self._quote(d, "撤回补充公告预算")]}]
        col = self._mini_collection("one", {"docA": a, "docC": c,
                                             "docD": d}, rels)
        out = run_pipeline(col, self.results, col / "relations.json",
                           force_rebuild=True)
        store = Store(Path(out["path"]) / "store.sqlite3")
        try:
            fm = {(f["section_id"], f["field"]): f
                  for f in current_facts(store)}
        finally:
            store.close()
        fact = fm[("S-1", "budget")]
        self.assertEqual(fact["status"], "value")
        self.assertEqual(fact["value"]["normalized"], "1000000")
        self.assertEqual([p["document_id"] for p in fact["provenance"]],
                         ["docA"])

    def test_supplement_does_not_close_old_value(self):
        a = ("公告A\n发布时间：2024-01-01T09:00:00\n标段编号：S-1\n"
             "采购预算：100 万元\n")
        c = ("公告C\n发布时间：2024-03-01T09:00:00\n标段编号：S-1\n"
             "采购预算：120 万元\n")
        rels = [
            {"relation_id": "r-sup", "acting_document_id": "docC",
             "verb": "supplement", "target_document_id": "docA",
             "target_sections": ["S-1"], "fields": ["budget"],
             "quote": [self._quote(c, "120 万元")]}]
        col = self._mini_collection("sup", {"docA": a, "docC": c}, rels)
        out = run_pipeline(col, self.results, col / "relations.json",
                           force_rebuild=True)
        store = Store(Path(out["path"]) / "store.sqlite3")
        try:
            self.assertEqual(store.count("closures"), 0)
            fm = {(f["section_id"], f["field"]): f
                  for f in current_facts(store)}
        finally:
            store.close()
        self.assertEqual(fm[("S-1", "budget")]["status"], "conflict")

    def test_never_asserted_field_is_absent(self):
        a = ("公告A\n发布时间：2024-01-01T09:00:00\n标段编号：S-1\n"
             "采购预算：100 万元\n")
        col = self._mini_collection("abs", {"docA": a}, [])
        out = run_pipeline(col, self.results, None, force_rebuild=True)
        store = Store(Path(out["path"]) / "store.sqlite3")
        try:
            fm = {(f["section_id"], f["field"]): f
                  for f in current_facts(store)}
        finally:
            store.close()
        # result_unit was never mentioned -> absent, not empty string
        self.assertEqual(fm[("S-1", "result_unit")]["status"], "absent")
        self.assertIsNone(fm[("S-1", "result_unit")]["value"])


class TestMergedHeader(PipelineMixin, unittest.TestCase):
    def test_merged_multirow_header_inherits_wan_unit(self):
        rd = self.run_collection("merged_header", with_relations=False)
        store = Store(Path(rd) / "store.sqlite3")
        try:
            rows = list(store.conn.execute(
                "SELECT section_id, normalized, currency, header, scope_basis "
                "FROM assertions WHERE field='budget' ORDER BY section_id"))
        finally:
            store.close()
        self.assertEqual(len(rows), 2)
        by_sec = {r["section_id"]: r for r in rows}
        self.assertEqual(by_sec["M-01"]["normalized"], "1000000")
        self.assertEqual(by_sec["M-02"]["normalized"], "2000000")
        for r in rows:
            self.assertEqual(r["currency"], "CNY")
            self.assertIn("万元", r["header"])


class TestMemoryScaling(PipelineMixin, unittest.TestCase):
    """Large synthetic corpus, generated at runtime; peak RSS recorded."""

    def _build_corpus(self, n_docs: int) -> Path:
        import hashlib
        col = self.tmp / f"large-{n_docs}"
        pages_dir = col / "pages"
        pages_dir.mkdir(parents=True)
        docs = []
        for i in range(n_docs):
            did = f"docL{i:05d}"
            sec = f"S-{i % 50:02d}"
            text = (
                f"大样本公告 {i}\n发布时间：2024-01-{(i % 28) + 1:02d}T09:00:00\n"
                f"标段编号：{sec}\n"
                f"采购预算：{100 + i}.00 万元\n"
                f"材料截止日期：2024年{(i % 12) + 1}月15日\n"
                f"中标单位：供应商{i % 200:03d}号有限公司\n"
                f"中标金额：人民币 {900000 + i} 元\n")
            payload = {"document_id": did,
                       "pages": [{"page_no": 1, "text": text}]}
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            rel = Path("pages") / f"{did}.json"
            (col / rel).write_bytes(data)
            docs.append({
                "document_id": did, "notice_id": f"N-L{i:05d}",
                "site": "site1", "project_id": "P-LARGE",
                "section_ids": [sec],
                "published_at": f"2024-01-{(i % 28) + 1:02d}T09:00:00",
                "source": {"file": str(rel),
                           "sha256": hashlib.sha256(data).hexdigest()}})
        (col / "manifest.json").write_text(
            json.dumps({"collection": f"large-{n_docs}",
                        "documents": docs}, ensure_ascii=False),
            encoding="utf-8")
        return col

    def test_peak_memory_does_not_grow_linearly(self):
        if os.environ.get("BIDFACT_SKIP_MEMORY"):
            self.skipTest("memory test disabled via env")
        peaks = {}
        counts = {}
        for n in (50, 500):
            col = self._build_corpus(n)
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            out = run_pipeline(col, self.results, force_rebuild=True)
            store = Store(Path(out["path"]) / "store.sqlite3")
            try:
                counts[n] = store.count("assertions")
            finally:
                store.close()
            peaks[n] = resource.getrusage(
                resource.RUSAGE_SELF).ru_maxrss - before
        # 10x documents must not produce 10x resident growth. The store is
        # SQLite-backed and pages are streamed, so allow a generous 3x.
        self.assertGreater(counts[500], counts[50] * 5)
        self.assertLess(peaks[500], max(peaks[50] * 3, peaks[50] + 200000),
                        f"peak rss growth {peaks}")
        report = self.tmp / "memory_report.json"
        report.write_text(json.dumps(
            {"peak_rss_kb_delta": peaks, "assertion_counts": counts,
             "rule_version": RULE_VERSION}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
