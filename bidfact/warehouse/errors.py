"""Typed failures for the offline warehouse-lineage library.

Line-level and referential problems are **not** raised: they are collected
as blocking issues carrying their source file and 1-based line, so one build
run can show everything that is wrong.  The exceptions below are for a
package that cannot be read at all, for refusing to overwrite a usable
library, and for a recall request that is malformed.
"""
from __future__ import annotations


class WarehouseError(Exception):
    """Base class for expected warehouse failures."""


class PackageError(WarehouseError):
    """The package manifest/announcement store cannot be read at all."""


class BuildBlocked(WarehouseError):
    """Rows are invalid; no library was delivered."""

    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__(
            f"warehouse build blocked by {len(issues)} issue(s); "
            "the existing library was left untouched")


class LibraryError(WarehouseError):
    """The lineage library is missing, unreadable or of another schema."""


class LibraryExists(WarehouseError):
    """A different, already usable library exists and --force was not given."""

    def __init__(self, path: str, existing_digest: str, new_digest: str):
        self.path = path
        self.existing_digest = existing_digest
        self.new_digest = new_digest
        super().__init__(
            f"{path} already holds a usable warehouse library built from "
            f"different inputs ({existing_digest[:16]}… vs "
            f"{new_digest[:16]}…); pass --force to replace it")


class RecallError(WarehouseError):
    """The recall request itself is malformed (bad range, unknown receipt)."""
