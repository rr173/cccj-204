"""Month package manifest and streaming JSONL readers.

A *month package* is one directory per account set (``账套``) with

* ``manifest.json`` — package metadata (see below);
* JSON Lines files for the monthly business rows, one JSON object per
  line, declared by ``files``;
* the upstream control outputs declared by ``control_files``;
* a pointer to the read-only announcement result store.

Manifest::

    {
      "ledger_id": "BOOK-1",
      "month": "2024-01",
      "announcement_result_dir": "../announcement_results/R…",
      "files": {
        "bank_flow": "bank_flows.jsonl",
        "voucher": "vouchers.jsonl",
        "clearing": "clearing.jsonl",
        "allocation_plan": "allocation_plan.jsonl"
      },
      "control_files": {
        "bank": "control_bank.jsonl",
        "voucher": "control_voucher.jsonl"
      }
    }

Row shapes (all amounts are positive integers in the currency's minor
unit — no decimals, no thousands separators, no floats; all ids are
case-sensitive strings)::

    bank_flow   {kind, ledger_id, account_id, bank_flow_id, month,
                 currency, direction: outflow|inflow, amount_minor, memo?}
    voucher     {kind, ledger_id, voucher_id, month, currency, direction,
                 amount_minor, voucher_type: payment|refund,
                 payment_voucher_id? (refund only: the original payment),
                 rate? (refund only, optional: the returned share of the
                 original as an exact "a/b" or decimal string, checked
                 when present), memo?}
    clearing    {kind, ledger_id, account_id, bank_flow_id, voucher_id,
                 amount_minor, memo?}
    allocation_plan
                {kind, ledger_id, plan_id, voucher_id, project_id,
                 section_id, weight, result_id,
                 result_unit_assertion_id, memo?}

Control files (one line per upstream group)::

    {"side": "bank",    "account_id": "ACC-1", "currency": "CNY",
     "direction": "outflow", "count": 2, "amount_minor": 9}
    {"side": "voucher", "currency": "CNY", "direction": "outflow",
     "count": 2, "amount_minor": 9}

Each file is read line by line and every parsed row keeps its 1-based
JSONL line number, which travels into the closed ledger.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .errors import InputError
from .money import MoneyError, parse_month, parse_positive_int

KIND_ORDER = ("bank_flow", "voucher", "clearing", "allocation_plan")
BUSINESS_KINDS = frozenset(KIND_ORDER)

DIRECTION_OUT = "outflow"
DIRECTION_IN = "inflow"
DIRECTIONS = (DIRECTION_OUT, DIRECTION_IN)
VOUCHER_PAYMENT = "payment"
VOUCHER_REFUND = "refund"
VOUCHER_KINDS = (VOUCHER_PAYMENT, VOUCHER_REFUND)

REQUIRED_KEYS = {
    "bank_flow": ("kind", "ledger_id", "account_id", "bank_flow_id",
                  "month", "currency", "direction", "amount_minor"),
    "voucher": ("kind", "ledger_id", "voucher_id", "month", "currency",
                "direction", "amount_minor", "voucher_type"),
    "clearing": ("kind", "ledger_id", "account_id", "bank_flow_id",
                 "voucher_id", "amount_minor"),
    "allocation_plan": ("kind", "ledger_id", "plan_id", "voucher_id",
                        "project_id", "section_id", "weight",
                        "result_id", "result_unit_assertion_id"),
}
OPTIONAL_KEYS = {
    "bank_flow": ("memo",),
    "voucher": ("payment_voucher_id", "rate", "memo"),
    "clearing": ("memo",),
    "allocation_plan": ("memo",),
}


@dataclass(frozen=True, order=True)
class SourceRef:
    """Where one input row was read from (1-based JSONL line, 0 = file)."""

    file: str
    line: int

    def as_dict(self) -> dict:
        return {"file": self.file, "line": self.line}


@dataclass(frozen=True)
class BankFlow:
    ledger_id: str
    account_id: str
    bank_flow_id: str
    month: str
    currency: str
    direction: str
    amount_minor: int
    memo: Optional[str]
    source: SourceRef

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.ledger_id, self.account_id, self.bank_flow_id)


@dataclass(frozen=True)
class Voucher:
    ledger_id: str
    voucher_id: str
    month: str
    currency: str
    direction: str
    amount_minor: int
    voucher_type: str
    payment_voucher_id: Optional[str]
    rate: Optional[str]
    memo: Optional[str]
    source: SourceRef

    @property
    def key(self) -> tuple[str, str]:
        return (self.ledger_id, self.voucher_id)


@dataclass(frozen=True)
class Clearing:
    ledger_id: str
    account_id: str
    bank_flow_id: str
    voucher_id: str
    amount_minor: int
    memo: Optional[str]
    source: SourceRef


@dataclass(frozen=True)
class PlanLine:
    ledger_id: str
    plan_id: str
    voucher_id: str
    project_id: str
    section_id: str
    weight: int
    result_id: str
    result_unit_assertion_id: str
    memo: Optional[str]
    source: SourceRef


@dataclass(frozen=True)
class ControlRow:
    side: str
    account_id: Optional[str]
    currency: str
    direction: str
    count: int
    amount_minor: int
    source: SourceRef


@dataclass
class MonthPackage:
    directory: Path
    ledger_id: str
    month: str
    files: dict[str, str]
    control_files: dict[str, str]
    announcement_result_dir: Path
    manifest_digest: str
    announcement_store_digest: str


# ------------------------------------------------------------------ manifest
def _relative_name(value: object, what: str) -> str:
    if (not isinstance(value, str) or not value
            or Path(value).is_absolute() or ".." in Path(value).parts):
        raise InputError(f"{what} must be a relative file name, got {value!r}")
    return value


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_package(directory: Path) -> MonthPackage:
    """Read the manifest only; row files are streamed later."""
    directory = Path(directory).resolve()
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise InputError(f"missing month manifest: {manifest_path}")
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{manifest_path}: not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise InputError(f"{manifest_path}: manifest must be a JSON object")

    ledger_id = manifest.get("ledger_id")
    if not isinstance(ledger_id, str) or not ledger_id:
        raise InputError("manifest.ledger_id must be a non-empty string")
    try:
        month = parse_month(manifest.get("month"))
    except MoneyError as exc:
        raise InputError(f"manifest.month: {exc}") from exc

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise InputError("manifest.files must be an object kind -> file name")
    for kind, name in files.items():
        if kind not in BUSINESS_KINDS:
            raise InputError(f"manifest.files has unknown kind {kind!r}")
        _relative_name(name, f"manifest.files[{kind!r}]")

    control_files = manifest.get("control_files", {})
    if not isinstance(control_files, dict):
        raise InputError("manifest.control_files must be object side -> file")
    for side, name in control_files.items():
        if side not in ("bank", "voucher"):
            raise InputError(
                f"manifest.control_files has unknown side {side!r}")
        _relative_name(name, f"manifest.control_files[{side!r}]")

    ann = manifest.get("announcement_result_dir")
    if not isinstance(ann, str) or not ann:
        raise InputError("manifest.announcement_result_dir must be a path")
    ann_path = (directory / ann).resolve()
    store_path = ann_path / "store.sqlite3"
    if not store_path.is_file():
        raise InputError(
            f"announcement_result_dir {ann!r} has no store.sqlite3")

    return MonthPackage(
        directory=directory, ledger_id=ledger_id, month=month, files=files,
        control_files=control_files, announcement_result_dir=ann_path,
        manifest_digest=hashlib.sha256(raw).hexdigest(),
        announcement_store_digest=hash_file(store_path))


# ------------------------------------------------------------------ streaming
def make_issue(code: str, ref: SourceRef, message: str,
               detail: Optional[dict] = None) -> dict:
    payload = {"code": code, "message": message, "source": ref.as_dict()}
    if detail:
        payload["detail"] = detail
    return payload


def iter_file(package: MonthPackage, rel: str, sink, hash_sink=None
              ) -> Iterator[tuple[int, dict]]:
    """Stream one JSONL file line by line, reporting shape problems.

    ``hash_sink(name, digest)`` receives the file digest; the caller
    decides whether that participates in the submitted-input digest.
    """
    path = package.directory / rel
    if not path.is_file():
        sink(make_issue("file_missing", SourceRef(rel, 0),
                        f"declared input file {rel!r} does not exist"))
        return
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for lineno, raw in enumerate(fh, 1):
                digest.update(raw)
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    msg = getattr(exc, "msg", str(exc))
                    sink(make_issue(
                        "line_not_json", SourceRef(rel, lineno),
                        f"line is not valid UTF-8 JSON: {msg}"))
                    continue
                if not isinstance(value, dict):
                    sink(make_issue(
                        "line_not_object", SourceRef(rel, lineno),
                        "JSONL line must be one JSON object"))
                    continue
                yield lineno, value
    finally:
        if hash_sink is not None:
            hash_sink(rel, digest.hexdigest())


# ------------------------------------------------------------------- parsing
def parse_row(kind: str, row: dict, ref: SourceRef,
              sink) -> Optional[object]:
    """Shape-check one row; report every defect with its line and drop it."""
    def fail(code: str, message: str, detail: Optional[dict] = None):
        sink(make_issue(code, ref, message, detail))

    for key in REQUIRED_KEYS[kind]:
        if key not in row:
            fail("schema_missing_field",
                 f"{kind} row is missing {key}", {"field": key})
            return None

    ledger_id = _string(row, "ledger_id", fail)
    optional: dict = {}
    for key in OPTIONAL_KEYS[kind]:
        value = row.get(key)
        if value is not None and not isinstance(value, str):
            fail("schema_bad_field",
                 f"{key} must be a string when present, got {value!r}",
                 {"field": key, "value": value})
            return None
        optional[key] = value
    if ledger_id is None:
        return None

    if kind == "bank_flow":
        month = _month_field(row, fail)
        currency = _string(row, "currency", fail)
        direction = _direction_field(row, fail)
        amount = _amount_field(row, fail)
        account_id = _string(row, "account_id", fail)
        flow_id = _string(row, "bank_flow_id", fail)
        if None in (month, currency, direction, account_id, flow_id) \
                or amount is None:
            return None
        return BankFlow(ledger_id, account_id, flow_id, month, currency,
                        direction, amount, optional["memo"], ref)

    if kind == "voucher":
        month = _month_field(row, fail)
        currency = _string(row, "currency", fail)
        direction = _direction_field(row, fail)
        amount = _amount_field(row, fail)
        voucher_id = _string(row, "voucher_id", fail)
        vtype = _string(row, "voucher_type", fail)
        if vtype is not None and vtype not in VOUCHER_KINDS:
            fail("voucher_type_unknown",
                 f"voucher_type must be one of {list(VOUCHER_KINDS)}, "
                 f"got {vtype!r}")
            return None
        linked = optional["payment_voucher_id"]
        rate = optional["rate"]
        if vtype == VOUCHER_REFUND and not linked:
            fail("refund_missing_original",
                 "refund voucher must name payment_voucher_id")
            return None
        if vtype == VOUCHER_PAYMENT and (linked or rate):
            fail("payment_carries_refund_fields",
                 "payment voucher must not carry payment_voucher_id/rate")
            return None
        if None in (month, currency, direction, voucher_id) or amount is None:
            return None
        return Voucher(ledger_id, voucher_id, month, currency, direction,
                       amount, vtype, linked, rate, optional["memo"], ref)

    if kind == "clearing":
        amount = _amount_field(row, fail)
        account_id = _string(row, "account_id", fail)
        flow_id = _string(row, "bank_flow_id", fail)
        voucher_id = _string(row, "voucher_id", fail)
        if None in (account_id, flow_id, voucher_id) or amount is None:
            return None
        return Clearing(ledger_id, account_id, flow_id, voucher_id, amount,
                        optional["memo"], ref)

    if kind == "allocation_plan":
        weight = row.get("weight")
        try:
            weight = parse_positive_int(weight, "weight")
        except MoneyError as exc:
            fail("bad_weight", str(exc),
                 {"field": "weight", "value": weight})
            return None
        plan_id = _string(row, "plan_id", fail)
        voucher_id = _string(row, "voucher_id", fail)
        project_id = _string(row, "project_id", fail)
        section_id = _string(row, "section_id", fail)
        result_id = _string(row, "result_id", fail)
        assertion_id = _string(row, "result_unit_assertion_id", fail)
        if None in (plan_id, voucher_id, project_id, section_id, result_id,
                    assertion_id):
            return None
        return PlanLine(ledger_id, plan_id, voucher_id, project_id, section_id,
                        weight, result_id, assertion_id, optional["memo"], ref)

    raise AssertionError(kind)


def _string(row: dict, key: str, fail) -> Optional[str]:
    value = row.get(key)
    if not isinstance(value, str) or value == "":
        fail("schema_bad_field",
             f"{key} must be a non-empty string, got {value!r}",
             {"field": key, "value": value})
        return None
    return value


def _month_field(row: dict, fail) -> Optional[str]:
    value = row.get("month")
    try:
        return parse_month(value)
    except MoneyError as exc:
        fail("bad_month", str(exc), {"field": "month", "value": value})
        return None


def _direction_field(row: dict, fail) -> Optional[str]:
    value = row.get("direction")
    if value not in DIRECTIONS:
        fail("bad_direction",
             f"direction must be one of {list(DIRECTIONS)}, got {value!r}",
             {"field": "direction", "value": value})
        return None
    return value


def _amount_field(row: dict, fail) -> Optional[int]:
    value = row.get("amount_minor")
    try:
        return parse_positive_int(value, "amount_minor")
    except MoneyError as exc:
        fail("bad_amount", str(exc),
             {"field": "amount_minor", "value": value})
        return None


def parse_control_row(row: dict, side: str, ref: SourceRef,
                      sink) -> Optional[ControlRow]:
    def fail(code: str, message: str, detail: Optional[dict] = None):
        sink(make_issue(code, ref, message, detail))

    if row.get("kind") not in (None, "control"):
        fail("control_unknown_row",
             "control file may only hold kind='control' rows")
        return None
    declared_side = row.get("side")
    if declared_side is not None and declared_side != side:
        fail("control_side_mismatch",
             f"row declares side {declared_side!r} but was read from the "
             f"{side!r} control file")
        return None
    try:
        count = parse_positive_int(row.get("count"), "count")
        amount = parse_positive_int(row.get("amount_minor"), "amount_minor")
    except MoneyError as exc:
        fail("control_bad_total", str(exc))
        return None
    currency = row.get("currency")
    direction = row.get("direction")
    if not isinstance(currency, str) or not currency:
        fail("control_bad_currency", "currency must be a non-empty string")
        return None
    if direction not in DIRECTIONS:
        fail("control_bad_direction",
             f"direction must be one of {list(DIRECTIONS)}")
        return None
    account_id = row.get("account_id")
    if side == "bank":
        if not isinstance(account_id, str) or not account_id:
            fail("control_bad_account",
                 "bank control rows must name account_id")
            return None
    else:
        if account_id is not None:
            fail("control_bad_account",
                 "voucher control rows are keyed by currency/direction only")
            return None
        account_id = None
    return ControlRow(side, account_id, currency, direction, count, amount,
                      ref)


def submitted_digest(ledger_id: str, month: str, manifest_digest: str,
                     result_id: str, files: dict[str, str]) -> str:
    """Deterministic digest of the *business* inputs of one submission.

    The announcement store bytes are deliberately excluded: the result
    id already pins the evidence, and re-walking the same store must not
    turn an identical resubmission into a false conflict.  Absolute
    paths and timestamps are excluded for the same reason.
    """
    h = hashlib.sha256(b"bidfact-fund-input-v1\n")
    for part in (ledger_id, month, manifest_digest, result_id):
        h.update(part.encode("utf-8"))
        h.update(b"\n")
    for name in sorted(files):
        h.update(name.encode("utf-8"))
        h.update(b":")
        h.update(files[name].encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()
