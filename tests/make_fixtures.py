#!/usr/bin/env python3
"""Build the *small, committable* fixtures.

Offsets and sha256 digests are computed from the source text itself,
so every declared slice is a real slice. The manual business
expectations live separately in expected_walkthrough.json and are
never produced by the extractor.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump(path: Path, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"file": str(path.relative_to(path.parents[1])),
            "sha256": sha256_bytes(data)}


def page_doc(document_id: str, pages: list[tuple[int, str]]) -> dict:
    return {"document_id": document_id,
            "pages": [{"page_no": n, "text": t} for n, t in pages]}


def span_of(text: str, needle: str, occurrence: int = 0) -> tuple[int, int]:
    idx = -1
    for _ in range(occurrence + 1):
        idx = text.index(needle, idx + 1)
    return idx, idx + len(needle)


def cell(table_id: str, page_no: int, row: int, col: int, text: str,
         page_text: str, rowspan: int = 1, colspan: int = 1,
         occurrence: int = 0) -> dict:
    start, end = span_of(page_text, text, occurrence)
    return {"page_no": page_no, "row": row, "col": col, "text": text,
            "start": start, "end": end, "rowspan": rowspan,
            "colspan": colspan}


# ---------------------------------------------------------------- walkthrough
def build_walkthrough() -> Path:
    col = FIX / "walkthrough"
    col.mkdir(parents=True, exist_ok=True)

    a_text = (
        "某采购项目（项目编号 P-77）招标公告\n"
        "发布时间：2024-01-10T09:00:00\n"
        "标段编号：L-01\n"
        "采购预算：100.00 万元\n"
        "材料截止日期：2024年2月15日\n"
        "投标截止时间：2024年2月15日 09时30分\n"
        "采购人：某中心\n")
    b_text = (
        "某采购项目更正公告\n"
        "发布时间：2024-02-01T10:00:00\n"
        "标段编号：L-01\n"
        "现对原公告作如下更正：材料截止日期修改为 2024年3月20日。\n"
        "原公告其余内容不变。\n")
    c_text = (
        "某采购项目补充公告\n"
        "发布时间：2024-03-01T10:00:00\n"
        "标段编号：L-01\n"
        "补充说明：本标段采购预算调整参考金额 120 万元，供报价参考。\n")
    d_text = (
        "某采购项目撤回公告\n"
        "发布时间：2024-04-01T10:00:00\n"
        "标段编号：L-01\n"
        "现撤回补充公告中关于 120 万元 预算金额的表述，其余内容不变。\n")
    e_text = (
        "某采购项目预算替代公告\n"
        "发布时间：2024-05-01T10:00:00\n"
        "标段编号：L-01\n"
        "采购预算：110 万元\n"
        "本公告替代原招标公告中的预算金额表述。\n")
    f_text = (
        "某采购项目预算撤回公告\n"
        "发布时间：2024-06-01T10:00:00\n"
        "标段编号：L-01\n"
        "现撤回 110 万元 预算替代公告中的预算金额表述。\n")

    pages = {
        "docA": page_doc("docA", [(1, a_text)]),
        "docB": page_doc("docB", [(1, b_text)]),
        "docC": page_doc("docC", [(1, c_text)]),
        "docD": page_doc("docD", [(1, d_text)]),
        "docE": page_doc("docE", [(1, e_text)]),
        "docF": page_doc("docF", [(1, f_text)]),
    }
    sources = {}
    for did, payload in pages.items():
        sources[did] = dump(col / "pages" / f"{did}.json", payload)

    manifest_docs = [
        dict(document_id="docA", notice_id="N-A", site="site1",
             project_id="P-77", section_ids=["L-01"],
             published_at="2024-01-10T09:00:00", source=sources["docA"]),
        dict(document_id="docB", notice_id="N-B", site="site1",
             project_id="P-77", section_ids=["L-01"],
             published_at="2024-02-01T10:00:00", source=sources["docB"]),
        dict(document_id="docC", notice_id="N-C", site="site1",
             project_id="P-77", section_ids=["L-01"],
             published_at="2024-03-01T10:00:00", source=sources["docC"]),
        dict(document_id="docD", notice_id="N-D", site="site1",
             project_id="P-77", section_ids=["L-01"],
             published_at="2024-04-01T10:00:00", source=sources["docD"]),
        dict(document_id="docE", notice_id="N-E", site="site1",
             project_id="P-77", section_ids=["L-01"],
             published_at="2024-05-01T10:00:00", source=sources["docE"]),
        dict(document_id="docF", notice_id="N-F", site="site1",
             project_id="P-77", section_ids=["L-01"],
             published_at="2024-06-01T10:00:00", source=sources["docF"]),
    ]
    dump(col / "manifest.json",
         {"collection": "walkthrough", "documents": manifest_docs})

    def q(text: str, needle: str) -> dict:
        s, e = span_of(text, needle)
        return {"page_no": 1, "start": s, "end": e, "text": needle}

    relations = {"relations": [
        {"relation_id": "rel-B-deadline", "acting_document_id": "docB",
         "verb": "replace", "target_document_id": "docA",
         "target_sections": ["L-01"], "fields": ["deadline"],
         "quote": [q(b_text, "材料截止日期修改为 2024年3月20日")]},
        {"relation_id": "rel-C-budget", "acting_document_id": "docC",
         "verb": "supplement", "target_document_id": "docA",
         "target_sections": ["L-01"], "fields": ["budget"],
         "quote": [q(c_text, "120 万元")]},
        {"relation_id": "rel-D-retract-c", "acting_document_id": "docD",
         "verb": "retract", "target_document_id": "docC",
         "target_sections": ["L-01"], "fields": ["budget"],
         "quote": [q(d_text, "120 万元")]},
        {"relation_id": "rel-E-replace-a", "acting_document_id": "docE",
         "verb": "replace", "target_document_id": "docA",
         "target_sections": ["L-01"], "fields": ["budget"],
         "quote": [q(e_text, "采购预算：110 万元")]},
        {"relation_id": "rel-F-retract-e", "acting_document_id": "docF",
         "verb": "retract", "target_document_id": "docE",
         "target_sections": ["L-01"], "fields": ["budget"],
         "quote": [q(f_text, "110 万元")]},
    ]}
    dump(col / "relations.json", relations)
    return col


# --------------------------------------------------------------- table scope
def build_table_scope() -> Path:
    col = FIX / "table_scope"
    col.mkdir(parents=True, exist_ok=True)

    t1 = (
        "项目编号 P-88 开标记录（第1页）\n"
        "标段编号 预算金额（万元） 投标截止时间 中标单位 中标金额（元）\n"
        "L-01 100.00 2024-02-15 甲公司 1000000\n"
        "L-02 200.50 2024-02-16 乙公司 2005000\n"
        "（下接第二页）\n")
    t2 = (
        "开标记录续表\n"
        "标段编号 预算金额（万元） 投标截止时间 中标单位 中标金额（元）\n"
        "L-03 300.00 2024-02-17 丙公司 3000000\n")
    doc = page_doc("docT", [(1, t1), (2, t2)])
    src = dump(col / "pages" / "docT.json", doc)

    headers = ["标段编号", "预算金额（万元）", "投标截止时间", "中标单位",
               "中标金额（元）"]
    cells1 = []
    for c, h in enumerate(headers):
        cells1.append(cell("T1", 1, 0, c, h, t1))
    rows1 = [["L-01", "100.00", "2024-02-15", "甲公司", "1000000"],
             ["L-02", "200.50", "2024-02-16", "乙公司", "2005000"]]
    for r, vals in enumerate(rows1, start=1):
        for c, v in enumerate(vals):
            cells1.append(cell("T1", 1, r, c, v, t1))
    marker_s, marker_e = span_of(t1, "（下接第二页）")
    table1 = {"table_id": "T1", "document_id": "docT", "page_no": 1,
              "continuation": None,
              "continuation_marker": {"page_no": 1, "start": marker_s,
                                      "end": marker_e},
              "cells": cells1}
    t1ref = dump(col / "tables" / "T1.json", table1)

    cells2 = []
    for c, h in enumerate(headers):
        cells2.append(cell("T2", 2, 0, c, h, t2))
    rows2 = [["L-03", "300.00", "2024-02-17", "丙公司", "3000000"]]
    for r, vals in enumerate(rows2, start=1):
        for c, v in enumerate(vals):
            cells2.append(cell("T2", 2, r, c, v, t2))
    ms2, me2 = span_of(t2, "开标记录续表")
    table2 = {"table_id": "T2", "document_id": "docT", "page_no": 2,
              "continuation": "continues_from:T1",
              "continuation_marker": {"page_no": 2, "start": ms2,
                                      "end": me2},
              "cells": cells2}
    t2ref = dump(col / "tables" / "T2.json", table2)

    dump(col / "manifest.json", {"collection": "table_scope", "documents": [
        dict(document_id="docT", notice_id="N-T", site="site1",
             project_id="P-88", section_ids=["L-01", "L-02", "L-03"],
             published_at="2024-01-20T09:00:00", source=src,
             tables=[t1ref, t2ref])]})
    return col


# ------------------------------------------------------- continuation no marker
def build_no_continuation() -> Path:
    col = FIX / "no_continuation"
    col.mkdir(parents=True, exist_ok=True)
    p1 = ("项目编号 P-89 开标记录\n"
          "标段编号 预算金额（万元）\n"
          "L-01 100.00\n")
    p2 = ("开标记录（无续表标识）\n"
          "标段编号 预算金额（万元）\n"
          "L-02 999.00\n")
    src = dump(col / "pages" / "docNC.json",
               page_doc("docNC", [(1, p1), (2, p2)]))
    headers = ["标段编号", "预算金额（万元）"]
    cells1 = ([cell("NC1", 1, 0, 0, headers[0], p1)]
              + [cell("NC1", 1, 0, 1, headers[1], p1)]
              + [cell("NC1", 1, 1, 0, "L-01", p1)]
              + [cell("NC1", 1, 1, 1, "100.00", p1)])
    t1 = dump(col / "tables" / "NC1.json",
              {"table_id": "NC1", "document_id": "docNC", "page_no": 1,
               "continuation": None, "cells": cells1})
    cells2 = ([cell("NC2", 2, 0, 0, headers[0], p2)]
              + [cell("NC2", 2, 0, 1, headers[1], p2)]
              + [cell("NC2", 2, 1, 0, "L-02", p2)]
              + [cell("NC2", 2, 1, 1, "999.00", p2)])
    # Claims continuation but provides NO verifiable marker: must be ignored.
    t2 = dump(col / "tables" / "NC2.json",
              {"table_id": "NC2", "document_id": "docNC", "page_no": 2,
               "continuation": "continues_from:NC1",
               "continuation_marker": None, "cells": cells2})
    dump(col / "manifest.json", {"collection": "no_continuation", "documents": [
        dict(document_id="docNC", notice_id="N-NC", site="site1",
             project_id="P-89", section_ids=["L-01", "L-02"],
             published_at="2024-01-21T09:00:00", source=src,
             tables=[t1, t2])]})
    return col


# ------------------------------------------------------- grammar edge fixtures
def build_grammar() -> Path:
    col = FIX / "grammar"
    col.mkdir(parents=True, exist_ok=True)
    text = (
        "项目编号 P-90 采购公告\n"
        "发布时间：2024-07-01T09:00:00\n"
        "标段编号：G-01\n"
        "采购预算：0.0001 万元\n"                          # 1 yuan, 万元 precision
        "其他预算参考：12345\n"                            # bare number -> unknown currency
        "大写预算：壹佰万元整\n"                            # unparsed capital
        "约 50 万元\n"                                    # hedged -> unparsed
        "材料截止日期：8月9日\n"                           # year backfilled 2024
        "开标安排：下月底前完成\n"                         # relative -> unparsed
        "中标单位：星河建筑有限公司\n"
        "中标金额：人民币 880,000 元\n"
        "金额区间：10 万元 至 20 万元\n")                  # range -> unparsed
    src = dump(col / "pages" / "docG.json",
               page_doc("docG", [(1, text)]))
    dump(col / "manifest.json", {"collection": "grammar", "documents": [
        dict(document_id="docG", notice_id="N-G", site="site1",
             project_id="P-90", section_ids=["G-01"],
             published_at="2024-07-01T09:00:00", source=src)]})
    return col


# ------------------------------------------------------- repost same name
def build_repost() -> Path:
    col = FIX / "repost"
    col.mkdir(parents=True, exist_ok=True)
    t1 = ("项目编号 P-91 招标公告\n标段编号：R-01\n采购预算：100 万元\n")
    t2 = ("【转载】项目编号 P-91 招标公告\n标段编号：R-01\n采购预算：100 万元\n")
    s1 = dump(col / "pages" / "docR1.json",
              page_doc("docR1", [(1, t1)]))
    s2 = dump(col / "pages" / "docR2.json",
              page_doc("docR2", [(1, t2)]))
    dump(col / "manifest.json", {"collection": "repost", "documents": [
        dict(document_id="docR1", notice_id="N-R", site="site1",
             project_id="P-91", section_ids=["R-01"],
             published_at="2024-01-05T09:00:00", source=s1,
             same_notice=[{"notice_id": "N-R", "document_id": "docR1",
                           "site": "site1"},
                          {"notice_id": "N-R", "document_id": "docR2",
                           "site": "site2"}]),
        # Different notice_id, no same_notice mapping: same title/section
        # must NOT be merged.
        dict(document_id="docR2", notice_id="N-R-COPY", site="site2",
             project_id="P-91", section_ids=["R-01"],
             published_at="2024-01-06T09:00:00", source=s2)]})
    return col


# ------------------------------------------------------- pending + loop
def build_relations_edges() -> Path:
    col = FIX / "relations_edges"
    col.mkdir(parents=True, exist_ok=True)
    # X1 and X2 share a publish instant, so the replacement cycle between
    # them is structurally invalid independent of any traversal ordering
    # (and is not masked by the future-target rule).
    a = ("项目 P-92 公告\n发布时间：2024-02-01T09:00:00\n标段编号：X-01\n"
         "采购预算：100 万元\n")
    b = ("项目 P-92 更正\n发布时间：2024-02-01T09:00:00\n标段编号：X-01\n"
         "采购预算：110 万元\n")
    c = ("项目 P-92 再更正\n发布时间：2024-03-01T09:00:00\n标段编号：X-01\n"
         "采购预算：120 万元\n")
    sa = dump(col / "pages" / "docX1.json", page_doc("docX1", [(1, a)]))
    sb = dump(col / "pages" / "docX2.json", page_doc("docX2", [(1, b)]))
    sc = dump(col / "pages" / "docX3.json", page_doc("docX3", [(1, c)]))
    dump(col / "manifest.json", {"collection": "relations_edges", "documents": [
        dict(document_id="docX1", notice_id="N-X1", site="site1",
             project_id="P-92", section_ids=["X-01"],
             published_at="2024-02-01T09:00:00", source=sa),
        dict(document_id="docX2", notice_id="N-X2", site="site1",
             project_id="P-92", section_ids=["X-01"],
             published_at="2024-02-01T09:00:00", source=sb),
        dict(document_id="docX3", notice_id="N-X3", site="site1",
             project_id="P-92", section_ids=["X-01"],
             published_at="2024-03-01T09:00:00", source=sc)]})

    def q(text, needle):
        s, e = span_of(text, needle)
        return {"page_no": 1, "start": s, "end": e, "text": needle}

    dump(col / "relations.json", {"relations": [
        # missing target document -> pending, closes nothing
        {"relation_id": "rel-missing-target", "acting_document_id": "docX2",
         "verb": "replace", "target_document_id": "docGHOST",
         "target_sections": ["X-01"], "fields": ["budget"],
         "quote": [q(b, "110 万元")]},
        # replacement cycle X2 -> X1, X1 -> X2: both invalid, no closures
        {"relation_id": "rel-x2-x1", "acting_document_id": "docX2",
         "verb": "replace", "target_document_id": "docX1",
         "target_sections": ["X-01"], "fields": ["budget"],
         "quote": [q(b, "110 万元")]},
        {"relation_id": "rel-x1-x2", "acting_document_id": "docX1",
         "verb": "replace", "target_document_id": "docX2",
         "target_sections": ["X-01"], "fields": ["budget"],
         "quote": [q(a, "100 万元")]},
        # future target: acting (Feb) names a document published in Mar
        {"relation_id": "rel-future", "acting_document_id": "docX1",
         "verb": "supplement", "target_document_id": "docX3",
         "target_sections": ["X-01"], "fields": ["budget"],
         "quote": [q(a, "100 万元")]}]})
    return col


# ------------------------------------------------------- unpaired amounts
def build_unpaired() -> Path:
    col = FIX / "unpaired"
    col.mkdir(parents=True, exist_ok=True)
    text = ("项目 P-93 采购公告\n发布时间：2024-05-01T09:00:00\n"
            "本公告含两个标段（编号 U-01、U-02，详见清单约束）。\n"
            "预算金额：100 万元\n"
            "另一部分预算金额：200 万元\n"
            "以上金额与标段的对应关系原文未给出。\n")
    src = dump(col / "pages" / "docU.json", page_doc("docU", [(1, text)]))
    dump(col / "manifest.json", {"collection": "unpaired", "documents": [
        dict(document_id="docU", notice_id="N-U", site="site1",
             project_id="P-93", section_ids=["U-01", "U-02"],
             published_at="2024-05-01T09:00:00", source=src)]})
    return col


# ------------------------------------------------------- bad slice fixture
def build_bad_slice() -> Path:
    col = FIX / "bad_slice"
    col.mkdir(parents=True, exist_ok=True)
    text = ("项目 P-94 公告\n发布时间：2024-04-01T09:00:00\n"
            "标段编号：B-01\n"
            "预算金额（万元） 投标单位\n"
            "采购预算：100 万元\n")
    src = dump(col / "pages" / "docB1.json", page_doc("docB1", [(1, text)]))
    s, e = span_of(text, "100 万元")
    bad_cell = {"table_id": "BAD1", "document_id": "docB1", "page_no": 1,
                "continuation": None,
                "cells": [
                    cell("BAD1", 1, 0, 0, "预算金额（万元）", text),
                    cell("BAD1", 1, 0, 1, "投标单位", text),
                    cell("BAD1", 1, 1, 0, "B-01", text),
                    # The extractor claims this cell text at offsets that do
                    # not slice it: must be rejected, never trusted as evidence.
                    {"page_no": 1, "row": 1, "col": 1,
                     "text": "错位金额 100 万元",
                     "start": s, "end": e + 3}]}
    tref = dump(col / "tables" / "BAD1.json", bad_cell)
    dump(col / "manifest.json", {"collection": "bad_slice", "documents": [
        dict(document_id="docB1", notice_id="N-B1", site="site1",
             project_id="P-94", section_ids=["B-01"],
             published_at="2024-04-01T09:00:00", source=src,
             tables=[tref])]})
    return col


# ------------------------------------------------------- merged headers
def build_merged_header() -> Path:
    col = FIX / "merged_header"
    col.mkdir(parents=True, exist_ok=True)
    text = (
        "项目 P-95 开标记录\n"
        "标段编号 预算金额 投标单位\n"
        "        （万元）\n"
        "M-01 100.00 甲公司\n"
        "M-02 200.00 乙公司\n")
    src = dump(col / "pages" / "docM.json",
               page_doc("docM", [(1, text)]))
    cells = [
        cell("MG1", 1, 0, 0, "标段编号", text, rowspan=2),
        # The budget header spans two rows; its unit line is the same
        # column, so the merged header inherits 万元 on col 1 only.
        cell("MG1", 1, 0, 1, "预算金额", text),
        cell("MG1", 1, 1, 1, "（万元）", text),
        cell("MG1", 1, 0, 2, "投标单位", text, rowspan=2),
        cell("MG1", 1, 2, 0, "M-01", text),
        cell("MG1", 1, 2, 1, "100.00", text),
        cell("MG1", 1, 2, 2, "甲公司", text),
        cell("MG1", 1, 3, 0, "M-02", text),
        cell("MG1", 1, 3, 1, "200.00", text),
        cell("MG1", 1, 3, 2, "乙公司", text)]
    tref = dump(col / "tables" / "MG1.json",
                {"table_id": "MG1", "document_id": "docM", "page_no": 1,
                 "continuation": None, "cells": cells})
    dump(col / "manifest.json", {"collection": "merged_header", "documents": [
        dict(document_id="docM", notice_id="N-MG", site="site1",
             project_id="P-95", section_ids=["M-01", "M-02"],
             published_at="2024-01-22T09:00:00", source=src,
             tables=[tref])]})
    return col


def main() -> None:
    builders = [build_walkthrough, build_table_scope, build_no_continuation,
                build_grammar, build_repost, build_relations_edges,
                build_unpaired, build_bad_slice, build_merged_header]
    for b in builders:
        print("built", b())


if __name__ == "__main__":
    main()
