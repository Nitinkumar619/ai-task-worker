"""End-to-end evals (ground-truth assertions against the ERP database) for the offline brain."""
import pytest

from worker.scenarios import SCENARIOS, run_scenario


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scenario(sc):
    r = run_scenario(sc, "offline")
    assert r.passed, r.errors
