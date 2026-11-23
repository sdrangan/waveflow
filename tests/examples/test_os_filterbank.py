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

from itertools import pairwise
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


def _pre_alone_x():
    """``pre`` alone on ``test``, every frame waiting (the harness's stand-ins send at once): the golden
    met, and each frame on ``x`` as ``(first beat, last beat)`` -- a burst started at ``t0`` and delivered
    at ``td`` has its beats at ``t0 + 1 .. td - 1`` (``examples/os_filterbank/fidelity.py``)."""
    sysm = harness(fb.OsPre, workload="test")
    dut = sysm.dut()
    per, rows = dut.clk.period, []
    iface = dut.m_x.interface
    orig = iface._transfer

    def _transfer(ep, words, cycles, tstart):
        stamp = yield from orig(ep, words, cycles, tstart)
        rows.append((round(stamp[0] / per) - 1, round(stamp[2] / per) - 1, int(np.asarray(words).shape[0])))
        return stamp
    iface._transfer = _transfer
    sysm.run()
    for port, want in sysm.golden_outputs().items():          # m_x frames by count: compare the words
        got = np.concatenate([np.asarray(b, dtype=np.uint64).reshape(-1)
                              for b in sysm.stand_ins[port].words])
        np.testing.assert_array_equal(got, np.concatenate([np.asarray(w, dtype=np.uint64).reshape(-1)
                                                           for w in want]), err_msg=port)
    return rows


def test_pre_feeds_the_fft_back_to_back_when_frames_wait():
    """``plans/ssr_fft_gap_throughput.md``, fix 1, in pysim: with every frame waiting, each frame's first
    word follows the previous frame's last on the next cycle -- no idle cycle between frames, which costs
    the FFT a frame-time -- and a frame is ``NW`` words in ``NW + 1`` cycles (the tag's cycle, inside the
    frame, between words 0 and 1)."""
    rows = _pre_alone_x()
    assert len(rows) == 2 * _n_blocks("test") and all(n == fb.NW for _f, _l, n in rows)
    assert all(last - first == fb.NW for first, last, _n in rows)
    assert all(b[0] == a[1] + 1 for a, b in pairwise(rows))


def _pre_body(events: list[str], n: int, tmp_path: Path) -> list[tuple[int, str, int]]:
    """``os_pre_body::step`` (``src/os_pre_task.h``) under g++ with stand-in headers, a cycle at a time
    (``os_filterbank_cpp/pre_check.cpp``): ``(cycle, stream, value)`` of every word it writes."""
    import subprocess

    here = Path(__file__).resolve().parent
    exe = tmp_path / "pre_check"
    cc = subprocess.run(["g++", "-std=c++17", "-Wall", "-Wno-unknown-pragmas", f"-I{here / 'os_filterbank_cpp'}",
                         f"-I{here.parent / 'streams' / 'cpp_mock'}", f"-DNW_={fb.NW}",
                         str(here / "os_filterbank_cpp" / "pre_check.cpp"), "-o", str(exe)],
                        capture_output=True, text=True, check=False)
    assert cc.returncode == 0, cc.stderr
    run = subprocess.run([str(exe)], input="\n".join([*events, f"run {n}"]) + "\n", capture_output=True,
                         text=True, check=False)
    assert run.returncode == 0, run.stderr                    # a read of an empty stream aborts
    return [(int(c), s, int(v)) for c, s, v in (ln.split() for ln in run.stdout.splitlines())]


def _frames_x(out, nw=fb.NW):
    xs = [c for c, s, _v in out if s == "x"]
    return [(xs[i], xs[i + nw - 1]) for i in range(0, len(xs), nw)]


@pytest.mark.skipif(__import__("shutil").which("g++") is None, reason="no g++: the body's logic check")
def test_pre_body_moves_words_as_its_model(tmp_path):
    """The HLS body's loop under g++ (logic only, not Vitis csim): every word, tag and direction out in
    order, and on the cycles the pysim model charges -- frames waiting: back to back, ``NW + 1`` cycles a
    frame, as ``test_pre_feeds_the_fft_back_to_back_when_frames_wait``; words arriving a cycle apart
    (cut-through): the first word out the cycle it is readable, the last one cycle after; and an isolated
    frame's last word is not held for a next frame that never comes."""
    sels = [3, 0, 1, 0]
    ev = []
    for k, s in enumerate(sels[:3]):                       # three frames, all here at cycle 0
        ev += [f"0 sel {s}", f"0 tag {1000 + k}"] + [f"0 w {100 * k + i}" for i in range(fb.NW)]
    t0 = 200                                               # then one alone, its words a cycle apart
    ev += [f"{t0} sel {sels[3]}", f"{t0} tag 1003"] + [f"{t0 + 1 + i} w {300 + i}" for i in range(fb.NW)]
    out = _pre_body(ev, t0 + fb.NW + 20, tmp_path)
    assert [v for _c, s, v in out if s == "x"] == [100 * k + i for k in range(4) for i in range(fb.NW)]
    assert [v for _c, s, v in out if s == "tag"] == [1000, 1001, 1002, 1003]
    assert [v for _c, s, v in out if s == "dir"] == [1 if s == 0 else 0 for s in sels]
    fr = _frames_x(out)
    for a, b in pairwise(fr[:3]):                      # waiting: back to back, NW words in NW + 1
        assert b[0] == a[1] + 1 and b[1] - b[0] == fb.NW
    assert fr[3] == (t0 + 1, t0 + fb.NW + 1)               # alone: last word read at t0 + NW, out next
    # Each frame's direction is written with its first word (the FFT reads it at its head word).
    dirs = [c for c, s, _v in out if s == "dir"]
    assert dirs == [f for f, _l in fr]
    # The same cycles as the model's, frames waiting (pysim's beats are offset by the harness's start).
    rows = _pre_alone_x()[:3]
    assert [(f - rows[0][0], la - rows[0][0]) for f, la, _n in rows] == \
        [(f - fr[0][0], la - fr[0][0]) for f, la in fr[:3]]


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


#: How far pysim's last status may be from the RTL's, per channel, a fraction of the RTL's cycle, per
#: scenario (``plans/osfb_pysim_fidelity.md`` Step 5; the user, 2026-11-22).  Measured at the time, every
#: stream block timed: ``test`` -2.68% at worst (channel 0), ``long`` -0.64% (channel 0) -- pysim short on
#: every channel, ledger row 21 the largest part of what remains.  The untimed bank fails ``test``'s bound
#: (channel 0 at -3.41%).
PYSIM_TOL = {"test": 0.03, "long": 0.01}


@pytest.mark.parametrize("kind", ["test", "long"])
def test_pysim_timing_is_within_tolerance_of_the_rtl(kind):
    """The pysim gate (``plans/osfb_pysim_fidelity.md`` Step 5): the bank in pysim with every stream block
    timed on the packaged platform -- the arbiter and the three demuxes, the blocks with a decision table
    -- puts each channel's last status within its scenario's :data:`PYSIM_TOL` of the cycle the RTL pins
    (:data:`LAST_STATUS`), in either direction.  pysim's cycle is that burst's last beat (a burst
    delivered at ``td`` cycles has its beats at ``t0 .. td - 1``), as ``examples/os_filterbank/fidelity.py``
    compares it.  No vendor tools: the RTL cycles are the XSI gate's."""
    from examples.os_filterbank.os_filterbank_build import N_CYCLES
    from waveflow.calib.platform import packaged_platforms_dir
    from waveflow.streams.timing import PLATFORM

    sysm = harness(fb.OsFilterBank, workload=kind, n_cycles=N_CYCLES[kind])
    dut = sysm.dut()
    blocks = (dut.arb, dut.dmx_ans, dut.dmx_st, dut.dmx_crd)
    for blk in blocks:
        blk.platform_dir = packaged_platforms_dir() / PLATFORM
    sysm.run()
    assert all(blk.timing_model is not None for blk in blocks)
    per = dut.clk.period
    for ch, js in fb.jobs(kind).items():
        assert len(sysm.stand_ins[f"m_status_{ch}"].words) == len(js), ch
        got = round(sysm.stand_ins[f"m_status_{ch}"].stream_ep.last_stamp.delivered / per) - 1
        rtl = LAST_STATUS[kind][ch]
        assert abs(got - rtl) <= PYSIM_TOL[kind] * rtl, (ch, got, rtl, f"{(got - rtl) / rtl:+.2%}")
