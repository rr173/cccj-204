"""Announcement evidence for allocation plan lines (read-only).

The announcement result store produced by the existing pipeline is the
only evidence used here.  For every ``allocation_plan`` line the fund
close verifies, through the result store's public reader:

* the ``result_id`` exists and is the store it was opened from;
* one ``result_unit`` assertion with the given assertion id is
  ``confirmed`` (not pending, not closed, not withdrawn);
* that assertion belongs to the plan line's ``(project_id, section_id)``
  and names a unit.

What this proves is exactly: *finance chose a section for which a
historical award declaration exists*.  It does **not** require the
declaration to be the latest one, does not compare budget or award
amounts (they are not payments), does not use announcement publication
time, and never matches suppliers by name.

The result store is opened with SQLite's read-only URI mode and the
store's digest is recorded before/after the close, so the monthly
close can prove the evidence database was not modified.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .allocate import PlanRow


class EvidenceError(RuntimeError):
    """The announcement result store cannot be opened read-only."""


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_readonly(store_path: Path) -> sqlite3.Connection:
    uri = f"file:{store_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
        return conn
    except sqlite3.Error as exc:
        raise EvidenceError(
            f"cannot open announcement store {store_path} read-only: {exc}"
        ) from exc


@dataclass(frozen=True)
class Evidence:
    """The persisted announcement locator for one plan line."""

    result_id: str
    document_id: str
    assertion_id: str
    project_id: str
    section_id: str
    unit_raw: str
    published_at: str
    span: dict

    def as_dict(self) -> dict:
        return {"result_id": self.result_id, "document_id": self.document_id,
                "assertion_id": self.assertion_id,
                "project_id": self.project_id, "section_id": self.section_id,
                "unit_raw": self.unit_raw, "published_at": self.published_at,
                "span": self.span}


def open_closed_ids(conn: sqlite3.Connection) -> set[str]:
    """Assertions closed by a correction relation (no longer open facts)."""
    return {r["target_assertion_id"] for r in conn.execute(
        "SELECT target_assertion_id FROM closures")}


def check_plan_line(conn: sqlite3.Connection, plan: PlanRow,
                    result_id: str) -> tuple[Optional[Evidence], Optional[str]]:
    """Return (evidence, None) or (None, reason) for one plan line."""
    stored_result = conn.execute(
        "SELECT value FROM meta WHERE key='result_id'").fetchone()
    if stored_result is None:
        return None, "result_store_has_no_result_id"
    if stored_result["value"] != result_id:
        return None, (f"result_id {result_id!r} does not match the "
                      f"opened store {stored_result['value']!r}")
    row = conn.execute(
        "SELECT * FROM assertions WHERE assertion_id=?",
        (plan.result_unit_assertion_id,)).fetchone()
    if row is None:
        return None, (f"assertion {plan.result_unit_assertion_id!r} does "
                      "not exist in the announcement store")
    if row["field"] != "result_unit":
        return None, (f"assertion {plan.result_unit_assertion_id!r} is "
                      f"{row['field']!r}, not 'result_unit'")
    if row["status"] != "confirmed":
        return None, (f"assertion {plan.result_unit_assertion_id!r} is "
                      f"{row['status']}, not confirmed")
    if plan.result_unit_assertion_id in open_closed_ids(conn):
        return None, (f"assertion {plan.result_unit_assertion_id!r} was "
                      "closed by a later correction relation")
    if row["project_id"] != plan.project_id:
        return None, (f"assertion project {row['project_id']!r} != plan "
                      f"project {plan.project_id!r}")
    if row["section_id"] != plan.section_id:
        return None, (f"assertion section {row['section_id']!r} != plan "
                      f"section {plan.section_id!r}")
    return Evidence(
        result_id=result_id, document_id=row["document_id"],
        assertion_id=row["assertion_id"], project_id=row["project_id"],
        section_id=row["section_id"], unit_raw=row["raw_value"],
        published_at=row["published_at"],
        span=json.loads(row["span_json"])), None
