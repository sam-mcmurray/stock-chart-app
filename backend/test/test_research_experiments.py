from dataclasses import FrozenInstanceError

import pytest

from services.research_experiments import EXPERIMENT_SPECS, WATCH_PRIORITY_EXPERIMENT


def test_watch_priority_experiment_is_fixed_and_research_only():
    spec = EXPERIMENT_SPECS[WATCH_PRIORITY_EXPERIMENT]

    assert spec.registered_at_iso == "2026-09-18T18:28:20Z"
    assert spec.primary_metric == "mean_mfe_pct"
    assert spec.horizon_bars == 60
    assert spec.minimum_post_registration_live_received_sample == 500
    assert spec.status == "preregistered_research_only"
    with pytest.raises(FrozenInstanceError):
        spec.primary_metric = "mean_return_pct"
