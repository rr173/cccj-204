"""Offline receipt-lot lineage and recall lists for `bidfact`.

This package answers one question: *a supplier recalled a purchase lot —
which warehouse pieces are affected, where are they now, who already has
the rest, and how did each quantity travel from the original receipt?*

Design boundaries (matching the project's rules for the fund module):

* it never touches stock (no receiving, picking, adjusting or shipping
  UI) — it only reads a package of rows the warehouse wrote;
* it never derives physical locations from announcement amounts, award
  claims, publication dates or the fund ledger;
* the announcement result store is opened **read-only** only to prove
  that the receipt's ``(project_id, section_id)`` under the cited
  ``result_id``/``document_id`` is registered.  The interpretation of
  award assertions is *not* used;
* build and recall never modify source files, the announcement store or
  the library (recall is read-only);
* a recall is driven by the exact key ``(supplier_id, supplier_batch,
  sku)`` and a business cutoff sequence, never by name matching or by
  amount.

Public API::

    from bidfact.warehouse import (build_library, recall, iter_recall,
                                   RecallQuery, BuildReport)

Commands: ``bidfact warehouse build`` and ``bidfact warehouse recall``.
"""
from .build import BuildReport, build_library
from .errors import (BuildBlocked, LibraryError, LibraryExists, PackageError,
                     RecallError, WarehouseError)
from .inputs import SourceRef
from .recall import (RecallQuery, RecallReport, iter_recall, recall,
                     write_recall_json, write_recall_jsonl)
from .store import library_digest, library_path

__all__ = [
    "WarehouseError", "PackageError", "BuildBlocked", "LibraryError",
    "LibraryExists", "RecallError",
    "BuildReport", "build_library",
    "SourceRef", "RecallQuery", "RecallReport", "recall", "iter_recall",
    "write_recall_json", "write_recall_jsonl",
    "library_path", "library_digest",
]
