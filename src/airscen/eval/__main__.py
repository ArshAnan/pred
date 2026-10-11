"""Print the five evaluation checks and the limits that go with them."""

from __future__ import annotations

from pathlib import Path

import yaml

from airscen.eval.ablation import ablation_lines
from airscen.eval.calibration import reliability_lines
from airscen.eval.checks import number_lines
from airscen.eval.faithfulness import faithfulness_lines
from airscen.eval.replay import replay_lines

_LIMITS = (
    "Limits:",
    "- One replay is a case study, not a statistical result.",
    "- No claim of predictive edge.",
    "- The brief is judged on faithfulness to its inputs, not on foresight.",
)


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def report() -> str:
    overrides_path = _root() / "overrides.yaml"
    overrides = yaml.safe_load(overrides_path.read_text()) or {}
    if not isinstance(overrides, dict):
        overrides = {}
    sections = [
        "Number correctness",
        *number_lines(overrides),
        "",
        "Delta replay",
        *replay_lines(),
        "",
        "With and without markets",
        *ablation_lines(),
        "",
        "Brief faithfulness",
        *faithfulness_lines([]),
        "",
        "Market calibration",
        *reliability_lines([]),
        "",
        *_LIMITS,
    ]
    return "\n".join(sections) + "\n"


def main() -> None:
    print(report(), end="")


if __name__ == "__main__":
    main()
