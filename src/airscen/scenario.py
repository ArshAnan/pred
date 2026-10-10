"""Scenario grid and summary. CP0 stub; behavior lands in B4.

Pure functions. Import only airscen.types, the standard library, and numpy.
No network, database, or clock reads.
"""

from airscen.types import ScenarioInputs, ScenarioRow, ScenarioSummary


def run_scenarios(inputs: ScenarioInputs) -> tuple[list[ScenarioRow], ScenarioSummary]:
    """Build the six-row grid.

    Raises DataError with code E_MISSING_FACT when operating income, net
    interest, gallons, debt, or the variable-rate share is missing.
    """
    raise NotImplementedError
