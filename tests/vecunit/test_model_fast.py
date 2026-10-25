"""Stage 10 of ``plans/vecunit_new.md``: the simulation got faster, and nothing it computes changed.

* The interpreter's fixed-point kernels (``model._quantize``, ``_add``, ``_sub``, ``_mul``, ``_div``)
  against :mod:`waveflow.utils.fixputils`, bit for bit, on random signed formats -- both rounding
  modes, both overflow modes, the width guard's refusals.
* The execute task's deferred waits (``VecExec.defer``): every output burst's words and arrival time
  the same as with a wait after every item.
* The caches -- a program's checks (``run_job(checks=...)``), its assembly (``assemble(cache=...)``),
  the CG unit's closed-loop results kept as its golden -- the same as without them.
"""

from __future__ import annotations

import numpy as np
import pytest

from waveflow.simulation.simulation import Simulation
from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit import model as M

QM, OM = (QMode.AP_RND, QMode.AP_TRN), (OMode.AP_SAT, OMode.AP_WRAP)


def _fmt(rng, wmax: int = 40) -> Format:
    W = int(rng.integers(2, wmax))
    return Format(W, int(rng.integers(1, W + 1)), True, QM[rng.integers(2)], OM[rng.integers(2)])


def _vals(rng, f: Format, shape=(4, 8)) -> np.ndarray:
    return rng.integers(-(1 << (f.W - 1)), 1 << (f.W - 1), shape)


def _same(ref, fast, *args) -> bool:
    """``fast(*args)`` equals ``ref(*args)``, or both refuse (the width guard)."""
    try:
        r = ref(*args)
    except NotImplementedError:
        try:
            fast(*args)
        except NotImplementedError:
            return True
        return False
    f = fast(*args)
    if isinstance(r, tuple):
        return np.array_equal(np.asarray(r[0], np.int64), f[0]) and r[1] == f[1]
    return np.array_equal(np.asarray(r, np.int64), f)


@pytest.mark.parametrize("seed", range(4))
def test_fast_kernels_equal_fixputils(seed):
    rng = np.random.default_rng(seed)
    for _ in range(800):
        a, b, d = _fmt(rng), _fmt(rng), _fmt(rng)
        x, y = _vals(rng, a), _vals(rng, b)
        assert _same(fx.quantize, M._quantize, x, a, d)
        for ref, fast in ((fx.add, M._add), (fx.sub, M._sub), (fx.mult, M._mul)):
            assert _same(ref, fast, x, a, y, b)
            try:
                r, fr = ref(x, a, y, b)
            except NotImplementedError:
                continue
            assert _same(fx.quantize, M._quantize, np.asarray(r), fr, d)


@pytest.mark.parametrize("seed", range(4))
def test_fast_divide_equals_fixputils(seed):
    """The guarded divide, with zero divisors and the dividend widened as ``wf.sdivz`` does."""
    rng = np.random.default_rng(100 + seed)
    for _ in range(500):
        a, b = _fmt(rng, 28), _fmt(rng, 28)
        x, y = _vals(rng, a, (4, 1)), _vals(rng, b, (4,))
        y[rng.integers(4)] = 0
        assert _same(fx.div, M._div, x, a, y, b)
    lo = Format(8, 4, True)  # the one overflowing case: the most negative over -1 LSB
    assert _same(fx.div, M._div, np.array([-128]), lo, np.array([-1]), lo)


def test_the_identity_table_stays_bounded():
    a = Format(12, 4, True)
    for _ in range(5000):
        M._quantize(np.zeros(3, np.int64), Format(12, 4, True), a)  # a fresh source each time
    assert len(M._BY_ID) <= 4097


def _outcome(unit, kind):
    """Every output burst of *unit*'s harness on *kind*: ``(cycle, words)`` per port."""
    from waveflow.hw.harness import harness

    s = harness(unit, workload=kind, n_cycles=10**8)
    rec: dict = {}
    for port in ("m_status", *(f"s_out{p}" for p in range(unit.cfg.n_out_ports))):
        sink = s.stand_ins[port]
        rx = sink.stream_ep.rx_proc

        def timed(words, rx=rx, port=port, sink=sink):
            rec.setdefault(port, []).append(
                (sink.env.now, np.asarray(words, np.uint64).reshape(-1).tolist()))
            yield from rx(words)

        sink.stream_ep.rx_proc = timed
    s.run()
    return rec


@pytest.mark.parametrize("which, kind", [("cg", "test"), ("cg", "calib"), ("mf", "test"),
                                         ("mf", "steady"), ("gen", "test")])
def test_deferred_waits_change_no_time(which, kind, monkeypatch):
    from examples.vecunit_cg.cg_unit import CgVecUnit
    from examples.vecunit_mf.mf_unit import MfVecUnit
    from tests.vecunit.test_unit import rtl_config
    from waveflow.vecunit.unit import VecExec, VecUnit

    def make():
        if which == "gen":
            return VecUnit(name="vecunit", sim=Simulation(), cfg=rtl_config())
        return {"cg": CgVecUnit, "mf": MfVecUnit}[which](name="vecunit", sim=Simulation())

    deferred = _outcome(make(), kind)
    monkeypatch.setattr(VecExec, "defer", False)
    assert _outcome(make(), kind) == deferred


def test_cached_checks_and_assembly_change_nothing():
    from examples.vecunit_cg.harness import unit
    from waveflow.vecunit.asm import assemble

    u = unit()
    jobs = u.jobs("steady")
    checks, programs = {}, {}
    for tx, progs, inputs in jobs:
        plain = M.run_job(u.cfg, *progs, inputs, tx_id=tx, mode="serial")
        cached = M.run_job(u.cfg, *progs, inputs, tx_id=tx, mode="serial", checks=checks)
        assert plain.status == cached.status and plain.executed == cached.executed
        for (r1, a1, b1), (r2, a2, b2) in zip(plain.outputs[0], cached.outputs[0]):
            assert r1 == r2 and np.array_equal(a1, a2) and np.array_equal(b1, b2)
        w1, w2 = assemble(u.cfg, progs, tx), assemble(u.cfg, progs, tx, programs)
        assert all(np.array_equal(w1[k], w2[k]) for k in w1)
    assert len(checks) == 1 and len(programs) == 1  # four solves, one program


def test_the_kept_closed_loop_results_are_the_golden():
    """CG's jobs are built by running each solve closed-loop in the interpreter; its golden reuses
    those results -- the same bursts as running the interpreter again on the static stimulus."""
    from examples.vecunit_cg.harness import unit
    from waveflow.vecunit.workload import golden_bursts

    u = unit()
    kept = u.golden("test")
    again = golden_bursts(u.cfg, u.jobs("test"))
    assert kept.keys() == again.keys()
    for port in kept:
        assert len(kept[port]) == len(again[port])
        assert all(np.array_equal(a, b) for a, b in zip(kept[port], again[port]))
