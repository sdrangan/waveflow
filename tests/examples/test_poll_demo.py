"""Test that the poll_demo harness runs and self-checks each polling-cost scenario."""
from __future__ import annotations

from examples.interface.poll_demo import (
    run_and_check,
    scenario_bandwidth,
    scenario_discovery,
    scenario_saturation,
)


def test_discovery_is_deterministic_mean():
    # poll_interval=8 ⇒ discovery delay is exactly the mean (8-1)/2 = 3.5 cycles.
    discovery = scenario_discovery()
    assert abs(discovery - 3.5) < 1e-9


def test_active_poller_stretches_the_burst():
    base, derated = scenario_bandwidth()
    # An ov=0.25 poller stretches the per-word term by 1/(1-ov)=1.333x; init and
    # access latency are not derated, so the total grows but by less than 1.333x.
    assert derated > base
    assert derated < base * (1.0 / (1.0 - 0.25))


def test_one_cycle_poll_warns_and_clamps():
    # A 1-cycle poll is ov=1; the model must flag it (clamp-and-warn), not diverge.
    scenario_saturation()


def test_run_and_check_entry_point():
    run_and_check()
