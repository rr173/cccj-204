"""Exact integer money arithmetic for the monthly fund close.

Every amount in the monthly fund ledger is a positive integer number of
minor currency units (``分`` for CNY).  No floating point and no FX
conversion is used anywhere:

* a **weight** is a positive integer;
* a **refund rate** is an exact rational, either ``"a/b"`` or a decimal
  string such as ``"0.5"``, kept as :class:`fractions.Fraction`;
* splitting an integer total by weights uses integer quotient plus a
  largest-remainder allocation (ties broken by caller supplied key),
  never rounding of a decimal quotient;
* applying a rate to a line uses ``floor(remaining * rate)`` in exact
  rational arithmetic; any residue that cannot be paid in whole minor
  units is reported instead of being hidden in a monthly total.

Strings are compared with Python ``str`` ordering, i.e. by Unicode code
point, case-sensitively.  That is the same order SQLite's BINARY
collation gives UTF-8 text.  The helpers below make that comparison
available to SQL order-by clauses as a registered collation.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction


class MoneyError(ValueError):
    """An amount, weight or rate is not in the accepted exact form."""


def _reject_bool(value: object, what: str) -> None:
    if isinstance(value, bool):
        raise MoneyError(f"{what} must be an integer, got bool")


def parse_positive_int(value: object, what: str = "amount") -> int:
    """Parse a positive integer given either as JSON int or digit string.

    Thousands separators, decimals, signs and booleans are refused: a
    stored amount must be exactly the integer the finance file wrote.
    """
    _reject_bool(value, what)
    if isinstance(value, int):
        n = value
    elif isinstance(value, str):
        if not value or not value.isascii() or not value.isdigit():
            raise MoneyError(
                f"{what} must be a positive integer, got {value!r}")
        n = int(value)
    else:
        raise MoneyError(f"{what} must be a positive integer, got {value!r}")
    if n <= 0:
        raise MoneyError(f"{what} must be a positive integer, got {n}")
    return n


def parse_non_negative_int(value: object, what: str = "amount") -> int:
    _reject_bool(value, what)
    if isinstance(value, int):
        n = value
    elif isinstance(value, str):
        if not value or not value.isascii() or not value.isdigit():
            raise MoneyError(
                f"{what} must be a non-negative integer, got {value!r}")
        n = int(value)
    else:
        raise MoneyError(
            f"{what} must be a non-negative integer, got {value!r}")
    if n < 0:
        raise MoneyError(f"{what} must be a non-negative integer, got {n}")
    return n


def parse_month(value: object, what: str = "month") -> str:
    """Require a literal ``YYYY-MM`` calendar month."""
    if (not isinstance(value, str) or len(value) != 7 or value[4] != "-"
            or not value[:4].isascii() or not value[:4].isdigit()
            or not value[5:].isascii() or not value[5:].isdigit()
            or not 1 <= int(value[5:]) <= 12):
        raise MoneyError(f"{what} must be YYYY-MM, got {value!r}")
    return value


def next_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:])
    return f"{year + 1:04d}-01" if mon == 12 else f"{year:04d}-{mon + 1:02d}"


def parse_rate(value: object, what: str = "rate") -> Fraction:
    """Parse an exact positive rational from ``"a/b"`` or a decimal string.

    Decimals go through :class:`decimal.Decimal`, so binary ``float``
    noise never enters the calculation.  Zero and negative rates are
    rejected; there is no implicit percentage scaling.
    """
    if isinstance(value, bool):
        raise MoneyError(f"{what} must be a positive ratio, got bool")
    if isinstance(value, int):
        if value <= 0:
            raise MoneyError(f"{what} must be positive, got {value}")
        return Fraction(value, 1)
    if not isinstance(value, str):
        raise MoneyError(f"{what} must be a string ratio, got {value!r}")
    text = value.strip()
    if not text:
        raise MoneyError(f"{what} is empty")
    if "/" in text:
        parts = text.split("/")
        if len(parts) != 2:
            raise MoneyError(f"{what} must be a/b, got {value!r}")
        try:
            n, d = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise MoneyError(f"{what} must be a/b, got {value!r}") from exc
        if n <= 0 or d <= 0:
            raise MoneyError(f"{what} must be positive, got {value!r}")
        return Fraction(n, d)
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(
            f"{what} is not a decimal ratio: {value!r}") from exc
    if not dec.is_finite() or dec <= 0:
        raise MoneyError(
            f"{what} must be a positive finite ratio, got {value!r}")
    return Fraction(dec)


def split_by_weights(total: int, weights: list[int],
                     keys: list[tuple[str, ...]]) -> tuple[list[int], list[int]]:
    """Split ``total`` minor units by positive integer weights, exactly.

    Each row first gets ``total * weight // sum(weights)``.  The units
    left over are handed out one per row in descending order of
    ``total * weight % sum(weights)``; ties are broken by ``keys``
    ascending (callers pass ``(project_id, section_id)``, compared by
    Unicode code point).  Rows may legitimately receive zero and the
    sum of the result is always exactly ``total``.

    Returns ``(allocations, remainders)`` so callers can persist the
    ordering evidence.
    """
    if not weights:
        raise MoneyError("cannot split across an empty weight list")
    if any(w <= 0 for w in weights):
        raise MoneyError("all weights must be positive integers")
    if total < 0:
        raise MoneyError("cannot split a negative total")
    if len(weights) != len(keys):
        raise MoneyError("weights and keys must align")
    denominator = sum(weights)
    bases: list[int] = []
    remainders: list[int] = []
    for w in weights:
        product = total * w
        bases.append(product // denominator)
        remainders.append(product % denominator)
    leftover = total - sum(bases)
    order = sorted(range(len(weights)),
                   key=lambda i: (-remainders[i], keys[i]))
    if leftover > len(order):
        raise MoneyError("internal error: remainder allocation overflow")
    for i in order[:leftover]:
        bases[i] += 1
    return bases, remainders


def floor_rate(units: int, rate: Fraction) -> int:
    """``floor(units * rate)`` in exact rational arithmetic."""
    if units < 0:
        raise MoneyError("cannot apply a rate to a negative amount")
    return (units * rate.numerator) // rate.denominator


def rate_remainder(units: int, rate: Fraction) -> Fraction:
    """The exact fractional residue left after :func:`floor_rate`."""
    return Fraction(units) * rate - floor_rate(units, rate)


def format_rate(rate: Fraction, scale: int = 18) -> str:
    """Render a rate as a decimal string, or exactly as ``a/b``.

    A rate whose denominator has prime factors other than 2 and 5 has no
    finite decimal expansion; returning ``a/b`` keeps it exact instead
    of fabricating digits.
    """
    denom = rate.denominator
    for p in (2, 5):
        while denom % p == 0:
            denom //= p
    if denom != 1:
        return f"{rate.numerator}/{rate.denominator}"
    scaled = rate * (10 ** scale)
    if scaled.denominator != 1:
        return f"{rate.numerator}/{rate.denominator}"
    digits = str(scaled.numerator).rjust(scale + 1, "0")
    whole, frac = digits[:-scale], digits[-scale:]
    frac = frac.rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def parse_exact_decimal(text: str) -> Fraction:
    """Exact fraction of a persisted decimal/fraction string."""
    return parse_rate(text, "persisted rate")


def compare_codepoints(a: object, b: object) -> int:
    """SQLite collation: case-sensitive Unicode code point order."""
    sa, sb = str(a), str(b)
    return (sa > sb) - (sa < sb)


def register_collations(conn: sqlite3.Connection) -> None:
    """Make ``CODEPOINT`` available for ORDER BY in SQL (older SQLite)."""
    try:
        conn.create_collation("CODEPOINT", compare_codepoints)
    except sqlite3.Error:
        # Read-only or an engine that refuses custom collations: callers
        # also sort in Python, so this is only a convenience.
        pass


@dataclass(frozen=True)
class WeightedLine:
    """One allocation scheme line: section key, weight, announcement proof."""

    project_id: str
    section_id: str
    weight: int
    result_id: str
    result_unit_assertion_id: str


# --------------------------------------------------------------------------
# Warehousing type helpers (``bidfact warehouse``).
#
# Quantities are positive integers in *pieces*; intervals are half-open
# ``[start, end)`` with 0 <= start < end.  They are deliberately kept
# separate from minor-currency amounts above: a piece is not money, and a
# negative or decimal quantity is always a malformed row, never rounded.

def parse_non_negative_int_strict(value: object, what: str = "offset") -> int:
    """Like :func:`parse_non_negative_int` but refuses negative zero forms."""
    n = parse_non_negative_int(value, what)
    return n


def parse_positive_quantity(value: object, what: str = "qty") -> int:
    return parse_positive_int(value, what)


def check_interval(start: object, end: object, what: str = "interval",
                   require_positive: bool = True) -> tuple[int, int]:
    """Validate ``[start, end)``; returns the exact integer pair."""
    s = parse_non_negative_int_strict(start, f"{what}.start")
    e = parse_non_negative_int_strict(end, f"{what}.end")
    if require_positive and e <= s:
        raise MoneyError(
            f"{what} must be a non-empty half-open interval [start, end), "
            f"got [{s}, {e})")
    if not require_positive and e < s:
        raise MoneyError(
            f"{what} must have start <= end, got [{s}, {e})")
    return s, e
