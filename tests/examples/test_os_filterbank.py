"""The overlap-save filter bank (``examples/os_filterbank``, ``plans/shared_kernel.md`` Stage 5): ``C``
channels sharing ONE ``SsrFft``, forward then inverse per block, through an arbiter, a tag beside the FFT,
resident filters, per-channel credits and demuxes back to each channel.

pysim: the golden is the FFT's own model, so the chain is bit-exact by construction -- and close to the
filter it implements (``float_check``); each stage alone and the bank on two scenarios against it, more
blocks than credits so the loop runs full, the channels finishing out of order; without the credit the
same bank deadlocks and the run still returns.  At RTL (``-m xsi``): a front alone, post alone, the bank
on both scenarios -- every recording equal to pysim's -- and the bank's timing pinned.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import examples.os_filterbank.os_filterbank as fb
from examples.os_filterbank.os_filterbank_build import (
    FRONT_TOP,
    HERE,
    POST_TOP,
    TOP,
    build_dag,
    gen_top,
)
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run
from waveflow.hw.harness import harness
from waveflow.hw.loop_check import LoopDepthWarning
from waveflow.simulation.simulation import Simulation


def _bursts(sysm, port):
    return [np.asarray(b, dtype=np.uint64).reshape(-1) for b in sysm.stand_ins[port].words]


def _assert_golden(sysm):
    for port, want in sysm.golden_outputs().items():
        got = _bursts(sysm, port)
        assert len(got) == len(want), port
        for g, w in zip(got, want):
            np.testing.assert_array_equal(g, np.asarray(w, dtype=np.uint64).reshape(-1))


def _n_blocks(kind, ch=None):
    return sum(len(x) // fb.B for c, js in fb.jobs(kind).items() if ch in (None, c) for _tx, x in js)


@pytest.mark.parametrize("kind", ["test", "long"])
def test_the_chain_is_the_filter(kind):
    """The bit-exact chain against ``np.convolve`` in floating point: within 0.5% of the output's peak."""
    for ch, js in fb.jobs(kind).items():
        for _tx, x in js:
            assert fb.float_check(ch, x) < 5e-3


@pytest.mark.parametrize("kind", ["test", "long"])
def test_the_bank_matches_the_golden(kind):
    sysm = harness(fb.OsFilterBank, workload=kind)
    sysm.run()
    _assert_golden(sysm)
    for ch, js in fb.jobs(kind).items():                         # every job answered, every block
        assert len(_bursts(sysm, f"m_status_{ch}")) == len(js)
        assert len(_bursts(sysm, f"m_out_{ch}")) == 2 * _n_blocks(kind, ch)


@pytest.mark.parametrize("cls", [fb.OsFront, fb.OsPost], ids=["front", "post"])
def test_each_stage_alone_matches_the_golden(cls):
    sysm = harness(cls, workload="test")
    sysm.run()
    _assert_golden(sysm)


def test_more_blocks_than_credits():
    """Every channel puts more blocks in than its credit admits, so every front waits on credit."""
    for kind in ("test", "long"):
        assert min(_n_blocks(kind, ch) for ch in range(fb.C)) > fb.CR


def test_without_the_credit_the_loop_deadlocks_and_the_run_still_returns(monkeypatch):
    """The witness for the credit, and for asserting counts: with no effective credit and a feedback link
    shorter than the loop can fill, post blocks on it while the arbiter is stuck pushing a new block
    into the FFT -- a cycle of full FIFOs.  pysim returns as if it had finished; only the counts say
    otherwise."""
    monkeypatch.setattr(fb, "CR", 100)
    monkeypatch.setattr(fb, "DEPTH", {**fb.DEPTH, "fb": fb.FRAME, "frm": 4, "y": 4, "x": 2, "tag": 2,
                                      "sel": 2, "blk": fb.FRAME})
    sysm = harness(fb.OsFilterBank, workload="long")
    with pytest.warns(LoopDepthWarning):                          # the loop check names the short link
        sysm.run()                                                 # returns: no error, no hang
    done = sum(len(_bursts(sysm, f"m_status_{ch}")) for ch in range(fb.C))
    assert done < sum(len(js) for js in fb.jobs("long").values())


def test_the_generated_top_carries_the_loop(tmp_path: Path):
    """The feedback is a channel like any other at its stated depth: post writes it, the arbiter reads it
    as input 0; the FFT's tasks are flattened into the same top, its s_dir fed by pre."""
    gen_top(fb.OsFilterBank(name="os_filterbank", sim=Simulation()), tmp_path)
    top = (tmp_path / "gen" / "os_filterbank.cpp").read_text()
    assert f"#pragma HLS STREAM variable=fb depth={fb.DEPTH['fb']}" in top
    assert "frm, sel, fb, blk0, blk1, blk2);" in top               # feedback first: input 0
    assert "s_coef, tag, y, fb, ans, st, crd);" in top             # post writes it
    assert "_rt_in_reg, x, dir, fft_in_reg" in top                 # pre's direction into the FFT
    assert top.count("hls_thread_local hls::task") == fb.C + 16                  # fronts, arb, pre, 10 FFT tasks, post, 3 demuxes


def test_the_system_top_drives_a_narrow_output_in_full():
    """The bank's ports are of three widths, and the system top's stream nets are the widest (184 bits).
    A 32-bit status port drives the low bits of its net; the rest are driven 0, never left X (the XSI
    testbench refuses an X in an accepted beat)."""
    from waveflow.build.system_top import render_system_top, system_top_spec
    from waveflow.build.system_xsi import discover

    sysm = harness(fb.OsFilterBank, workload="test")
    xbar, _host, cut = discover(sysm)
    v = render_system_top(system_top_spec(xbar, cut, top="t", data_width=sysm.data_width))
    assert sysm.data_width == fb.W_ANS
    for c in range(fb.C):
        assert f".m_status_{c}_TDATA(m_status_{c}_if_TDATA[31:0])" in v
        assert f"assign m_status_{c}_if_TDATA[183:32] = 152'd0;" in v
        assert f"assign m_status_{c}_if_TKEEP[22:4] = 19'd0;" in v
        assert f".m_out_{c}_TDATA(m_out_{c}_if_TDATA)" in v          # a full-width port: as before


@pytest.fixture(scope="module")
def dag():
    return build_dag(work_dir=WORK)


@pytest.mark.xsi
@pytest.mark.parametrize("prefix,top,outs", [
    ("", TOP, [f"m_out_{c}" for c in range(fb.C)] + [f"m_status_{c}" for c in range(fb.C)]),
    ("long_", TOP, [f"m_out_{c}" for c in range(fb.C)] + [f"m_status_{c}" for c in range(fb.C)]),
    ("front_", FRONT_TOP, ["m_blk"]),
    ("post_", POST_TOP, ["m_ans", "m_crd", "m_fb", "m_st"]),
], ids=["bank", "long", "front", "post"])
def test_rtl_recordings_match_pysim(dag, prefix, top, outs):
    """THE gates: a front alone, post alone, and the bank with its loop on both scenarios -- every
    recording at RTL equal to pysim's, word for word and TLAST for TLAST."""
    work = system_run(dag, HERE, f"{prefix}{top}", through=f"{prefix}compare")
    assert sorted(recorded_names(work)) == sorted(outs)
    assert compared_clean(work)


#: The cycle each channel's last status leaves the bank at RTL (Vitis 2025.1, xczu48dr at 250 MHz).
LAST_STATUS = {"test": {0: 821, 1: 1067, 2: 888}, "long": {0: 2349, 1: 2371, 2: 2424}}


@pytest.mark.xsi
@pytest.mark.parametrize("prefix,kind", [("", "test"), ("long_", "long")], ids=["bank", "long"])
def test_rtl_timing_is_exact(dag, prefix, kind):
    """The bank's timing, pinned: every job's status out, and the cycle each channel's last one leaves.
    A change to the credit, the arbiter's policy, a loop depth or a stage body moves it."""
    work = system_run(dag, HERE, f"{prefix}{TOP}", through=f"{prefix}compare")
    for ch, js in fb.jobs(kind).items():
        cyc = np.fromfile(work / "traces" / f"m_status_{ch}" / "cycles.bin", dtype="<u8")
        assert len(cyc) == len(js)
        assert int(cyc[-1]) == LAST_STATUS[kind][ch], ch
