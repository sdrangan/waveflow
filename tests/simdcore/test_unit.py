"""The SIMD core in pysim (``plans/simdcore.md`` Stage 4): in its harness -- a driver on ``s_prog`` and
each input port, a sink on each output and on ``m_status`` -- every output burst equals the
interpreter's, for the matched filter and CG; an error is fatal; the job's time follows the cycle model."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from examples.simdcore_cg.cg_program import cg_config, cg_job, problem
from examples.simdcore_mf.mf_program import frame, mf_config, mf_inputs, mf_text
from waveflow.hw.harness import harness
from waveflow.simdcore.unit import Job, SimdCore, Status, golden_bursts, job_bursts, job_words, parse_job
from waveflow.simulation.simulation import Simulation


@dataclass
class MfCore(SimdCore):
    def jobs(self, kind: str = "test") -> list[Job]:
        out = []
        for j, (nsamp, nrx) in enumerate([(128, self.cfg.window("R").n), (64, self.cfg.window("R").n),
                                          (100, self.cfg.window("R").n)]):
            xr, xi, rr, ri = frame(nsamp, nrx, seed=j)
            out.append(Job(200 + j, mf_text(nrx), args=(nsamp,), inputs=mf_inputs(self.cfg, xr, xi, rr, ri)))
        return out


@dataclass
class CgCore(SimdCore):
    def jobs(self, kind: str = "test") -> list[Job]:
        return [cg_job(self.cfg, *problem(8, 32, seed=s), 3, tx_id=300 + s) for s in range(2)]


def _run(core, workload, tmp_path):
    sysm = harness(core, workload=workload, n_cycles=10**6)
    sysm.write_scenario(tmp_path)
    sysm.run()
    return sysm


def _assert_outputs(sysm, gold):
    for port, bursts in gold.items():
        got = sysm.stand_ins[port].words
        assert len(got) == len(bursts), (port, len(got), len(bursts))
        for a, b in zip(got, bursts):
            assert np.array_equal(np.asarray(a, np.uint64).reshape(-1), np.asarray(b, np.uint64).reshape(-1)), port


def test_job_words_roundtrip():
    w = job_words(7, [1, 2, 3], args=(5, -1), budget=99)
    assert parse_job(w) == (7, [1, 2, 3], [5, 0xFFFFFFFF], 99)


@pytest.mark.parametrize("nrx,L", [(8, 2), (3, 4)])
def test_mf_pysim_equals_the_interpreter(nrx, L, tmp_path):
    core = MfCore(name="simdcore", sim=Simulation(), cfg=mf_config(nrx, L=L))
    sysm = _run(core, "test", tmp_path)
    gold = core.golden("test")
    assert len(gold["m_status"]) == 3
    _assert_outputs(sysm, gold)


def test_cg_pysim_equals_the_interpreter(tmp_path):
    core = CgCore(name="simdcore", sim=Simulation(), cfg=cg_config(8, 32, L=2))
    sysm = _run(core, "test", tmp_path)
    _assert_outputs(sysm, core.golden("test"))


def _exec(sysm):
    from waveflow.simdcore.unit import SimdExec

    objs = sysm.sim._sim_objs
    return next(o for o in (objs.values() if isinstance(objs, dict) else objs) if isinstance(o, SimdExec))


@dataclass
class MfSteady(SimdCore):
    def jobs(self, kind: str = "test") -> list[Job]:
        return [Job(j, mf_text(8), args=(256,), inputs=mf_inputs(self.cfg, *frame(256, 8, seed=j)))
                for j in range(5)]


def test_mf_steady_interval_in_pysim(tmp_path):
    """Back-to-back frames (256 x 8): one lane is compute-bound (2,048 multiplies a frame), two sit at
    the input floor (1,152 words) -- the loader runs ahead into the next job."""
    out = {}
    for L in (1, 2):
        core = MfSteady(name="simdcore", sim=Simulation(), cfg=mf_config(8, L=L))
        sysm = _run(core, "test", tmp_path / f"L{L}")
        ts = np.asarray(_exec(sysm).status_times) / core.clk.period
        out[L] = float(np.median(np.diff(ts)))
    assert 2048 <= out[1] <= 2048 * 1.05
    assert 1152 <= out[2] <= 1152 * 1.05


def test_an_error_is_fatal(tmp_path):
    """The second job reads past data memory: its status is ADDR, what it pushed before is stored, and
    the third job is not answered."""
    cfg = mf_config(2, L=2)
    xr, xi, rr, ri = frame(64, 2, seed=1)
    good = Job(1, mf_text(2), args=(64,), inputs=mf_inputs(cfg, xr, xi, rr, ri))
    bad_text = mf_text(2).replace("    ecall\n", "    li t1, 0x7FFFFFF0\n    vle32.v v3, (t1)\n    ecall\n")
    bad = Job(2, bad_text, args=(64,), inputs=mf_inputs(cfg, xr, xi, rr, ri))
    jobs = [good, bad, Job(3, mf_text(2), args=(64,), inputs=mf_inputs(cfg, xr, xi, rr, ri))]
    gold = golden_bursts(cfg, jobs)
    assert [(int(w[0]) >> 16) & 0xFF for w in gold["m_status"]] == [Status.OK, Status.ADDR]
    sysm = _run(MfCore(name="simdcore", sim=Simulation(), cfg=cfg), job_bursts(cfg, jobs), tmp_path)
    _assert_outputs(sysm, gold)
