"""In-memory dataclasses used while streaming one document at a time.

Only a single document's page text and cells are ever held in memory;
everything durable is persisted to the result's SQLite store by
:mod:`bidfact.storage`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# The four fields the processor is able to assert anything about.
F_BUDGET = "budget"
F_DEADLINE = "deadline"
F_RESULT_UNIT = "result_unit"
F_RESULT_AMOUNT = "result_amount"
FIELDS = (F_BUDGET, F_DEADLINE, F_RESULT_UNIT, F_RESULT_AMOUNT)

# Relation verbs.
V_REPLACE = "replace"
V_SUPPLEMENT = "supplement"
V_RETRACT = "retract"
VERBS = (V_REPLACE, V_SUPPLEMENT, V_RETRACT)

# Canonical statuses returned by the point-in-time reader.
S_VALUE = "value"
S_CONFLICT = "conflict"
S_WITHDRAWN = "withdrawn"
S_ABSENT = "absent"
S_PENDING = "pending"


@dataclass(frozen=True)
class Page:
    """One extracted page: 1-based page_no and its full extracted text."""

    page_no: int
    text: str


@dataclass(frozen=True)
class Cell:
    """One table cell with coordinates and a span into its page text.

    start/end are character offsets inside *that page's* text; the
    ingestion layer verifies text[start:end] == text_value, so an
    extractor misalignment is rejected rather than treated as evidence.
    """

    page_no: int
    row: int
    col: int
    text: str
    start: int
    end: int
    rowspan: int = 1
    colspan: int = 1


@dataclass(frozen=True)
class Table:
    """A table on one page; continuation pages link by table_id."""

    table_id: str
    page_no: int
    continuation: Optional[str]  # "continues_from:<table_id>" or None
    continuation_marker_span: Optional[tuple[int, int]] = None
    cells: tuple[Cell, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Span:
    """A verified piece of source text."""

    page_no: int
    start: int
    end: int
    text: str
    source_kind: str = "page"  # page | cell
    table_id: Optional[str] = None
    row: Optional[int] = None
    col: Optional[int] = None
    header: Optional[str] = None


@dataclass(frozen=True)
class Assertion:
    """One confirmed (or pending) field assertion made by one document.

    scope_basis records the numbering/manifest reasoning that binds the
    assertion to a section; provenance never leaves the document that
    actually said the words.
    """

    assertion_id: str
    document_id: str
    project_id: Optional[str]
    section_id: Optional[str]
    field: str
    normalized: Optional[str]
    raw_value: str
    currency: Optional[str]
    span: Span
    header: Optional[str]
    scope_basis: str
    status: str  # confirmed | pending
    pending_reason: Optional[str]
    published_at: str
    rule_version: str


@dataclass(frozen=True)
class Closure:
    """A target assertion closed by a validated relation verb."""

    closure_id: str
    relation_id: str
    acting_document_id: str
    target_assertion_id: str
    verb: str
    field: str
    published_at: str


@dataclass(frozen=True)
class Unparsed:
    """An in-scope but unsupported expression: kept, never silently dropped."""

    unparsed_id: str
    document_id: str
    field: Optional[str]
    reason: str
    raw_text: str
    span: Span
    published_at: str
    rule_version: str
