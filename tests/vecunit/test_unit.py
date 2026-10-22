"""The vector unit in pysim (Stage 4 of ``plans/vecunit_new.md``): in its harness -- a driver per
program and input stream, a sink per output and on ``m_status`` -- every output burst equals the
interpreter's, on random jobs over every instruction form, for two configurations; an error is fatal."""

from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.harness import harness
from waveflow.linalg.message import Status
from waveflow.simulation.simulation import Simulation
from waveflow.utils.burst_io import StreamBurst
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit import isa as I
from waveflow.vecunit.asm import job_words
from waveflow.vecunit.config import Cap, VecUnitConfig
from waveflow.vecunit.unit import VecUnit
from waveflow.vecunit.workload import golden_bursts, job_bursts, random_jobs


def rand_fmt(rng, wmin=8, wmax=14) -> Format:
    W = int(rng.integers(wmin, wmax + 1))
    return Format(W, int(rng.integers(1, W)), True,
                  QMode.AP_RND if rng.integers(2) else QMode.AP_TRN,
                  OMode.AP_SAT if rng.integers(2) else OMode.AP_WRAP)


def centre() -> VecUnitConfig:
    rng = np.random.default_rng(0)
    return VecUnitConfig(group_formats=tuple(rand_fmt(rng) for _ in range(8)),
                         scalar_formats=tuple(rand_fmt(rng, 10, 20) for _ in range(6)),
                         in_queue_formats=(rand_fmt(rng), rand_fmt(rng)),
                         out_queue_formats=(rand_fmt(rng), rand_fmt(rng)),
                         L=4, VLMAX=8, Kmax=8, Nmax=9, g_div=3)


def two_ports() -> VecUnitConfig:
    """Two input and two output ports, two lanes, two slots (the second without a multiplier),
    32-bit words."""
    rng = np.random.default_rng(1)
    return VecUnitConfig(group_formats=tuple(rand_fmt(rng) for _ in range(6)),
                         scalar_formats=tuple(rand_fmt(rng, 10, 20) for _ in range(4)),
                         in_queue_formats=(rand_fmt(rng), rand_fmt(rng), rand_fmt(rng)),
                         out_queue_formats=(rand_fmt(rng), rand_fmt(rng)),
                         L=2, VLMAX=6, Kmax=6, Nmax=5, S=2, NB=2, g_div=2,
                         slot_caps=(Cap.ALL, Cap.MOVE | Cap.DOT | Cap.LOAD | Cap.STORE),
                         n_in_ports=2, n_out_ports=2, word_bits=32)


def run(cfg: VecUnitConfig, workload, tmp_path):
    unit = VecUnit(name="vecunit", sim=Simulation(), cfg=cfg)
    sysm = harness(unit, workload=workload, n_cycles=10**6)
    sysm.write_scenario(tmp_path)
    sysm.run()
    return unit, sysm


def assert_outputs(sysm, gold: dict):
    for port, bursts in gold.items():
        got = sysm.stand_ins[port].words
        assert len(got) == len(bursts), port
        for a, b in zip(got, bursts):
            assert np.array_equal(np.asarray(a, np.uint64).reshape(-1),
                                  np.asarray(b, np.uint64).reshape(-1)), port


@pytest.mark.parametrize("make", [centre, two_ports])
def test_pysim_equals_the_interpreter(make, tmp_path):
    cfg = make()
    unit, sysm = run(cfg, "test", tmp_path)
    gold = unit.golden("test")
    assert len(gold["m_status"]) == 4
    assert_outputs(sysm, gold)


def test_the_two_port_jobs_use_both_ports():
    cfg = two_ports()
    jobs = random_jobs(cfg, 4, seed=1)
    loads = {int(x.rs1) for _, p, _ in jobs for x in p.load}
    stores = {int(x.rd) for _, p, _ in jobs for x in p.store}
    assert loads == {0, 1} and stores == {0, 1}


def test_an_error_is_fatal(tmp_path):
    """A job whose execute program names a register the unit does not have: its status is
    ``BAD_DIMS``, what it pushed before is stored, and nothing after it is answered."""
    cfg = centre()
    good = random_jobs(cfg, 3, seed=5)
    tx, progs, inputs = good[1]
    bad_exec = progs.execute[:6] + [I.vmv_v_v(cfg.NG, 0)] + progs.execute[6:]
    bad = (tx, type(progs)(progs.load, bad_exec, progs.store), inputs)
    jobs = [good[0], bad, good[2]]
    stim = job_bursts(cfg, [good[0], good[2]])
    ok = job_bursts(cfg, [good[1]])  # its inputs and load and store programs, well formed
    ok["s_xprog"] = [StreamBurst(np.asarray(job_words(tx, bad_exec), np.uint64), True)]
    for port in stim:  # job 0, the bad job, job 2, in that order on every stream
        stim[port] = stim[port][:len(stim[port]) // 2] + ok[port] + stim[port][len(stim[port]) // 2:]
    _unit, sysm = run(cfg, stim, tmp_path)
    gold = golden_bursts(cfg, jobs)
    assert [int(w[0]) >> 16 & 0xFF for w in gold["m_status"]] == [Status.OK, Status.BAD_DIMS]
    assert_outputs(sysm, gold)
