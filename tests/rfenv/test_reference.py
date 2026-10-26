"""Step 10 of plans/sionna_rt/sionna_rt.md: the committed reference channel files.

Without Sionna: each file loads, still belongs to its specification, and drives the channel applier.
With Sionna (``sionna`` marker): tracing the specification again on this install reproduces it.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.rfenv._reference import REFERENCES, reference
from waveflow.rfenv.apply import ChannelApplier
from waveflow.rfenv.cache import describes
from waveflow.rfenv.sionna_backend import trace


@pytest.mark.parametrize("name", sorted(REFERENCES))
def test_a_reference_belongs_to_its_specification(name):
    ps = reference(name)  # refuses a file whose spec_hash differs
    assert ps.manifest["spec_hash"] == REFERENCES[name].hash()
    assert ps.manifest["generator"]["versions"]["sionna-rt"].startswith("2.")


def test_the_floor_wall_reference_has_its_two_paths():
    ps = reference("floor_wall_siso")
    v = ps.valid[0, 0]
    assert v.sum() == 2
    assert np.allclose(
        np.sort(ps.tau[0, 0][v]), [20 / 299_792_458.0, 28.2842712 / 299_792_458.0]
    )
    assert np.all(ps.doppler[0, 0][v] > 0)  # the receiver closes on both


def test_the_array_reference_has_four_channels_with_distinct_phases():
    ps = reference("empty_1x4")
    assert (ps.n_rx_ch, ps.n_tx_ch) == (4, 1)
    phases = np.angle(ps.a[:, 0][ps.valid[:, 0]])
    assert len(set(np.round(phases, 3))) == 4


@pytest.mark.parametrize("name", sorted(REFERENCES))
def test_a_reference_drives_the_applier_without_sionna(name):
    ps = reference(name)
    rng = np.random.default_rng(0)
    x = rng.standard_normal((ps.n_tx_ch, 512)) + 1j * rng.standard_normal(
        (ps.n_tx_ch, 512)
    )
    y = ChannelApplier(ps, 122.88e6).process(x, 0)
    assert y.shape == (ps.n_rx_ch, 512) and np.any(y)


@pytest.mark.sionna
@pytest.mark.parametrize("name", sorted(REFERENCES))
def test_tracing_again_reproduces_the_reference(name):
    """Bit for bit on the install (versions and variant) that traced it; within float32's 1e-3 on
    another."""
    pytest.importorskip("sionna.rt")
    ref = reference(name)
    new = trace(REFERENCES[name])
    same_variant = (
        ref.manifest["generator"]["variant"] == new.manifest["generator"]["variant"]
    )
    if describes(ref, REFERENCES[name]) and same_variant:
        for field in ("a", "tau", "doppler", "valid"):
            assert np.array_equal(getattr(new, field), getattr(ref, field)), field
    else:
        assert np.array_equal(new.valid, ref.valid)
        assert np.linalg.norm(new.a - ref.a) <= 1e-3 * np.linalg.norm(ref.a)
