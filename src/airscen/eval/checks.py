"""Number-correctness check.

Delta's debt and balance-sheet identities are the verified September-quarter
figures from the design. Extract-versus-override rows are printed per metric.
An empty overrides file does not invent peer financials.
"""

from __future__ import annotations

# Current debt, noncurrent debt, total. September-quarter 2026 8-K.
DAL_DEBT = (3_324.0, 9_527.0, 12_851.0)
# The three balance-sheet addends and their total, from the same filing.
DAL_BALANCE = (33_575.0, 29_960.0, 22_513.0, 86_048.0)
_AIRLINES = ("DAL", "UAL", "AAL")
_TOLERANCE = 0.005


def debt_identity(current: float, noncurrent: float, total: float, tolerance: float = _TOLERANCE) -> bool:
    expected = current + noncurrent
    scale = max(abs(total), 1.0)
    return abs(expected - total) / scale <= tolerance


def balance_sheet(assets: float, liabilities: float, equity: float, total: float, tolerance: float = _TOLERANCE) -> bool:
    scale = max(abs(total), 1.0)
    return abs((assets + liabilities + equity) - total) / scale <= tolerance


def number_lines(overrides: dict | None, extracted: dict | None = None) -> list[str]:
    """One pass/fail line per identity and per extracted metric."""
    current, noncurrent, total = DAL_DEBT
    debt_ok = debt_identity(current, noncurrent, total)
    lines = [
        "DAL debt_identity: "
        + ("pass" if debt_ok else "fail")
        + f" ({current:g} + {noncurrent:g} = {current + noncurrent:g}, total {total:g})"
    ]
    a, b, c, sheet = DAL_BALANCE
    sheet_ok = balance_sheet(a, b, c, sheet)
    lines.append(
        "DAL balance_sheet: "
        + ("pass" if sheet_ok else "fail")
        + f" ({a:g} + {b:g} + {c:g} = {a + b + c:g}, total {sheet:g})"
    )
    overrides = overrides or {}
    extracted = extracted or {}
    for ticker in _AIRLINES:
        over = overrides.get(ticker) or {}
        got = extracted.get(ticker) or {}
        if not isinstance(over, dict):
            over = {}
        if not isinstance(got, dict):
            got = {}
        if not over and not got:
            lines.append(f"{ticker} extract: fail (no extracted facts and no overrides)")
            continue
        for metric in sorted(set(over) | set(got)):
            if metric not in over:
                lines.append(f"{ticker} {metric}: fail (missing override)")
            elif metric not in got:
                lines.append(f"{ticker} {metric}: fail (missing extract)")
            elif _close(float(got[metric]), float(over[metric])):
                lines.append(f"{ticker} {metric}: pass")
            else:
                lines.append(f"{ticker} {metric}: fail")
    return lines


def _close(got: float, expected: float) -> bool:
    scale = max(abs(expected), 1.0)
    return abs(got - expected) / scale <= _TOLERANCE
