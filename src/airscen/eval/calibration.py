"""Brier score and a reliability table by liquidity bucket.

Each row is (probability, outcome, liquidity). Outcome is 1 when the event
happened and 0 otherwise. Liquidity may be None when the source file has no
volume; those rows still count in the Brier score and are not put in a bucket.
The reference forecast of always saying 50% has Brier score 0.25. Liquidity
buckets are contracts: low below 1,000, medium below 10,000, otherwise high.
"""

from __future__ import annotations

_ALWAYS_HALF = 0.25


def brier_score(rows: list[tuple]) -> float | None:
    if not rows:
        return None
    return sum((p - y) ** 2 for p, y, *_ in rows) / len(rows)


def reliability_lines(rows: list[tuple]) -> list[str]:
    score = brier_score(rows)
    if score is None:
        return [
            f"Brier score not available (always-50% reference {_ALWAYS_HALF:.2f})",
            "reliability sample size 0",
        ]
    lines = [
        f"Brier score {score:.4f} versus {_ALWAYS_HALF:.2f} for always saying 50%",
        f"calibration sample size {len(rows)}",
    ]
    buckets = {"low": [], "medium": [], "high": []}
    unknown = 0
    for probability, outcome, *rest in rows:
        liquidity = rest[0] if rest else None
        if liquidity is None:
            unknown += 1
            continue
        if liquidity < 1_000:
            name = "low"
        elif liquidity < 10_000:
            name = "medium"
        else:
            name = "high"
        buckets[name].append((probability, outcome))
    if unknown and not any(buckets.values()):
        lines.append(
            f"liquidity not recorded for {unknown} markets, so no reliability buckets"
        )
        return lines
    for name, group in buckets.items():
        if not group:
            lines.append(f"{name}: n=0")
            continue
        mean_p = sum(p for p, _ in group) / len(group)
        frequency = sum(y for _, y in group) / len(group)
        lines.append(f"{name}: n={len(group)} mean_p={mean_p:.3f} frequency={frequency:.3f}")
    if unknown:
        lines.append(f"liquidity not recorded: n={unknown}")
    return lines
