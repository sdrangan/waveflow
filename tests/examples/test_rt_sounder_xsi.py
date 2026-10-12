"""examples/rt_sounder at RTL (plans/sionna_rt/sionna_rt.md, step 18, AC6): source mode replayed.

``RtSignalSource``'s rendered bundle -- the street canyon's received signal, noise included -- is
replayed by ``RfFileSource`` into the I/Q ``Rfdc`` ADC model, through ``examples/rf_loopback``'s
``rf_pass_through`` RTL, out of the DAC model into a sink.  The geometry is the loopback I/Q gate's,
so the transient and the drops are judged against that gate's recorded expectations
(``tests/examples/test_rf_loopback_xsi.py``, ``WANT_IQ_*``).

**What is bit-exact, and at what grain.**  ``rf_pass_through`` reads a whole block of its own --
64 words (``XSI_NWORDS_BLK``), half an RF block of this I/Q geometry -- before it writes one, so
while the DAC paces the write the ADC's words have nowhere to go and are dropped (pattern A,
``WANT_ADC_DROPPED_IS_STRUCTURAL``): about 15 words in each write phase, two phases per RF block.
The first data block survives whole; every later one is spliced across source blocks at the drops.
So the claim is made per **word**: every word that reaches the sink is the matching word of the
ADC-quantized source, in order, and the words skipped between them are exactly the recorded gaps
(:data:`WANT_SRC_GAPS`).  The first data block is also asserted whole, as the I/Q gate does.

The two tests without a marker need no toolchain and run under the Sionna blocker too
(``tests/rfenv/test_no_sionna.py``).
"""

from __future__ import annotations

import itertools
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.rf_loopback.rf_loopback_xsi import TOP, XSI_NBLK
from examples.rt_sounder.rt_sounder import (
    IN_BUNDLE_SRC,
    OUT_BUNDLE_SRC,
    SOURCE_BLKSIZE,
    adc_quantized,
    run_source_mode,
)
from examples.rt_sounder.rt_sounder_xsi import COUNTERS, TB_NS, generate_tb, make_xsi_tb
from tests.examples.test_rf_loopback_xsi import (
    RTL_STARTUP_BLOCKS,
    WANT_IQ_ADC_WORDS,
    WANT_IQ_DAC_BLOCKS_OUT,
    WANT_IQ_DAC_ZERO_FILLED,
    WANT_IQ_DATA_BLOCKS_OUT,
    WANT_IQ_SINK_BLOCKS,
    WANT_IQ_SINK_LAST_BLOCK_CYCLE,
)
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rt_sounder"
XSI = ROOT / "xsi"
#: The RTL's owner: this example synthesizes nothing of its own.
LOOPBACK = ROOT.parent / "rf_loopback"
PROJ = LOOPBACK / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
#: Samples per AXIS word (the I/Q word: two complex samples).
SPW = 2

#: **The drop pattern, recorded** (Vivado 2024.1 xsim, ``rf_pass_through`` csynth'd with Vitis HLS
#: 2024.1 on 2026-10-12): the source words skipped between delivered ones, as ``(first skipped
#: word, count)``.  Pinned exactly rather than bounded by the ADC's drop count: the 30 drops after the
#: last delivered word would otherwise leave room for an RTL that lost words of its own.  The pattern
#: is a property of the RTL and the converter model's input FIFO, not of the data -- the loopback
#: I/Q gate's random scenario drops the same words (M5 review) -- so it moves only with one of those,
#: and a change wants diagnosing, like :data:`WANT_IQ_SINK_LAST_BLOCK_CYCLE`, before re-recording.
WANT_SRC_GAPS = (
    (194, 14),
    (272, 16),
    (352, 14),
    (430, 15),
    (509, 15),
    (588, 15),
    (667, 16),
    (747, 14),
    (825, 15),
)
#: Words the ADC dropped in all: the gaps' 134 plus 30 after the last word that reached the sink.
WANT_ADC_DROPPED = 164
#: The grid periods at the sink that carry data (zero-fill between them: the relay cannot sustain
#: the DAC's rate).
WANT_DATA_PERIODS = (1, 2, 4, 5, 6, 7)


def _require(cond: bool, why: str) -> None:
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


# ---------------------------------------------------------------------------
# Generation -- no toolchain
# ---------------------------------------------------------------------------


def test_source_mode_lowers_to_a_replay_harness():
    """The source's twin is ``RfFileSource`` on the rendered bundle; the converters are I/Q."""
    from waveflow.build.composite_gen import render_tb_harness, tb_top_spec

    h = render_tb_harness(tb_top_spec(make_xsi_tb()), ns=TB_NS)
    assert re.search(r"RfFileSource \w+_adc_if_tx;", h)
    assert f'in_bundle = "{IN_BUNDLE_SRC}";' in h
    assert f'out_bundle = "{OUT_BUNDLE_SRC}";' in h
    assert h.count("RFDC_IQ") == 2  # the ADC's format and the DAC's


def test_source_mode_counters_main_names_real_harness_members():
    """The hand-written main reads members the generated harness declares (it is compiled only
    under ``-m xsi``, so a renamed testbench would otherwise surface there first)."""
    from waveflow.build.composite_gen import render_tb_harness, tb_top_spec

    h = render_tb_harness(tb_top_spec(make_xsi_tb()), ns=TB_NS)
    main = (XSI / f"{COUNTERS}.cpp").read_text(encoding="utf-8")
    used = set(re.findall(r"\bh\.(\w+)\.", main))
    assert used, "the counters main reads no harness members"
    for member in sorted(used):
        assert re.search(
            rf"[\w>] {member};", h
        ), f"the harness declares no member {member}"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.xsi
def test_source_mode_replays_through_the_rtl_word_for_word():
    """AC6's RTL half: the same bytes in both backends, the first block whole, every word that
    reaches the sink the matching quantized source word, and the I/Q gate's accounting.
    """
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(
        PROJ.is_dir(),
        f"no csynth RTL at {PROJ} -- run "
        f"python -m examples.rf_loopback.rf_dut_build --through csynth",
    )
    _require(rtl_staleness(LOOPBACK, TOP) is None, rtl_staleness(LOOPBACK, TOP) or "")

    generate_tb(ROOT)
    shutil.rmtree(XSI / "xsim.dir" / TOP, ignore_errors=True)
    # Never pass on the last run's output.
    shutil.rmtree(XSI / OUT_BUNDLE_SRC, ignore_errors=True)
    r = subprocess.run(
        xsi_runner_cmd(TOP, COUNTERS),
        cwd=str(XSI),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    out = (r.stdout or "") + (r.stderr or "")
    assert (
        "XSI_EXITCODE=0" in out
    ), f"the replay did not complete cleanly:\n{out[-3000:]}"
    counters = {
        k: int(v)
        for k, v in re.findall(r"^(\w+)=(\d+)$", out, re.MULTILINE)
        if k != "XSI_EXITCODE"
    }

    from waveflow.simulation.rf_tb import read_rf_bundle

    # --- 1) The converter models were built for the I/Q word ------------------------------------
    assert counters["FMT_IQ_MODE"] == 1 and counters["FMT_WORD_BITS"] == 64

    # --- 2) Both backends start from the same bytes ---------------------------------------------
    tb, sent, got_py = run_source_mode()
    replayed = read_rf_bundle(XSI / IN_BUNDLE_SRC, 1, SOURCE_BLKSIZE, complex_samp=True)
    assert len(replayed) == len(sent) == XSI_NBLK
    for k, (a, b) in enumerate(zip(replayed, sent)):
        assert np.array_equal(a, b), f"bundle block {k} differs from pysim's render"
    tb.dac_if.assert_clean(startup_blocks=tb.loop_blk_latency)

    # --- 3) The capture: the RTL transient, then the surviving data blocks ----------------------
    rtl = read_rf_bundle(XSI / OUT_BUNDLE_SRC, 1, SOURCE_BLKSIZE, complex_samp=True)
    assert len(rtl) == WANT_IQ_SINK_BLOCKS
    for k in range(RTL_STARTUP_BLOCKS):
        assert not np.any(rtl[k]), f"block {k} is inside the RTL startup transient"
    periods = tuple(k for k, b in enumerate(rtl) if np.any(b))
    assert periods == WANT_DATA_PERIODS
    data = [rtl[k] for k in periods]
    assert len(data) == WANT_IQ_DATA_BLOCKS_OUT, (
        f"{len(data)} of {len(rtl)} grid periods carried data, the I/Q gate records "
        f"{WANT_IQ_DATA_BLOCKS_OUT}"
    )
    # The first data block is pysim's first data block, whole.
    first_py = got_py[tb.loop_blk_latency]
    assert np.array_equal(rtl[RTL_STARTUP_BLOCKS], first_py)

    # --- 4) Every word that reaches the sink is the matching quantized source word, in order ----
    src = np.concatenate([adc_quantized(b)[0] for b in sent]).reshape(-1, SPW)
    assert len({w.tobytes() for w in src}) == len(
        src
    ), "source words must be distinguishable"
    index = {w.tobytes(): i for i, w in enumerate(src)}
    words = np.concatenate([b[0] for b in data]).reshape(-1, SPW)
    pos = [index.get(w.tobytes(), -1) for w in words]
    assert (
        -1 not in pos
    ), f"sink word {pos.index(-1)} is no word of the quantized source"
    assert pos[0] == 0 and all(
        b > a for a, b in itertools.pairwise(pos)
    ), "words out of order"
    gaps = tuple((a + 1, b - a - 1) for a, b in itertools.pairwise(pos) if b > a + 1)
    assert gaps == WANT_SRC_GAPS, (
        f"the source words skipped between delivered ones are {gaps}, recorded {WANT_SRC_GAPS}. "
        f"Extra or longer gaps are words lost beyond the ADC's; check the counters before "
        f"re-recording."
    )
    assert counters["ADC_DROPPED"] == WANT_ADC_DROPPED
    assert sum(n for _, n in gaps) <= counters["ADC_DROPPED"]

    # --- 5) The accounting is the I/Q gate's ----------------------------------------------------
    assert counters["ADC_WORDS_SENT"] + counters["ADC_DROPPED"] == WANT_IQ_ADC_WORDS
    assert counters["ADC_DROPPED"] > 0
    assert counters["DAC_BLOCKS_OUT"] == WANT_IQ_DAC_BLOCKS_OUT
    assert (
        counters["DAC_BLOCKS_ZERO_FILLED"]
        == counters["DAC_BLOCKS_OUT"] - WANT_IQ_DATA_BLOCKS_OUT
        == WANT_IQ_DAC_ZERO_FILLED
    )
    assert counters["SINK_BLOCKS_IN"] == WANT_IQ_SINK_BLOCKS
    assert counters["ADC_CHAN_DROPPED"] == 0 and counters["DAC_CHAN_DROPPED"] == 0
    assert counters["SRC_BLOCKS_OUT"] == XSI_NBLK
    assert counters["SINK_LAST_BLOCK_CYCLE"] == WANT_IQ_SINK_LAST_BLOCK_CYCLE
    # What the DAC took but could not yet play is under one block; what the DUT holds, under one
    # relay chunk (64 words).
    blk_words = SOURCE_BLKSIZE // SPW
    assert 0 <= counters["DAC_WORDS_RECV"] - len(words) < blk_words
    assert 0 <= counters["ADC_WORDS_SENT"] - counters["DAC_WORDS_RECV"] < 64
