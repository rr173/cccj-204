"""Money parsing: currencies, 元/万元 units and table-cell shorthand.

Deliberate rules:

* 万元 conversion uses exact decimal arithmetic (``Decimal``), never
  floating point: ``0.0001 万元 == 1 元``.
* A cell may omit its unit ("100" under a "预算（万元）" header); the
  unit is then inherited from the table header, and only from there.
* If no currency is expressed anywhere, the result currency is
  ``unknown_currency`` — RMB is never assumed by default.
* Capital numbers (壹佰万…), ranges and hedged amounts (约/左右/余)
  are outside the supported grammar and return None so the caller can
  keep them as ``unparsed``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from .util import wan_to_yuan

CURRENCY_MAP = {
    "人民币": "CNY", "元": "CNY", "圆": "CNY", "rmb": "CNY", "cny": "CNY",
    "美元": "USD", "美金": "USD", "usd": "USD",
    "欧元": "EUR", "eur": "EUR",
    "港币": "HKD", "港元": "HKD", "hkd": "HKD",
    "日元": "JPY", "日圆": "JPY", "jpy": "JPY",
}
UNKNOWN_CURRENCY = "unknown_currency"

_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_CURRENCY_WORDS = (r"人民币|美元|美金|欧元|港币|港元|日元|日圆"
                   r"|rmb|cny|usd|eur|hkd|jpy")
_UNIT_WORDS = r"万人民币|万元|万|元|圆"
# Currency may lead ("人民币 880,000 元") or follow the number
# ("100 万元"); unit words follow the number.
_AMOUNT_RE = re.compile(
    rf"(?P<prefix>{_CURRENCY_WORDS})?\s*"
    rf"(?P<num>{_NUMBER})\s*"
    rf"(?P<unit>{_UNIT_WORDS}|[A-Za-z]{{3}})?",
    re.IGNORECASE)
_CAPITAL_MONEY_RE = re.compile(
    r"[壹贰叁肆伍陆柒捌玖拾佰仟]{2,}[壹贰叁肆伍陆柒捌玖拾佰仟万亿圆角分整]*")
_HEDGE_RE = re.compile(r"约|左右|余|上下|大概")
# Ranges need a real connector; "-" alone between plain ints is too
# noisy (dates, project ids) so 至/到/~/～/— are required, hyphen only
# when flanked by spaces. Optional units may follow each operand.
_RANGE_RE = re.compile(
    rf"(?:{_NUMBER})\s*(?:万?元|万人民币)?\s*"
    r"(?:至|到|~|～|—|\s-\s)\s*"
    rf"(?:{_NUMBER})\s*(?:万?元|万人民币)?")


@dataclass(frozen=True)
class MoneyParse:
    amount: str            # normalized amount, in yuan (exact decimal string)
    currency: str          # ISO code or unknown_currency
    unit_raw: str          # the unit word actually used / inherited, "" if none
    number_raw: str        # the number exactly as written, commas kept


def header_unit(header: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Extract (unit, currency) hints from a table header string."""
    if not header:
        return None, None
    unit: Optional[str] = None
    currency: Optional[str] = None
    if "万元" in header or "万人民币" in header:
        unit = "万元"
    elif re.search(r"人民币|元\b|（元）|\(元\)|金额", header):
        unit = "元"
    for word, code in CURRENCY_MAP.items():
        haystack = header.lower() if word.isascii() else header
        if word in haystack:
            currency = code
            break
    return unit, currency


def _looks_unsupported(text: str) -> bool:
    return bool(_CAPITAL_MONEY_RE.search(text) or _HEDGE_RE.search(text)
                or _RANGE_RE.search(text))


def parse_amount(text: str, header: Optional[str] = None
                 ) -> Optional[MoneyParse]:
    """Parse one money expression; None means unsupported/non-money."""
    stripped = text.strip()
    if not stripped or _looks_unsupported(stripped):
        return None
    m = _AMOUNT_RE.fullmatch(stripped)
    if not m:
        return None
    number_raw = m.group("num")
    unit_word = m.group("unit") or ""
    prefix_word = m.group("prefix") or ""
    try:
        # number_raw only ever contains digits, commas and a dot.
        number = Decimal(number_raw.replace(",", ""))
    except InvalidOperation:
        return None
    h_unit, h_currency = header_unit(header)
    currency: Optional[str] = None
    if prefix_word:
        currency = CURRENCY_MAP.get(prefix_word.lower())
    if currency is None and unit_word:
        currency = CURRENCY_MAP.get(unit_word.lower())
    if currency is None and h_currency:
        currency = h_currency

    if unit_word in ("万元", "万人民币", "万"):
        amount = wan_to_yuan(format(number, "f"))
        unit_raw = unit_word
        currency = currency or "CNY"  # 万元 itself contains the yuan marker
    elif unit_word:
        amount = format(number, "f")
        unit_raw = unit_word
    elif h_unit == "万元":
        # Cell shorthand: bare number under a 万元 header.
        amount = wan_to_yuan(format(number, "f"))
        unit_raw = "header:万元"
    else:
        amount = format(number, "f")
        unit_raw = h_unit and f"header:{h_unit}" or ""

    return MoneyParse(amount=amount,
                      currency=currency or UNKNOWN_CURRENCY,
                      unit_raw=unit_raw,
                      number_raw=number_raw)


def contains_capital_money(text: str) -> bool:
    return bool(_CAPITAL_MONEY_RE.search(text))


def find_amount(text: str, header: Optional[str] = None
                ) -> Optional[tuple[int, int, MoneyParse]]:
    """Find the first parseable money expression inside a text window.

    Returns relative (start, end, parse). Tokens that fail the grammar
    (capital numbers, hedged/ranged amounts) are skipped here so the
    caller can surface them through the unparsed channel instead.
    """
    for m in _AMOUNT_RE.finditer(text):
        full = m.group(0)
        token = full.strip()
        parsed = parse_amount(token, header)
        if parsed is not None:
            # Advance past any leading whitespace so the span starts at
            # the first character of the actual money expression.
            lead = len(full) - len(full.lstrip())
            start = m.start() + lead
            return start, start + len(token), parsed
    return None


def find_unsupported_money(text: str) -> list[tuple[int, int, str]]:
    """Return spans of recognizably-money but unsupported expressions."""
    out: list[tuple[int, int, str]] = []
    for m in _CAPITAL_MONEY_RE.finditer(text):
        out.append((m.start(), m.end(), "capital-number money not in "
                    "supported grammar"))
    for m in _RANGE_RE.finditer(text):
        out.append((m.start(), m.end(), "money range unsupported"))
    # Hedged amounts not already part of a range/capital span.
    for m in re.finditer(rf"(?P<num>{_NUMBER})\s*[^。；;\n]{{0,6}}?"
                         r"(?:约|左右|余|上下)", text):
        out.append((m.start(), m.end(), "hedged/approximate amount unsupported"))
    for m in re.finditer(r"(?:约|左右|余|上下)\s*"
                         rf"(?P<num>{_NUMBER})\s*万?元?", text):
        out.append((m.start(), m.end(), "hedged/approximate amount unsupported"))
    # De-duplicate overlapping spans.
    dedup: list[tuple[int, int, str]] = []
    for s, e, reason in out:
        if not any(not (e <= ds or s >= de) for ds, de, _ in dedup):
            dedup.append((s, e, reason))
    return dedup
