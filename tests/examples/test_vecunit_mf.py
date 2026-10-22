"""The matched filter as a program (Stage 3a of ``plans/vecunit_new.md``): on the interpreter it equals
``mf_golden`` bit for bit, with ``nrx`` below, at and above ``L`` (one and several column groups, ``L``
not dividing ``nrx``) and frames that are and are not a multiple of the chunk."""

from __future__ import annotations

import numpy as np
import pytest

from examples.vecunit_mf.mf_golden import mf_golden
from examples.vecunit_mf.mf_program import (
    instructions_per_chunk,
    mf_config,
    mf_programs,
    run_mf,
)
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit.asm import assemble, check

FX = Format(16, 2, True, QMode.AP_RND, OMode.AP_SAT)
FR = Format(16, 3, True, QMode.AP_RND, OMode.AP_SAT)
FY = Format(16, 3, True, QMode.AP_TRN, OMode.AP_SAT)


@pytest.mark.parametrize("mode", ["pass", "serial"])
@pytest.mark.parametrize("nrx", [3, 4, 7, 8])
@pytest.mark.parametrize("nsamp", [32, 37, 5])
def test_matched_filter_is_bit_exact(nrx, nsamp, mode):
    cfg = mf_config(FX, FR, FY, L=4, VLMAX=16, nrx_max=8)
    rng = np.random.default_rng(nrx * 100 + nsamp)
    lo, hi = -(1 << 15), 1 << 15
    xr, xi = rng.integers(lo, hi, nsamp), rng.integers(lo, hi, nsamp)
    rr, ri = rng.integers(lo, hi, (nsamp, nrx)), rng.integers(lo, hi, (nsamp, nrx))
    assert check(cfg, mf_programs(cfg, nsamp, nrx)) == []
    yr, yi, r = run_mf(cfg, xr, xi, rr, ri, tx_id=4, mode=mode)
    gr, gi = mf_golden(xr, xi, FX, rr, ri, FR, FY)
    assert r.status == 0
    assert np.array_equal(yr, gr) and np.array_equal(yi, gi)
    assert [o[0]["tag"] for o in r.outputs[0]] == list(range(-(-nsamp // 16)))


def test_saturation_is_exercised():
    """Full-scale inputs: the products saturate Y somewhere, and still match."""
    cfg = mf_config(FX, FR, FY, L=4, VLMAX=16, nrx_max=8)
    n = 16
    xr = np.full(n, (1 << 15) - 1)
    xi = np.full(n, (1 << 15) - 1)
    rr = np.full((n, 5), (1 << 15) - 1)
    ri = np.full((n, 5), (1 << 15) - 1)
    yr, yi, _ = run_mf(cfg, xr, xi, rr, ri)
    gr, gi = mf_golden(xr, xi, FX, rr, ri, FR, FY)
    assert np.array_equal(yr, gr) and np.array_equal(yi, gi)
    assert (yr == (1 << 15) - 1).all()  # Re = 2 xr rr saturates Y


def test_instruction_count_and_assembly():
    cfg = mf_config(FX, FR, FY, L=4, VLMAX=64, nrx_max=16)
    assert instructions_per_chunk(cfg, 4) == 8 and instructions_per_chunk(cfg, 16) == 17
    p = mf_programs(cfg, 256, 16)
    assert len(p.execute) == 4 * 17 and len(p.load) == 8 and len(p.store) == 4
    words = assemble(cfg, p, tx_id=1)
    assert len(words["execute"]) == 1 + 4 * 17


@pytest.mark.parametrize("kind", ["test", "steady"])
def test_pysim_unit_equals_the_interpreter(kind, tmp_path):
    """The matched-filter-configured unit in its harness (Stage 4): every frame's outputs and
    statuses equal to the interpreter's (and so to ``mf_golden``)."""
    from examples.vecunit_mf.mf_unit import MfVecUnit
    from waveflow.hw.harness import harness
    from waveflow.simulation.simulation import Simulation

    unit = MfVecUnit(name="vecunit", sim=Simulation(), VLMAX=16)
    sysm = harness(unit, workload=kind, n_cycles=10**6)
    sysm.write_scenario(tmp_path)
    sysm.run()
    gold = unit.golden(kind)
    assert len(gold["m_status"]) == 4
    for port, bursts in gold.items():
        got = sysm.stand_ins[port].words
        assert len(got) == len(bursts)
        assert all(np.array_equal(np.asarray(a, np.uint64).reshape(-1), np.asarray(b, np.uint64))
                   for a, b in zip(got, bursts))
