"""The stream blocks' decision cycles in pysim (``waveflow/streams/timing.py``, ``plans/stream_dispatch.md``
Stage 7): opt-in by naming a calibrated platform, the platform's lookup reproduced by pysim at every
measured point, refused off it, and no effect on a module's identity.  At RTL (``-m xsi``): one point a
block re-measured at the platform's target against its committed table, and pysim against both."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from waveflow.calib.fixtures import stream_blocks as sb
from waveflow.calib.platform import packaged_platforms_dir
from waveflow.hw.clock import Clock
from waveflow.hw.harness import harness
from waveflow.simulation.simulation import Simulation
from waveflow.streams import StreamArbiter, StreamDispatch
from waveflow.streams import timing as st
from waveflow.streams.rtl import DispatchPool, sorted_frames
from waveflow.toolchain.toolchain import find_vitis_path, find_vivado_path

PLATFORM = packaged_platforms_dir() / st.PLATFORM
CLK = Clock(freq=250e6)
ROOT = Path(__file__).resolve().parents[2] / "work" / "stream_timing_gate"


def _corpus(block: str) -> pd.DataFrame:
    return pd.read_csv(st.model(block, PLATFORM).calib_dir / "corpus.csv")


def test_every_point_of_the_sweep_is_calibrated():
    """Both blocks at 2, 4, 8 and 16 ports under both policies -- and the dispatcher's two firings --
    are on the platform, each a lookup row whose pysim span equals the RTL's."""
    for block, kinds in (("arbiter", [None]), ("dispatch", [st.JOB, st.CREDIT])):
        tm = st.model(block, PLATFORM, CLK)
        df = _corpus(block)
        assert len(df) == len(sb.NS) * 2 * len(kinds)
        assert np.allclose(df["span_pysim"], df["span_rtl"]), df
        for n in sb.NS:
            for rr in (False, True):
                for kind in kinds:
                    feat = st.features(n, rr, kind)
                    assert tm.confidence_feat(feat).level.value == "EXACT", feat


@pytest.mark.parametrize("block", ["arbiter", "dispatch"])
def test_pysim_reproduces_the_rtl_at_every_calibrated_point(block):
    """The fixture's own scenario, in pysim on the platform's model: the spans it measures are the
    RTL's, row by row (``corpus.csv``)."""
    tm = st.model(block, PLATFORM, CLK)
    for row in _corpus(block).to_dict("records"):
        n, rr = int(row["n"]), bool(row["rr"])
        pol = "round_robin" if rr else sb.POLICIES[block][0]
        spans = [(s, e) for s, e, _ in sb.pysim_spans(block, n, pol, tm, CLK)]
        if block == "dispatch":
            got = sb.job_and_credit(spans)["credit" if row["kind"] == st.CREDIT else "job"]
        else:
            got = set(sb.frame_gaps(spans, n))
            assert len(got) == 1, (row, got)
            (got,) = got
        assert got == row["span_rtl"], (row, got)


def test_a_block_is_untimed_unless_it_names_a_platform():
    """No platform, no model; a platform, the model attached as the simulation starts (``pre_sim``)."""
    for m in (StreamDispatch(name="d", sim=Simulation()), StreamArbiter(name="a", sim=Simulation())):
        m.pre_sim()
        assert m.timing_model is None
    d = StreamDispatch(name="d", sim=Simulation(), n_out=4, clk=CLK, platform_dir=PLATFORM)
    assert d.timing_model is None
    d.pre_sim()
    assert d.timing_model is not None and d.timing_model.component == "stream_dispatch_task.decision"


def test_the_platform_refuses_another_clock_and_an_unmeasured_point():
    """An unmeasured point at construction; another clock as the simulation starts -- a structural copy
    (a codegen rebuild at the class's default clock) is not refused, since it never runs."""
    with pytest.raises(ValueError, match="no measurement"):
        StreamArbiter(name="a", sim=Simulation(), n_in=3, clk=CLK, platform_dir=PLATFORM)
    d = StreamDispatch(name="d", sim=Simulation(), n_out=4, platform_dir=PLATFORM)          # 100 MHz
    with pytest.raises(ValueError, match="250 MHz"):
        d.pre_sim()


def test_naming_a_platform_does_not_change_a_module_key():
    """``platform_dir`` is context (``elaborate._CONTEXT_ATTRS``), and the model attached is too: the
    resource records filed for the blocks are found from a timed module as from an untimed one."""
    from waveflow.calib.module_key import identify_instance

    for cls, kw in ((StreamDispatch, {"n_out": 4, "credits": 4, "policy": "round_robin"}),
                    (StreamArbiter, {"n_in": 4, "policy": "round_robin"})):
        plain = cls(name="m", sim=Simulation(), clk=CLK, **kw)
        timed = cls(name="m", sim=Simulation(), clk=CLK, platform_dir=PLATFORM, **kw)
        key = [identify_instance(m, require_bound=False).key for m in (plain, timed)]
        assert key[0] == key[1]


def test_the_harness_and_the_pool_keep_the_platform():
    """A fresh copy (the harness's DUT) keeps ``platform_dir``; the pool hands it to both blocks, and
    the timed pool still returns every job once and whole."""
    d = StreamDispatch(name="d", sim=Simulation(), n_out=2, clk=CLK, platform_dir=PLATFORM)
    assert harness(d).dut().platform_dir == PLATFORM
    mod = DispatchPool(name="pool", sim=Simulation(), n=4, credits=1, policy="first_free", clk=CLK,
                       platform_dir=PLATFORM)
    sysm = harness(mod, workload="long")
    pool = sysm.dut()
    sysm.run()
    assert pool.dsp.timing_model is not None and pool.arb.timing_model is not None
    got = sysm.stand_ins["m_out"].words
    assert sorted_frames(got, 1) == mod.expected_frames("long")


def test_a_timed_dispatcher_is_slower_by_its_decision_cycles():
    """40 back-to-back 8-word jobs at 4 workers, first-free: untimed, back to back; timed, the
    platform's job gap between each pair."""
    tm = st.model("dispatch", PLATFORM, CLK)
    g = tm.predict_feat(st.features(4, False, st.JOB))[0] / CLK.period
    assert g > 0
    from waveflow.streams.dispatch_timing import pysim_gaps

    assert set(pysim_gaps(4, "first_free")["gaps"]) == {0}                       # untimed, 100 MHz
    timed = sb.job_and_credit([(s, e) for s, e, _ in sb.pysim_spans("dispatch", 4, "first_free", tm, CLK)])
    assert timed["job"] == round(g)


# --- at RTL -----------------------------------------------------------------------------------------


@pytest.mark.xsi
@pytest.mark.parametrize("block,n,policy", [("dispatch", 4, "round_robin"), ("arbiter", 4, "round_robin")],
                         ids=["dispatch4_rr", "arbiter4_rr"])
def test_rtl_decision_cost_is_the_platforms(block, n, policy):
    """The gate: the block re-measured at RTL at the platform's target (xczu48dr, 4 ns) gives the spans
    its committed table holds, and pysim on that table gives them too.  A Vitis version or body change
    that moves the decision cost fails here, naming the fixture to re-run."""
    if not (find_vivado_path() and find_vitis_path()):
        pytest.skip("XSI gate prerequisite missing: Vitis HLS + Vivado")
    fx = sb.FIXTURES[block]
    fx._platform_dir = PLATFORM
    rows = fx.rtl_rows(n, policy, ROOT)
    tm = st.model(block, PLATFORM, CLK)
    df = _corpus(block)
    for label, rs in rows.items():
        feat = {k: rs[0][k] for k in st.BASIS[block]}
        want = df[(df[list(feat)] == pd.Series(feat)).all(axis=1)]["span_rtl"].item()
        assert {r["span"] for r in rs} == {want}, (label, want, sorted({r["span"] for r in rs}))
    py = fx.pysim_rows(n, policy, tm, CLK)
    for label, rs in py.items():
        assert {round(r["span"] / CLK.period) for r in rs} == {r["span"] for r in rows[label]}, label
