"""Date parsing: ISO dates and the supported Chinese date forms.

Rules implemented on purpose:

* ``2024-02-15`` / ``2024/2/15`` (ISO-like, optional padding)
* ``2024年2月15日`` (full Chinese form)
* ``2月15日`` (year omitted) — backfilled **only** with the publishing
  document's own publish year, never with the current system year.
* Time parts are preserved in the raw word; normalization is date-only.

Anything else (``下个月底``, capital-number dates, ranges, ...) is
returned as ``None`` so the extractor keeps it as ``unparsed``.
"""
from __future__ import annotations

import re
from typing import Optional

# 2024-02-15, 2024/2/15, also 2024.02.15
_ISO_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
# 2024年2月15日
_CN_FULL_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# 2月15日 — note: must be tried after the full Chinese form.
_CN_NOYEAR_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")

# Expressions we deliberately do not support; surfaced as unparsed.
_KNOWN_UNSUPPORTED = (
    re.compile(r"下(?:个)?月"),
    re.compile(r"本\s*月"),
    re.compile(r"\d+\s*月\s*(?:初|中|底)"),
    re.compile(r"[一二三四五六七八九十〇零两]{4,}年"),
)


class UnsupportedDate(ValueError):
    """Raised when the token is recognizably a date but out of support."""


def _ymd(year: int, month: int, day: int) -> str:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise UnsupportedDate(f"impossible calendar date {year}-{month}-{day}")
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_date(token: str, publish_year: int) -> Optional[str]:
    """Return normalized YYYY-MM-DD, or None if no supported date is found."""
    m = _ISO_RE.search(token)
    if m:
        return _ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _CN_FULL_RE.search(token)
    if m:
        return _ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _CN_NOYEAR_RE.search(token)
    if m:
        # The omitted year comes from this announcement's publish time only.
        return _ymd(publish_year, int(m.group(1)), int(m.group(2)))
    return None


def find_date(text: str, publish_year: int
              ) -> Optional[tuple[int, int, str]]:
    """Find the first supported date and return (start, end, normalized).

    Tried in fixed priority order so an ISO date and a Chinese date in
    the same window do not depend on dict ordering.
    """
    for pattern in (_ISO_RE, _CN_FULL_RE):
        m = pattern.search(text)
        if m:
            return m.start(), m.end(), _ymd(
                int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _CN_NOYEAR_RE.search(text)
    if m:
        return (m.start(), m.end(),
                _ymd(publish_year, int(m.group(1)), int(m.group(2))))
    return None


def has_unsupported_date(token: str) -> bool:
    return any(p.search(token) for p in _KNOWN_UNSUPPORTED)
