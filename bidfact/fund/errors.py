"""Typed failures for the monthly fund close.

Line-level and referential problems are *not* raised: they are recorded
as blocking issues carrying their source file and line, so a trial run
can show everything that is wrong at once.  The exceptions below are
for a malformed monthly package, an impossible close request, or a
ledger state that must never be overwritten.
"""
from __future__ import annotations


class FundError(Exception):
    """Base class for expected fund-close failures."""


class InputError(FundError):
    """The monthly input package cannot be read at all (manifest shape)."""


class CloseBlocked(FundError):
    """A close was attempted while blocking issues remain."""

    def __init__(self, month: str, issues: list[dict]):
        self.month = month
        self.issues = issues
        super().__init__(
            f"{month}: close blocked by {len(issues)} issue(s); "
            "run trial to inspect them")


class MonthAlreadyClosed(FundError):
    """The month is closed and the submitted inputs are not identical."""

    def __init__(self, month: str, closed_input_digest: str,
                 submitted_input_digest: str):
        self.month = month
        self.closed_input_digest = closed_input_digest
        self.submitted_input_digest = submitted_input_digest
        super().__init__(
            f"{month} is already closed with different inputs "
            f"(closed {closed_input_digest[:16]}…, submitted "
            f"{submitted_input_digest[:16]}…); old months are never overwritten")
