"""Extraction engine.

The engine only looks at already-extracted page text plus table cells
with coordinates. Every piece of evidence is a *verified* slice
(:mod:`bidfact.ingestion` guarantees the offsets); the engine's own
choices are recorded in ``scope_basis`` and ``header`` so a reader can
re-audit them.

Scope rules:

* a section is only paired when its numbering appears in the same text
  block/row or a manifest single-section constraint applies;
* multi-section documents whose value cannot be paired emit a
  ``pending`` assertion carrying the candidate(s), never a guessed
  "all sections";
* table header units and merged-header scope are inherited only with
  explicit table-structure evidence; continuation across pages needs a
  continuation marker, otherwise the table ends at the page break.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .models import (Assertion, Page, Span, Table, Unparsed,
                     F_BUDGET, F_DEADLINE, F_RESULT_AMOUNT, F_RESULT_UNIT)
from .parse_dates import (UnsupportedDate, find_date, has_unsupported_date,
                          parse_date)
from .parse_money import (find_amount, find_unsupported_money, parse_amount)
from .util import stable_id

# --- field label vocabularies (original words are always kept) --------
BUDGET_LABELS = ("预算金额", "最高限价", "控制价", "采购预算", "预算价", "预算")
DEADLINE_LABELS = ("投标截止时间", "响应文件截止时间", "文件递交截止时间",
                   "报名截止时间", "材料截止日期", "投标截止", "截止时间", "截止日期")
RESULT_UNIT_LABELS = ("中标单位", "成交单位", "中标供应商", "成交供应商",
                      "中标人", "成交人", "投标单位")
RESULT_AMOUNT_LABELS = ("中标金额", "成交金额", "中标价", "成交价")

_SECTION_RE = re.compile(r"标段\s*(?:编号|ID|id)?\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9\-_]*)")
_SECTION_ALT_RE = re.compile(r"第\s*([0-9一二三四五六七八九十]+)\s*[标段包]")
_LABEL_RE_TMPL = r"{label}\s*[:：是为]?\s*"
_DATE_RANGE_RE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*(?:至|到|-|~)\s*\d{1,2}\s*月\s*\d{1,2}\s*日")


@dataclass
class ExtractOutcome:
    assertions: list[Assertion]
    unparsed: list[Unparsed]


def _publish_year(published_at: str) -> int:
    return int(published_at[:4])


def _find_segments(pages: list[Page], sections: list[str]) -> list[dict]:
    """Split all pages into section-headed segments with absolute offsets.

    A segment is [marker_start, next_marker_start). Documents without
    markers yield one whole-text segment with section=None.
    """
    markers: list[tuple[int, int, str, str]] = []  # page, start, end, sec
    candidates = set(sections)
    for page in pages:
        for m in _SECTION_RE.finditer(page.text):
            candidates.add(m.group(1))
        for m in _SECTION_ALT_RE.finditer(page.text):
            candidates.add(m.group(1))
    for sec in sorted(candidates, key=len, reverse=True):
        # A marker requires an explicit formal form: 标段编号：X,
        # 标段:X or 第N标/段/包. A bare "标段 U-01、U-02" mention inside
        # prose is not a marker and must not split scope.
        pat = re.compile(
            r"标段\s*(?:编号|ID|id)?\s*[:：]\s*" + re.escape(sec)
            + r"(?!\w)")
        for page in pages:
            for m in pat.finditer(page.text):
                markers.append((page.page_no, m.start(), m.end(), sec))
    if not markers:
        full = []
        for page in pages:
            full.append({"page_no": page.page_no, "start": 0,
                         "end": len(page.text), "section": None})
        return full
    markers.sort(key=lambda x: (x[0], x[1]))
    segments: list[dict] = []
    # head of first page(s) before the first marker -> its own segment
    first_pg, first_start, _, first_sec = markers[0]
    if first_start > 0:
        segments.append({"page_no": first_pg, "start": 0,
                         "end": first_start, "section": None})
    for i, (pg, start, end, sec) in enumerate(markers):
        nxt = markers[i + 1] if i + 1 < len(markers) else None
        if nxt and nxt[0] == pg:
            seg_end = nxt[1]
            seg_page = pg
        elif nxt:
            segments.append({"page_no": pg, "start": start,
                             "end": len(_page_text(pages, pg)),
                             "section": sec})
            for mid_pg in range(pg + 1, nxt[0]):
                segments.append({"page_no": mid_pg, "start": 0,
                                 "end": len(_page_text(pages, mid_pg)),
                                 "section": sec})
            seg_page, seg_end = nxt[0], nxt[1]
        else:
            segments.append({"page_no": pg, "start": start,
                             "end": len(_page_text(pages, pg)),
                             "section": sec})
            for mid_pg in range(pg + 1, pages[-1].page_no + 1):
                segments.append({"page_no": mid_pg, "start": 0,
                                 "end": len(_page_text(pages, mid_pg)),
                                 "section": sec})
            continue
        segments.append({"page_no": seg_page, "start": start,
                         "end": seg_end, "section": sec})
    return segments


def _page_text(pages: list[Page], page_no: int) -> str:
    for p in pages:
        if p.page_no == page_no:
            return p.text
    raise KeyError(page_no)


def _resolve_scope(segment_section: Optional[str], manifest_sections: list[str],
                   body_section_mentions: list[str]) -> tuple[Optional[str], str, bool]:
    """Return (section_id|None, scope_basis, is_pending)."""
    if len(manifest_sections) == 1:
        sec = manifest_sections[0]
        if segment_section and segment_section != sec:
            return None, (f"marker {segment_section} not in manifest sections "
                          f"{manifest_sections}"), True
        return sec, ("single manifest section "
                     f"{sec}; block marker={segment_section or 'none'}"), False
    if segment_section and segment_section in manifest_sections:
        return segment_section, f"section numbering {segment_section} in same block", False
    if segment_section:
        return None, f"marker {segment_section} not in manifest {manifest_sections}", True
    if len(body_section_mentions) == 1 and body_section_mentions[0] in manifest_sections:
        sec = body_section_mentions[0]
        return sec, f"only section {sec} mentioned in block", False
    return None, ("scope not unique; candidates="
                  f"{sorted(set(body_section_mentions)) or 'none'}"), True


def _make_span(pages: list[Page], page_no: int, start: int, end: int,
               header: Optional[str] = None) -> Optional[Span]:
    text = _page_text(pages, page_no)[start:end]
    if len(text) != end - start or not text:
        return None
    return Span(page_no=page_no, start=start, end=end, text=text, header=header)


def extract_document(doc: dict, pages: list[Page], tables: list[Table],
                     rule_version: str) -> ExtractOutcome:
    assertions: list[Assertion] = []
    unparsed: list[Unparsed] = []
    published_at = doc["published_at"]
    year = _publish_year(published_at)
    manifest_sections = list(doc.get("section_ids", []))
    page_index = {p.page_no: p.text for p in pages}

    segments = _find_segments(pages, manifest_sections)
    multi_section = len(manifest_sections) > 1
    for seg in segments:
        block = page_index[seg["page_no"]][seg["start"]:seg["end"]]
        mentions = sorted({s for s in manifest_sections if s in block})
        section, scope_basis, pending = _resolve_scope(
            seg["section"], manifest_sections, mentions)
        # In multi-section documents, prose outside any explicit section
        # marker cannot be paired: keep unpaired money/date candidates as
        # pending assertions rather than binding them to a guessed section.
        if multi_section and seg["section"] is None:
            _extract_unpaired_prose(doc, block, seg, pages, year,
                                    published_at, rule_version,
                                    manifest_sections, assertions, unparsed)
            continue
        _extract_prose_block(doc, block, seg, section, scope_basis, pending,
                             pages, year, published_at, rule_version,
                             assertions, unparsed)

    _extract_tables(doc, pages, tables, manifest_sections, year,
                    published_at, rule_version, assertions, unparsed)
    return ExtractOutcome(assertions=assertions, unparsed=unparsed)


def _emit_assertion(doc, field, section, scope_basis, pending, raw, normalized,
                    currency, span, header, published_at, rule_version,
                    assertions, pending_reason=None):
    aid = stable_id("ast", doc["document_id"], field, section or "?",
                    span.page_no, span.start, span.end, normalized or raw)
    assertions.append(Assertion(
        assertion_id=aid, document_id=doc["document_id"],
        project_id=doc.get("project_id"), section_id=section, field=field,
        normalized=normalized, raw_value=raw, currency=currency, span=span,
        header=header, scope_basis=scope_basis,
        status="pending" if pending else "confirmed",
        pending_reason=pending_reason if pending else None,
        published_at=published_at, rule_version=rule_version))


def _emit_unparsed(doc, field, reason, raw, span, published_at,
                   rule_version, unparsed):
    uid = stable_id("unp", doc["document_id"], field or "?",
                    span.page_no, span.start, span.end, reason)
    unparsed.append(Unparsed(
        unparsed_id=uid, document_id=doc["document_id"], field=field,
        reason=reason, raw_text=raw, span=span, published_at=published_at,
        rule_version=rule_version))


def _all_label_matches(text: str, labels: tuple[str, ...]) -> list[re.Match]:
    out: list[re.Match] = []
    for label in labels:
        out.extend(re.finditer(
            _LABEL_RE_TMPL.format(label=re.escape(label)), text))
    out.sort(key=lambda m: m.start())
    return out


def _extract_unpaired_prose(doc, block, seg, pages, year, published_at,
                            rule_version, manifest_sections,
                            assertions, unparsed):
    """Labelled values in multi-section prose with no section marker.

    The candidates are preserved verbatim with their spans, but every
    assertion stays ``pending`` with reason ``scope_not_unique`` — the
    system never promotes one to "all sections" or picks a section.
    """
    pg = seg["page_no"]
    base = seg["start"]

    def abs_span(rel_start, rel_end):
        return _make_span(pages, pg, base + rel_start, base + rel_end)

    for labels, field in ((BUDGET_LABELS, F_BUDGET),
                          (RESULT_AMOUNT_LABELS, F_RESULT_AMOUNT)):
        for m in _all_label_matches(block, labels):
            nl = block.find("\n", m.end())
            window = block[m.end(): nl if nl >= 0 else len(block)]
            found = find_amount(window)
            if found is None:
                for s0, e0, reason in find_unsupported_money(window):
                    sp = abs_span(m.end() + s0, m.end() + e0)
                    if sp:
                        _emit_unparsed(doc, field, reason, sp.text, sp,
                                       published_at, rule_version, unparsed)
                continue
            rs, re_, money = found
            span = abs_span(m.end() + rs, m.end() + re_)
            if span is None:
                continue
            _emit_assertion(
                doc, field, None,
                (f"unpaired value in multi-section document; candidates "
                 f"sections={manifest_sections}; no same-block section numbering"),
                True, span.text, money.amount, money.currency, span, None,
                published_at, rule_version, assertions,
                pending_reason="scope_not_unique")

    for m in _all_label_matches(block, DEADLINE_LABELS):
        nl = block.find("\n", m.end())
        window = block[m.end(): nl if nl >= 0 else len(block)]
        found = find_date(window, year)
        if found:
            rs, re_, normalized = found
            span = abs_span(m.end() + rs, m.end() + re_)
            if span is not None:
                _emit_assertion(
                    doc, F_DEADLINE, None,
                    "unpaired date in multi-section document; "
                    f"candidate sections={manifest_sections}",
                    True, span.text, normalized, None, span, None,
                    published_at, rule_version, assertions,
                    pending_reason="scope_not_unique")


def _extract_prose_block(doc, block, seg, section, scope_basis, pending,
                         pages, year, published_at, rule_version,
                         assertions, unparsed):
    pg = seg["page_no"]
    base = seg["start"]

    def abs_span(rel_start: int, rel_end: int, header=None):
        return _make_span(pages, pg, base + rel_start, base + rel_end, header)

    def line_window(label_match) -> str:
        # Stay inside the current line: a label only owns the values
        # until the next newline.
        nl = block.find("\n", label_match.end())
        return block[label_match.end(): nl if nl >= 0 else len(block)]

    # ---- budget / result amount (money) -------------------------------
    for labels, field in ((BUDGET_LABELS, F_BUDGET),
                          (RESULT_AMOUNT_LABELS, F_RESULT_AMOUNT)):
        for m in _all_label_matches(block, labels):
            window = line_window(m)
            found = find_amount(window)
            if found is None:
                # Recognisably money but outside the grammar -> keep raw.
                bad = find_unsupported_money(window)
                if bad:
                    s0, e0, reason = bad[0]
                    span = abs_span(m.end() + s0, m.end() + e0)
                    if span:
                        _emit_unparsed(doc, field, reason, span.text, span,
                                       published_at, rule_version, unparsed)
                continue
            rs, re_, money = found
            span = abs_span(m.end() + rs, m.end() + re_)
            if span is None:
                continue
            _emit_assertion(doc, field, section, scope_basis, pending,
                            span.text, money.amount, money.currency, span, None,
                            published_at, rule_version, assertions,
                            pending_reason="scope_not_unique" if pending else None)
            # Additional unsupported money on the same line is still kept.
            for s0, e0, reason in find_unsupported_money(window):
                if e0 <= rs or s0 >= re_:
                    sp = abs_span(m.end() + s0, m.end() + e0)
                    if sp:
                        _emit_unparsed(doc, field, reason, sp.text, sp,
                                       published_at, rule_version, unparsed)

    # ---- deadline (date) ----------------------------------------------
    for m in _all_label_matches(block, DEADLINE_LABELS):
        window = line_window(m)
        found = find_date(window, year)
        if found is None:
            # Unsupported date wording after a deadline label.
            reason = "unsupported date expression"
            if has_unsupported_date(window):
                reason = "relative/vague date unsupported"
            token = window.strip()
            if token:
                span = abs_span(m.end(), m.end() + len(window))
                if span:
                    _emit_unparsed(doc, F_DEADLINE,
                                   f"{reason}: {token}", token, span,
                                   published_at, rule_version, unparsed)
        else:
            rs, re_, normalized = found
            span = abs_span(m.end() + rs, m.end() + re_)
            if span is not None:
                _emit_assertion(doc, F_DEADLINE, section, scope_basis,
                                pending, span.text, normalized, None, span,
                                None, published_at, rule_version,
                                assertions,
                                pending_reason="scope_not_unique"
                                if pending else None)

    # ---- result unit (raw string, no entity merging) ------------------
    for m in _all_label_matches(block, RESULT_UNIT_LABELS):
        window = line_window(m).strip()
        if window:
            span = abs_span(m.end(), m.end() + len(line_window(m)))
            if span is not None:
                _emit_assertion(doc, F_RESULT_UNIT, section, scope_basis,
                                pending, window, window, None, span, None,
                                published_at, rule_version, assertions,
                                pending_reason="scope_not_unique"
                                if pending else None)

    # ---- unsupported-but-recognised expressions are kept, not lost ----
    # Only scanned outside labelled lines, which already handled their
    # own unsupported values above.
    labelled_spans: list[tuple[int, int]] = []
    for labels in (BUDGET_LABELS, RESULT_AMOUNT_LABELS, DEADLINE_LABELS,
                   RESULT_UNIT_LABELS):
        for lm in _all_label_matches(block, labels):
            nl = block.find("\n", lm.end())
            labelled_spans.append((lm.start(), nl if nl >= 0 else len(block)))

    def outside_labels(s: int, e: int) -> bool:
        return all(e <= ls or s >= le for ls, le in labelled_spans)

    for s0, e0, reason in find_unsupported_money(block):
        if outside_labels(s0, e0):
            span = abs_span(s0, e0)
            if span:
                _emit_unparsed(doc, F_BUDGET, reason, span.text, span,
                               published_at, rule_version, unparsed)
    for pat_reason in ((re.compile(r"下(?:个)?月[^。；;\n]*"),
                        "relative date unsupported"),
                       (re.compile(r"\d+\s*月\s*(?:初|中|底)"),
                        "vague date unsupported")):
        pat, reason = pat_reason
        for dm in pat.finditer(block):
            if outside_labels(dm.start(), dm.end()):
                span = abs_span(dm.start(), dm.end())
                if span:
                    _emit_unparsed(doc, F_DEADLINE, reason, dm.group(0),
                                   span, published_at, rule_version, unparsed)
    for dm in _DATE_RANGE_RE.finditer(block):
        if outside_labels(dm.start(), dm.end()):
            span = abs_span(dm.start(), dm.end())
            if span:
                _emit_unparsed(doc, F_DEADLINE, "date range unsupported",
                               dm.group(0), span, published_at,
                               rule_version, unparsed)


# ---------------------------------------------------------------- tables
def _table_grid(table: Table) -> dict[tuple[int, int], object]:
    """Expand rowspan/colspan into a dense (row,col)->cell grid.

    Merged regions repeat the origin cell, and ``merged_from`` records
    the origin coordinate: that is the explicit structural evidence
    required before a header/unit may be inherited. Origin coordinates
    are never overwritten by another cell's expansion, so overlapping
    claims cannot silently re-map columns.
    """
    grid: dict[tuple[int, int], object] = {}
    origins: dict[tuple[int, int], tuple[int, int]] = {}
    for cell in sorted(table.cells, key=lambda c: (c.row, c.col)):
        for r in range(cell.row, cell.row + cell.rowspan):
            for c in range(cell.col, cell.col + cell.colspan):
                is_origin = (r, c) == (cell.row, cell.col)
                if is_origin:
                    grid[(r, c)] = {"cell": cell, "merged_from": None}
                    origins[(r, c)] = (cell.row, cell.col)
                elif (r, c) not in grid:
                    grid[(r, c)] = {
                        "cell": cell,
                        "merged_from": (cell.row, cell.col)}
    return grid


def _classify_header(text: str) -> Optional[str]:
    if any(k in text for k in BUDGET_LABELS) or "限价" in text:
        return F_BUDGET
    if any(k in text for k in DEADLINE_LABELS):
        return F_DEADLINE
    if any(k in text for k in RESULT_AMOUNT_LABELS):
        return F_RESULT_AMOUNT
    if any(k in text for k in RESULT_UNIT_LABELS):
        return F_RESULT_UNIT
    if re.search(r"标段|包号|分包", text):
        return "section"
    return None


def _build_chains(tables: list[Table]) -> list[list[Table]]:
    """Group tables into continuation chains on explicit evidence only.

    A continuation requires both the ``continues_from:`` link *and* a
    verified continuation-marker span (ingestion nulls the link when
    the marker does not slice the page). Anything else stays its own
    table, so a header on page 2 can never silently inherit scope.
    """
    by_id = {t.table_id: t for t in tables}
    seen: set[str] = set()
    chains: list[list[Table]] = []
    for t in tables:
        if t.table_id in seen:
            continue
        chain = [t]
        seen.add(t.table_id)
        current = t
        while True:
            nxt = next(
                (x for x in tables
                 if x.continuation == f"continues_from:{current.table_id}"
                 and x.continuation_marker_span is not None
                 and x.table_id not in seen), None)
            if nxt is None:
                break
            chain.append(nxt)
            seen.add(nxt.table_id)
            current = nxt
        chains.append(chain)
    return chains


def _header_for_table(table: Table) -> tuple[dict[int, tuple[str, str]],
                                             Optional[int], set[int]]:
    """Classify this table's own header rows.

    Multi-row headers are joined per column: a merged "预算金额" cell
    above a "（万元）" cell only carries its unit once the pieces are
    combined, and the structural rowspan/colspan evidence is what lets
    them be treated as one header. Returns column->(field, header text),
    the section column and the header row index set.
    """
    grid = _table_grid(table)
    if not grid:
        return {}, None, set()

    def _is_header_like(text: str) -> bool:
        if _classify_header(text) is not None:
            return True
        # Unit-only continuation lines of a multi-row header. A plain
        # numeric body cell ("1000000") must not be mistaken for one.
        if re.fullmatch(r"\d[\d.,]*", text.strip()):
            return False
        return bool(re.fullmatch(
            r"[（(]?\s*(?:万元|万人民币|元)\s*[）)]?", text.strip()))

    header_like_rows = {r for (r, _c), e in grid.items()
                        if _is_header_like(e["cell"].text)}
    if not header_like_rows:
        return {}, None, set()
    # The header block is the leading contiguous run ending at the last
    # header-like row; body rows begin immediately after that.
    header_boundary = max(header_like_rows)
    header_rows = set(range(0, header_boundary + 1))
    # Include rows a labelled cell spans into via rowspan/colspan.
    for (r, _c), entry in grid.items():
        if (r > header_boundary and entry["merged_from"] is not None
                and entry["merged_from"][0] <= header_boundary):
            header_rows.add(r)
            header_boundary = max(header_boundary, r)
    max_col = max(c for _r, c in grid)

    # Collect every header cell covering each column (origin or merged).
    # Concrete single-column field headers win over a spanning merged
    # header: "预算金额" colspan=2 must not swallow a sibling
    # "投标单位" column.
    pieces: dict[int, list[str]] = {c: [] for c in range(max_col + 1)}
    concrete_kind: dict[int, str] = {}
    section_col: Optional[int] = None
    for (r, c), entry in sorted(grid.items()):
        if r not in header_rows:
            continue
        kind = _classify_header(entry["cell"].text)
        if kind == "section":
            section_col = c
        cell_obj = entry["cell"]
        is_concrete = cell_obj.colspan == 1 and cell_obj.rowspan == 1
        for cc in range(c, min(c + cell_obj.colspan, max_col + 1)):
            if is_concrete and kind in (F_BUDGET, F_DEADLINE,
                                        F_RESULT_AMOUNT, F_RESULT_UNIT):
                concrete_kind[cc] = kind
            if cell_obj.text not in pieces[cc]:
                pieces[cc].append(cell_obj.text)

    header_map: dict[int, tuple[str, str]] = {}
    for c, texts in pieces.items():
        joined = "".join(texts)
        if c in concrete_kind:
            header_map[c] = (concrete_kind[c], joined)
            continue
        kind = _classify_header(joined)
        if kind and kind != "section":
            header_map[c] = (kind, joined)
    return header_map, section_col, header_rows


def _extract_tables(doc, pages, tables, manifest_sections, year,
                    published_at, rule_version, assertions, unparsed):
    for chain in _build_chains(tables):
        inherited_note = ("; continuation evidence: marker span "
                          + ",".join(str(t.continuation_marker_span)
                                     for t in chain[1:])
                          if len(chain) > 1 else "")
        for t in chain:
            grid = _table_grid(t)
            header_map, section_col, header_rows = _header_for_table(t)
            if not header_map:
                # No structural header evidence: cells cannot be bound
                # to fields, so nothing here becomes an assertion.
                continue
            body_start = (max(header_rows) + 1) if header_rows else 0
            rows = sorted({r for (r, c) in grid})
            for r in rows:
                if r < body_start:
                    continue
                row_cells = {c: grid[(r, c)] for (rr, c) in grid if rr == r}
                # scope for this row
                row_section = None
                if section_col is not None and section_col in row_cells:
                    token = row_cells[section_col]["cell"].text.strip()
                    m = (_SECTION_RE.search(token)
                         or _SECTION_ALT_RE.search(token))
                    candidate = m.group(1) if m else token
                    if candidate in manifest_sections:
                        row_section = candidate
                if len(manifest_sections) == 1 and row_section is None:
                    row_section = manifest_sections[0]
                    scope_basis = (f"table row; single manifest section "
                                   f"{row_section}")
                    pending = False
                elif row_section:
                    scope_basis = (f"table section column value {row_section} "
                                   "on same row")
                    pending = False
                else:
                    scope_basis = ("table row without resolvable section; "
                                   f"manifest={manifest_sections}")
                    pending = True

                for c, entry in row_cells.items():
                    if c == section_col or c not in header_map:
                        continue
                    field, header_text = header_map[c]
                    cell = entry["cell"]
                    merged_note = ("; merged header cell inherited from "
                                   f"r{entry['merged_from'][0]}"
                                   f"c{entry['merged_from'][1]}"
                                   if entry["merged_from"] else "")
                    span = Span(page_no=cell.page_no, start=cell.start,
                                end=cell.end, text=cell.text,
                                source_kind="cell", table_id=t.table_id,
                                row=cell.row, col=cell.col, header=header_text)
                    _table_cell_value(
                        doc, field, cell.text, header_text, span,
                        row_section, scope_basis + merged_note + inherited_note,
                        pending, year, published_at, rule_version,
                        assertions, unparsed)


def _table_cell_value(doc, field, raw, header_text, span, section,
                      scope_basis, pending, year, published_at, rule_version,
                      assertions, unparsed):
    token = raw.strip()
    if not token:
        return
    if field in (F_BUDGET, F_RESULT_AMOUNT):
        money = parse_amount(token, header_text)
        if money is None:
            _emit_unparsed(doc, field,
                           f"unsupported money cell under header {header_text}",
                           token, span, published_at, rule_version, unparsed)
            return
        _emit_assertion(doc, field, section, scope_basis, pending,
                        token, money.amount, money.currency, span,
                        header_text, published_at, rule_version, assertions,
                        pending_reason="scope_not_unique" if pending else None)
    elif field == F_DEADLINE:
        try:
            normalized = parse_date(token, year)
        except UnsupportedDate:
            normalized = None
        if normalized is None:
            _emit_unparsed(doc, F_DEADLINE,
                           f"unsupported date cell under header {header_text}",
                           token, span, published_at, rule_version, unparsed)
            return
        _emit_assertion(doc, F_DEADLINE, section, scope_basis, pending,
                        token, normalized, None, span, header_text,
                        published_at, rule_version, assertions,
                        pending_reason="scope_not_unique" if pending else None)
    else:  # result unit: keep the exact original string, no entity merging
        _emit_assertion(doc, F_RESULT_UNIT, section, scope_basis, pending,
                        token, token, None, span, header_text, published_at,
                        rule_version, assertions,
                        pending_reason="scope_not_unique" if pending else None)
