"""Monthly purchase-fund close (资金月结).

Separate from the announcement fact pipeline: the announcement result
store is used **read-only** as evidence for which section a payment's
money belongs to.  Budgets, award amounts and publication times are
never treated as payments, and the extraction/relation rules of the
existing pipeline are untouched.

Public API::

    from bidfact.fund import (trial_close, close_month, read_month,
                              months_closed, read_cumulative)

All amounts are positive integers in the currency's minor unit; every
allocation and refund uses exact integer arithmetic
(:mod:`bidfact.fund.money`).
"""
from .close import (Report, close_month, months_closed, read_cumulative,
                    read_month, trial_close)
from .errors import CloseBlocked, FundError, InputError, MonthAlreadyClosed
from .impact import (STATUS_CLOSED, STATUS_CONTESTED, STATUS_UNCHANGED,
                     STATUS_UNVERIFIABLE, ImpactError, ImpactFilters,
                     ImpactReport, analyze_impact, render_terminal,
                     write_report_jsonl)

__all__ = [
    "FundError", "InputError", "CloseBlocked", "MonthAlreadyClosed",
    "Report", "trial_close", "close_month", "read_month", "months_closed",
    "read_cumulative",
    "ImpactError", "ImpactFilters", "ImpactReport", "analyze_impact",
    "render_terminal", "write_report_jsonl",
    "STATUS_UNCHANGED", "STATUS_CONTESTED", "STATUS_CLOSED",
    "STATUS_UNVERIFIABLE",
]
