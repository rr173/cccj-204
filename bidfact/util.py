"""Small deterministic helpers (hashing, precise money conversion)."""
from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


def stable_id(prefix: str, *parts: object) -> str:
    """Deterministic id so a re-run never creates duplicate assertions."""
    joined = "\x1f".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha256(joined.encode('utf-8')).hexdigest()[:16]}"


# 万元 -> yuan as exact decimal. Decimal(str(float)) would introduce
# binary noise, so callers must pass strings.
_WAN = Decimal("10000")


def wan_to_yuan(value: str) -> str:
    """Convert a 万元 decimal string to yuan, keeping exact precision.

    ``100`` -> ``"1000000"``; ``0.0001`` -> ``"1"``; ``120.5`` ->
    ``"1205000"``.  Raises InvalidOperation on non-decimal input.
    """
    d = Decimal(value) * _WAN
    if d == d.to_integral_value():
        return str(d.quantize(Decimal("1")))
    # A 万元 value with more than 4 decimal places produces fractional
    # yuan; keep the exact decimal rather than rounding silently.
    normalized = d.normalize()
    return format(normalized, "f")


def is_decimal(value: str) -> bool:
    try:
        Decimal(value)
        return True
    except (InvalidOperation, ValueError):
        return False


def fingerprint(rule_version: str, manifest_digest: str, relations_digest: str,
                file_digests: dict[str, str]) -> str:
    """Bind one result number to the rule version and this exact corpus."""
    h = hashlib.sha256()
    h.update(b"bidfact-result\n")
    h.update(rule_version.encode("utf-8"))
    h.update(b"\n")
    h.update(manifest_digest.encode("utf-8"))
    h.update(b"\n")
    h.update(relations_digest.encode("utf-8"))
    h.update(b"\n")
    for name in sorted(file_digests):
        h.update(name.encode("utf-8"))
        h.update(b":")
        h.update(file_digests[name].encode("ascii"))
        h.update(b"\n")
    return "R" + h.hexdigest()[:16]
