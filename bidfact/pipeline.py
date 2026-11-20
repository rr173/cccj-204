"""Pipeline orchestration: ingest -> extract -> validate relations -> persist.

One document's page text is loaded, extracted and released before the
next; the durable SQLite store keeps only compact facts and span
references, so memory does not grow with corpus size.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import relations as rel_mod
from .errors import MaterialConflict
from .extract import extract_document
from .ingestion import iter_collection, load_manifest
from .storage import Store
from .util import fingerprint, stable_id

RULE_VERSION = "bidfact-rules-1.0.0"


def run_pipeline(collection_dir: Path, results_dir: Path,
                 relations_file: Path | None = None,
                 force_rebuild: bool = False) -> dict:
    collection_dir = collection_dir.resolve()
    results_dir = results_dir.resolve()
    manifest, manifest_digest = load_manifest(collection_dir)

    file_digests: dict[str, str] = {}
    for doc in manifest["documents"]:
        file_digests[doc["source"]["file"]] = doc["source"]["sha256"]
        for t in doc.get("tables", []):
            file_digests[t["file"]] = t["sha256"]
    rels_payload_digest = "none"
    rels: list[dict] = []
    if relations_file is not None:
        rels, rels_payload_digest = rel_mod.load_relations(relations_file)

    result_id = fingerprint(RULE_VERSION, manifest_digest,
                            rels_payload_digest, file_digests)
    out_dir = results_dir / result_id
    db_path = out_dir / "store.sqlite3"
    if db_path.exists() and not force_rebuild:
        return {"result_id": result_id, "path": str(out_dir), "reused": True}

    store = Store(db_path)
    try:
        store.set_meta("result_id", result_id)
        store.set_meta("rule_version", RULE_VERSION)
        store.set_meta("manifest_digest", manifest_digest)
        store.set_meta("relations_digest", rels_payload_digest)
        store.set_meta("collection_dir", str(collection_dir))

        def issue_sink(issue_id, document_id, kind, detail, span=None):
            store.add_issue(issue_id, document_id, kind, detail, span)

        for doc, pages, tables in iter_collection(
                collection_dir, manifest, issue_sink):
            store.add_file(doc["source"]["file"], doc["source"]["sha256"])
            store.add_document(doc, doc["source"]["sha256"])
            for link in doc.get("same_notice", []):
                store.add_same_notice(link["notice_id"],
                                      link["document_id"], link["site"])
            outcome = extract_document(doc, pages, tables, RULE_VERSION)
            for a in outcome.assertions:
                store.add_assertion(a)
            for u in outcome.unparsed:
                store.add_unparsed(u)
        store.commit()

        rel_results: list[dict] = []
        if rels:
            rel_results = rel_mod.validate_and_apply(
                store, collection_dir, rels)

        n_assertions = store.count("assertions")
        n_closures = store.count("closures")
        store.set_meta("assertion_count", str(n_assertions))
        store.set_meta("closure_count", str(n_closures))
        store.commit()
    except Exception:
        store.close()
        if not force_rebuild:
            # A failed half-built store must not masquerade as a result.
            db_path.unlink(missing_ok=True)
        raise
    else:
        store.close()

    return {"result_id": result_id, "path": str(out_dir), "reused": False,
            "assertions": n_assertions, "relations": rel_results}
