"""The B1 CG engine (``examples/cg_pool``, ``plans/dse_matrix_sched.md`` Section 7a, E1): M dedicated CG
workers behind a ``StreamDispatch``, merged by a ``StreamArbiter`` whose ``m_sel`` is the credit.

The golden: the factored fixed-point CG against the same CG in floating point; the stop rule.  pysim: the
worker alone (every scenario, the fatal ones included), the split alone, and the pool at 1, 2 and 4
workers -- every job answered once, ``X`` bit-exact, a status each, counts asserted.  At RTL (``-m xsi``):
the worker alone, the split alone, and the pool on a batch of mixed set sizes, X bit-exact against pysim
and the golden, cycles against pysim.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import examples.cg_pool.cg_golden as cgg
import examples.cg_pool.cg_pool as cp
from waveflow.hw.harness import harness
from waveflow.simulation.simulation import Simulation
from waveflow.utils import fixputils as fx


def _bursts(sysm, port):
    return [np.asarray(b, dtype=np.uint64).reshape(-1) for b in sysm.stand_ins[port].words]


def _assert_golden(sysm):
    for port, want in sysm.golden_outputs().items():
        got = _bursts(sysm, port)
        assert len(got) == len(want), port
        for g, w in zip(got, want):
            np.testing.assert_array_equal(g, np.asarray(w, dtype=np.uint64).reshape(-1))


def _pool(n, kind):
    sysm = harness(cp.CgPool(name="cg_pool", sim=Simulation(), n_workers=n), workload=kind)
    sysm.run()
    ans, st = cp.answers_by_tx(sysm.stand_ins["m_out"].words, sysm.stand_ins["m_status"].words)
    return sysm, ans, st


# --- the golden ------------------------------------------------------------------------------------
def test_the_formats_fit_64_bits():
    cgg.FORMATS.check()


def test_fixed_point_cg_is_close_to_float_cg():
    """The bit-exact worker against the same t iterations in complex128 on the same quantized inputs:
    below -60 dB on every synthetic job (-73 dB mean when measured)."""
    rng = np.random.default_rng(5)
    errs = [cgg.quant_error(cgg.make_problem(rng, i)) for i in range(20)]
    assert max(errs) < -60.0, errs


def test_cg_converges_toward_the_solve():
    """More iterations, closer to ``(I + H Hᴴ)⁻¹ B``: the engine computes what it says."""
    rng = np.random.default_rng(6)
    p = cgg.make_problem(rng, 0, users=2, snr_db=(-5.0, -5.0))
    H = fx.to_float(p.hr, cgg.FORMATS.H) + 1j * fx.to_float(p.hi, cgg.FORMATS.H)
    B = fx.to_float(p.br, cgg.FORMATS.cg.B) + 1j * fx.to_float(p.bi, cgg.FORMATS.cg.B)
    exact = np.linalg.solve(np.eye(cgg.N_RX) + H @ H.conj().T, B)
    err = []
    for t in (1, 2, 4, 8):
        p.t = t
        xr, xi, _ = cgg.fixed_solve(p)
        x = fx.to_float(xr, cgg.FORMATS.Xo) + 1j * fx.to_float(xi, cgg.FORMATS.Xo)
        err.append(np.linalg.norm(x - exact) / np.linalg.norm(exact))
    assert all(a > b for a, b in zip(err, err[1:])), err


def test_a_zero_column_stops_at_once():
    """The stop rule: a zero right-hand side has ``‖r‖² = 0``, so its column never iterates and its X
    is zero; the other columns are unchanged by it (columns are independent)."""
    rng = np.random.default_rng(7)
    p = cgg.make_problem(rng, 0, users=2, c=4)
    xr0, xi0, _ = cgg.fixed_solve(p)
    p.br = p.br.copy()
    p.bi = p.bi.copy()
    p.br[:, 1] = 0
    p.bi[:, 1] = 0
    xr, xi, ran = cgg.fixed_solve(p)
    assert ran[1] == 0 and ran[0] == p.t
    assert not xr[:, 1].any() and not xi[:, 1].any()
    np.testing.assert_array_equal(xr[:, [0, 2, 3]], xr0[:, [0, 2, 3]])


def test_the_job_words_round_trip():
    rng = np.random.default_rng(8)
    p = cgg.make_problem(rng, 0, users=3)
    hr, hi = cp.unpack(cp.pack(p.hr, p.hi, cp.E_H), cp.E_H, p.m)
    np.testing.assert_array_equal(hr, p.hr)
    np.testing.assert_array_equal(hi, p.hi)
    assert len(cp.job_bursts(cp.JobSpec(p))[1].words) == cp.payload_words(p.m, p.c)


# --- pysim: each module alone ----------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["test", "bad_args", "bad_len"])
def test_the_worker_alone_matches_the_golden(kind):
    """Every frame bit-exact and counted; on the fatal scenarios the bad job is the last answered."""
    sysm = harness(cp.CgWorker, workload=kind)
    sysm.run()
    _assert_golden(sysm)
    js = cp.jobs(kind)
    n_answered = len(js) if kind == "test" else 3
    assert len(_bursts(sysm, "m_out")) == 3 * n_answered
    assert sysm.dut().jobs_done == (len(js) if kind == "test" else 2)
    assert sysm.dut().halted == (kind != "test")


def test_the_split_alone_matches_the_golden():
    sysm = harness(cp.CgSplit, workload="test")
    sysm.run()
    _assert_golden(sysm)
    assert len(_bursts(sysm, "m_status")) == len(cp.jobs("test"))


# --- pysim: the pool --------------------------------------------------------------------------------
@pytest.mark.parametrize("n", [1, 2, 4])
@pytest.mark.parametrize("kind", ["test", "mixed"])
def test_the_pool_answers_every_job_once_bit_exact(n, kind):
    """Every job answered once, its X bit-exact against the golden, one OK status each; at more than one
    worker every worker took jobs (first-free, one credit, jobs far longer than a dispatch)."""
    sysm, ans, st = _pool(n, kind)
    exp = sysm.dut().expected_answers(kind)
    assert sorted(ans) == sorted(exp) == sorted(st)
    assert all(v == cp.StatusCode.OK for v in st.values())
    for tx, (bursts, _status) in exp.items():
        for got, want in zip(ans[tx], bursts):
            np.testing.assert_array_equal(got.reshape(-1), np.asarray(want).reshape(-1))
    if n > 1:
        dsp = sysm.dut().dsp
        assert sum(dsp.sent) == len(exp) and min(dsp.sent) > 0 and dsp.bad_credits == 0


def test_more_workers_finish_sooner():
    """The pool's point: the mixed batch's last status leaves earlier at 2 workers than 1, and at 4 than 2."""
    ends = []
    for n in (1, 2, 4):
        sysm, _ans, _st = _pool(n, "mixed")
        ends.append(sysm.stand_ins["m_status"].stream_ep.last_stamp.delivered)
    assert ends[0] > 1.8 * ends[1] and ends[1] > 1.4 * ends[2], ends     # 12 jobs: 1.9x, then 1.7x


@pytest.mark.parametrize("n", [2, 4])
@pytest.mark.parametrize("kind", ["bad_args", "bad_len"])
def test_an_error_stops_its_worker_and_the_run_still_returns(n, kind):
    """Fatal in a pool: the bad job is answered (Resp n = 0, the error status), its worker stops, and the
    engine answers at most the jobs already on other workers -- a job sent to the stopped worker never is.
    The run returns either way: only the counts and the codes say what happened."""
    sysm, ans, st = _pool(n, kind)
    bad = cp.jobs(kind)[2]
    assert st[bad.p.tx_id] == bad.code != cp.StatusCode.OK
    assert int(cp.read_msg(cp.Resp, ans[bad.p.tx_id][0]).n) == 0
    exp = sysm.dut().expected_answers(kind)
    for tx in ans:
        if tx != bad.p.tx_id:
            assert st[tx] == cp.StatusCode.OK
            for got, want in zip(ans[tx], exp[tx][0]):
                np.testing.assert_array_equal(got.reshape(-1), np.asarray(want).reshape(-1))
    assert sum(w.halted for w in sysm.dut().workers) == 1


#: The cycle each job's Status leaves the worker alone at RTL, the ``test`` scenario (xc7z020 at 100 MHz,
#: Vitis / xsim 2025.1; the worker's XSI gate below).
WORKER_RTL_STATUS = [4742, 16010, 16665, 19475, 24215, 28112]


def test_worker_pysim_cycles_are_the_rtls():
    """The worker's timing model (``CYC``, its loops' csynth depths plus a fit to the traced RTL): every
    job's Status within 2 cycles of the RTL's -- pysim is a cycle early on each, alike, so the error does
    not grow job to job.  No vendor tools: the RTL cycles are the XSI gate's, pinned above."""
    sysm = harness(cp.CgWorker, workload="test")
    sink = sysm.stand_ins["m_out"]
    times = []
    orig = sink.rx_proc

    def rx(words):
        times.append(sink.env.now)
        yield from orig(words)

    sink.stream_ep.rx_proc = rx
    sysm.run()
    per = sysm.dut().clk.period
    got = [round(t / per) - 1 for t in times[2::3]]
    assert len(got) == len(WORKER_RTL_STATUS)
    assert all(abs(g - r) <= 2 for g, r in zip(got, WORKER_RTL_STATUS)), list(zip(got, WORKER_RTL_STATUS))


@pytest.mark.vitis
@pytest.mark.parametrize("kind", ["test", "bad_args", "bad_len", "mixed"])
def test_the_worker_body_is_bit_exact_under_csim(tmp_path, kind):
    """The HLS body (``src/cg_worker_task.h``) under Vitis C simulation on the scenario pysim plays: every
    output burst equal to the golden's, word for word and TLAST for TLAST -- the fatal ones included."""
    from examples.cg_pool import csim
    from waveflow.toolchain import toolchain

    if not toolchain.find_vitis_path():
        pytest.skip("Vitis not found")
    got, want, log = csim.run(tmp_path, kind)
    assert "CSim done with 0 errors" in log, log[-3000:]
    assert not csim.compare(got, want)


def test_the_pool_refuses_a_bad_size():
    with pytest.raises(ValueError):
        cp.CgPool(name="p", sim=Simulation(), n_workers=17)


# --- at RTL ------------------------------------------------------------------------------------------
#: The gates' build (csynth fails silently past the Windows path limit: keep it short).
RTL_ROOT = Path(__file__).resolve().parents[2] / "work" / "cg_pool"


def _trace_bursts(trace_dir: Path) -> list[np.ndarray]:
    """A recording's bursts, 128-bit words as (lo, hi) chunk pairs."""
    words = np.fromfile(trace_dir / "words.bin", dtype="<u8").reshape(-1, 2)
    bounds = np.fromfile(trace_dir / "bounds.bin", dtype="<u8")
    starts = [0, *bounds[:-1]]
    return [words[int(s):int(e)] for s, e in zip(starts, bounds)]


def _rtl_run(prefix: str):
    """Build (csynth if stale) and run one system of the DAG through RTL and pysim; its work directory."""
    from examples.cg_pool import cg_pool_build as cb
    from waveflow.build.build import BuildConfig
    from waveflow.toolchain.toolchain import find_vitis_path, find_vivado_path

    if not (find_vivado_path() and find_vitis_path()):
        pytest.skip("XSI gate prerequisite missing: Vitis HLS + Vivado")
    import os
    os.environ.setdefault("WAVEFLOW_CG_ROOT", str(RTL_ROOT))
    root = cb.build_root()
    dag = cb.build_dag(work_dir=root / "xw")
    cfg = BuildConfig(root_dir=root, params={"synth": "build"})
    res = dag.run(cfg, through=f"{prefix}system_xsi")
    res.update(dag.run(cfg, through=f"{prefix}pysim"))
    bad = {k: r.message for k, r in res.items() if not r.success}
    assert not bad, bad
    top = {"worker_": cb.WORKER_TOP, "split_": cb.SPLIT_TOP}.get(prefix) or cb.pool_top(int(prefix[4:-1]))
    return root / "xw" / f"{prefix}{top}"


@pytest.mark.xsi
@pytest.mark.parametrize("prefix,kind,outs", [("worker_", "test", ["m_out"]),
                                              ("split_", "test", ["m_out", "m_status"])],
                         ids=["worker", "split"])
def test_rtl_module_alone_equals_pysim_and_the_golden(prefix, kind, outs):
    """The worker alone and the split alone at RTL: every output burst equal to pysim's and to the
    golden's, word for word and TLAST for TLAST (their order is the jobs': no timing in it)."""
    work = _rtl_run(prefix)
    cls = cp.CgWorker if prefix == "worker_" else cp.CgSplit
    golden = cls(name="g", sim=Simulation()).golden(kind)
    for port in outs:
        rtl = _trace_bursts(work / "traces" / port)
        sim = _trace_bursts(work / "pysim_traces" / port)
        want = [np.asarray(w, dtype=np.uint64).reshape(-1, 2) for w in golden[port]]
        assert len(rtl) == len(sim) == len(want), port
        for r, s, w in zip(rtl, sim, want):
            np.testing.assert_array_equal(r, w)
            np.testing.assert_array_equal(s, w)


@pytest.mark.xsi
@pytest.mark.parametrize("n", [2, 4])
def test_rtl_pool_answers_every_job_bit_exact(n):
    """THE gate (E1): the pool at RTL on the mixed batch -- set sizes 1-4 -- every job answered once, its
    tx_id's X bit-exact against the golden and pysim, one OK status each.  The order out is timing's,
    so the answers are compared by tx_id, not in order."""
    work = _rtl_run(f"pool{n}_")
    exp = cp.CgPool(name="p", sim=Simulation(), n_workers=n).expected_answers("mixed")
    for where in ("traces", "pysim_traces"):
        ans, st = cp.answers_by_tx(_trace_bursts(work / where / "m_out"),
                                   _trace_bursts(work / where / "m_status"))
        assert sorted(ans) == sorted(st) == sorted(exp), where
        assert all(v == cp.StatusCode.OK for v in st.values()), where
        for tx, (bursts, _s) in exp.items():
            for got, want in zip(ans[tx], bursts):
                np.testing.assert_array_equal(got.reshape(-1, 2), np.asarray(want).reshape(-1, 2))
