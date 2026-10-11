"""Brief faithfulness: every number in a brief must appear in its run."""

from __future__ import annotations

from airscen.brief import check_numbers
from airscen.types import RunResult


def faithfulness_lines(pairs: list[tuple[str, RunResult]]) -> list[str]:
    """Pass rate of check_numbers over stored LLM briefs.

    An empty list is sample size 0: no brief with llm_status ready was available.
    """
    if not pairs:
        return ["faithfulness pass rate not available, sample size 0"]
    passed = sum(1 for md, result in pairs if check_numbers(md, result))
    rate = passed / len(pairs)
    return [f"faithfulness pass rate {rate:.2f}, sample size {len(pairs)}"]
