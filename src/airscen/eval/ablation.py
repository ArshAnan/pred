"""With-and-without-markets comparison for the July 10 replay.

Assumption: the jet-fuel crack is unchanged from the company's own guide
(CRUDE_ONLY). A WTI level then maps to a gallon price by

    gallon = 3.15 + (WTI - Q2 mean WTI) / 42

The without-markets case uses the guide itself, 3.15 USD per gallon, and
does not use the prediction-market curve. Both errors are against the
September quarter's adjusted 3.61 USD per gallon.
"""

from __future__ import annotations

from airscen.eval.replay import GUIDE_GAL, REALIZED_GAL, load_replay, replay_lines

_BARREL = 42.0


def gallon_from_wti(wti: float, wti_ref: float, baseline_gal: float = GUIDE_GAL) -> float:
    return baseline_gal + (wti - wti_ref) / _BARREL


def _p50(lines: list[str]) -> float | None:
    for line in lines:
        if line.startswith("WTI P10"):
            parts = line.split()
            return float(parts[parts.index("P50") + 1])
    return None


def ablation_lines(doc: dict | None = None) -> list[str]:
    doc = doc or load_replay()
    lines = replay_lines(doc)
    p50 = _p50(lines)
    ref = float(doc["wti_ref_q2"]["mean"])
    assumption_error = abs(REALIZED_GAL - GUIDE_GAL)
    if p50 is None:
        return [
            "Assumption: crack unchanged, gallon = 3.15 + (WTI - Q2 mean WTI) / 42.",
            "market error: not available",
            f"assumption error {assumption_error:.4f} USD/gal",
        ]
    market_gal = gallon_from_wti(p50, ref)
    market_error = abs(REALIZED_GAL - market_gal)
    return [
        "Assumption: the crack spread is unchanged, so gallon = "
        f"{GUIDE_GAL:.2f} + (WTI - {ref:.2f}) / 42. "
        "The company guide is a single price with no market distribution.",
        f"market P50 {p50:.2f} USD/bbl implies {market_gal:.4f} USD/gal",
        f"market error {market_error:.4f} USD/gal",
        f"assumption error {assumption_error:.4f} USD/gal",
    ]
