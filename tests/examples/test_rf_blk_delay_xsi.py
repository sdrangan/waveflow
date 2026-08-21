"""The **pattern-B loop at RTL** — two sample buffers and a user block, through real Verilog.

``plans/adc_model.md`` § *Two design patterns*, the B case, and the first design in this repo whose
generated top is **two levels deep**: ``RfBlkDelayLoop`` contains ``RfSampBufRx`` and ``RfSampBufTx``,
which are themselves composites, and ``hls::task`` has no hierarchy — so the generator flattens it
into five tasks joined by six channels (``tests/build/test_composite_flattening.py`` pins that).
What xsim elaborates is the **wrapper** (``rf_blk_delay_top``): the kernel plus *both* buffers'
``bram_t2p`` memories, so the testbench sees only AXI-Stream.

**Why this gate is worth its runtime when ``rf_samp_buf_rx``/``_tx`` already pass.**  Those gate each
buffer against a driver written to suit it.  Here the two are joined back to back through a user
block, and the thing being checked is the one property neither can check alone: that a sample
entering the ADC comes out of the DAC, unchanged, at the moment the design asked for.  Both converter
edges are real converter models, so both ways of losing samples are live at once — the ADC drops what
the fabric will not take, the DAC plays whatever is in its FIFO when the period comes due, and
neither loss has a protocol event.

Five things are gated, and they fail in different ways:

* **Nothing was dropped and nothing underran** — the two counters pattern B exists to make zero.
* **The played samples**, bit-exact against the ramp that went in, *through both converters*.
* **The delay is the one that was asked for**, measured from the played stream rather than asserted.
* **Every command answered** by both buffers, in order, with its own tid — the in-band framing gate.
* **The completion cycle**, recorded exactly.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.rf_blk_delay.rf_blk_delay import (
    BLKSIZE,
    DELAY_BLOCKS,
    N_BLK,
    SAMP_BW,
    SRC_NBLK,
    ramp_samples,
    write_scenario,
)
from examples.rf_blk_delay.rf_blk_delay_build import RTL_FILES, TOP, WRAPPER
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd
from waveflow.hw.rf_samp_buf import RF_SAMP_BUF_OK, RxResp
from waveflow.hw.rf_samp_buf_tx import TxResp

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_blk_delay"
#: The hand-written main: the generated one runs and dumps, this one also prints the model counters,
#: and this design's two most important numbers live only on the converter models.
TB = "rf_blk_delay_counters"

#: Word width on the wire — four 16-bit samples, ``Rfdc``'s ceiling.
WORD_BW = 64

#: Cycle the last TX response reached its sink.  Recorded 2026-08-17 on the first green run.
#:
#: Exact, not a bound: it moves only if the design's timing changes, and either direction is worth a
#: human.  Almost all of it is the loop waiting on the CONVERTER rather than on the fabric — the last
#: block cannot be captured before the ADC has produced it, and 13 blocks of 256 samples at one
#: sample per cycle is ~3328 cycles on its own.  The fabric's whole contribution is the ~130 cycles
#: between that and this number.
WANT_TX_RESP_LAST_CYCLE = 4210
#: ...and the RX response that fed it, 384 cycles earlier: the last block still has to cross
#: ``BlkDelay`` and be placed by the loader after the capture has finished serving it.
WANT_RX_RESP_LAST_CYCLE = 3826

#: **Blocks relayed — SRC_NBLK, not N_BLK, and the difference is a real pysim/RTL divergence.**
#:
#: The synthesized body carries no block count (see ``blk_delay_task.h``): it relays for as long as
#: the ADC produces, and idles by blocking on a block that never arrives.  pysim needs a bound only so
#: SimPy's event queue can empty, so it stops at ``N_BLK``.  Given a source that plays ``SRC_NBLK``,
#: the RTL therefore answers once more than the twin does — which is the twin being bounded, not the
#: hardware being wrong, and it is asserted here rather than left as a surprise.
WANT_BLOCKS_RELAYED = SRC_NBLK

#: Samples of start-up phase between the player's sample index and the DAC's block grid — measured,
#: 64, a quarter of a block.  See ``test_the_delay_the_rtl_produced_is_the_one_the_design_asked_for``
#: for why this is a converter-edge property and not a delay error.  pysim's skew is 0.
RTL_GRID_SKEW = 64

#: Blocks the DAC pulled out of the fabric over the 60000-cycle run — 15000 words at 0.25 words per
#: cycle, 64 words to a block.  A property of the run length and the converter's rate, not of the
#: design: the DAC keeps playing (the buffer's last contents) long after the source has stopped.
WANT_DAC_BLOCKS_OUT = 234


def _require(cond: bool, why: str) -> None:
    """Skip loudly — a silent skip on a gate this expensive reads as 'passed' in a summary line."""
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


def _counters(out: str) -> dict[str, int]:
    """The ``KEY=VALUE`` lines the counters main prints."""
    vals = {}
    for line in out.splitlines():
        if "=" in line and line.split("=", 1)[0].isupper():
            k, v = line.split("=", 1)
            try:
                vals[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return vals


@pytest.fixture(scope="module")
def run() -> tuple[dict[str, int], str]:
    """One RTL run, shared by the assertions below."""
    xsi = ROOT / "xsi"
    _require((xsi / XSI_RUNNER).exists(), f"{xsi / XSI_RUNNER}")
    proj = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
    _require(proj.is_dir(), f"no csynth RTL at {proj} — run rf_blk_delay_build.py --through csynth")
    # SECOND INSTANCE OF THIS CLASS: `*_proj/` is gitignored build output, and a gate that
    # compares a cycle count against RTL it did not produce reports "a real behaviour change"
    # when the truth is a stale artifact. See rtl_staleness().
    _require(rtl_staleness(ROOT, 'rf_blk_delay') is None, rtl_staleness(ROOT, 'rf_blk_delay') or "")
    for f in RTL_FILES:
        _require((xsi / f).is_file(), f"{xsi / f} — run rf_blk_delay_build.py --through codegen_dut")

    # Regenerate the file list from the RTL actually on disk; never trust the committed .f.
    (xsi / f"rtl_{WRAPPER}.f").write_text(render_rtl_f(TOP, ROOT, extra=RTL_FILES), encoding="utf-8")
    # Force a clean elaboration of the WRAPPER: a cached snapshot proves nothing about this design.
    shutil.rmtree(xsi / "xsim.dir" / WRAPPER, ignore_errors=True)
    for stale in (f"{TB}.exe", f"{TB}.bin", f"{TB}.o"):
        (xsi / stale).unlink(missing_ok=True)
    for od in ("rf_out", "rxresp", "txresp"):
        shutil.rmtree(xsi / "vectors" / od, ignore_errors=True)
    write_scenario(xsi)

    r = subprocess.run(xsi_runner_cmd(WRAPPER, TB), cwd=str(xsi),
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in out, f"the RTL run did not complete cleanly:\n{out[-3000:]}"
    return _counters(out), out


def _played() -> np.ndarray:
    """The samples the RF sink captured, as unsigned 16-bit words — the far side of the DAC."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    d = ROOT / "xsi" / "vectors" / "rf_out"
    if not d.is_dir():
        return np.zeros(0, dtype=np.uint64)
    blocks = read_rf_bundle(d, 1, BLKSIZE)
    flat = np.concatenate([np.asarray(b, dtype=np.float64).ravel() for b in blocks])
    ints = np.rint(flat * float(1 << (SAMP_BW - 1))).astype(np.int64)
    return (ints % (1 << SAMP_BW)).astype(np.uint64)


def _resps(name: str, cls) -> list[tuple[int, ...]]:
    """Deserialize a response bundle **through the schema**, never by slicing raw words.

    At a 64-bit word all three 16-bit fields ride in ONE word, so a hand-rolled slice reads the whole
    response as a single enormous integer — which is exactly the bug the generated serializers exist
    to prevent, and exactly the one that is invisible at a 16-bit word.
    """
    from waveflow.utils.burst_io import read_burst_bundle

    d = ROOT / "xsi" / "vectors" / name
    if not d.is_dir():
        return []
    flat = np.concatenate(read_burst_bundle(d)).astype(np.uint64)
    n = cls.nwords_per_inst(WORD_BW)
    out = []
    for i in range(0, flat.size, n):
        r = cls().deserialize(flat[i:i + n], word_bw=WORD_BW)
        out.append(tuple(int(getattr(r, f)) for f in cls.elements))
    return out


def _measured_delay(played: np.ndarray) -> int | None:
    """``out_ts - in_ts``, measured: where input sample 0 lands in what the DAC played."""
    want = ramp_samples()[:16]
    for i in range(max(0, played.size - 16)):
        if np.array_equal(played[i:i + 16], want):
            return i
    return None


# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_rtl_lost_nothing_at_either_converter(run):
    """**THE gate for pattern B**, and it is two counters because a loop has two ways to lose.

    ``ADC_DROPPED`` is what the RX ingress failed to accept, and it must be zero *structurally*: the
    ingress writes a BRAM, which cannot refuse, so there is no rate at which it stalls its input.
    That is the number ``rf_loopback`` could not get to zero without splitting its body in two —
    72 of 512 words gone at pattern A's first attempt — and here it is zero for free.

    ``DAC_UNDERRUN`` is the mirror: word periods the converter came due with an empty input FIFO.
    Zero because the TX buffer is primed ``delay_blocks`` blocks ahead, which is the whole reason the
    delay has a floor.

    **This counter only became evidence when the model was fixed.**  ``RfdcDacSlave`` used to drive
    ``TREADY`` high unconditionally, so the free-running player was paced by nothing and this counted
    the beat pattern of two unrelated periods (10000 of 60000 cycles on a bit-exact run) rather than
    starvation.  Held to the converter's grid it reads 0 — and the same fix moved the measured
    end-to-end delay from 960 back to the 1024 the design asks for.  See ``xsi_rfdc.h``.
    """
    c, out = run
    assert c["ADC_WORDS_SENT"] > 0, f"the ADC drove nothing:\n{out[-2000:]}"
    assert c["ADC_DROPPED"] == 0, (
        f"the fabric refused {c['ADC_DROPPED']} ADC words (last at cycle "
        f"{c.get('ADC_LAST_DROP_CYCLE')}). RfSampBufRx's ingress writes a BRAM and is supposed to "
        f"make that impossible at any rate check_rate allows.")
    assert c["DAC_WORDS_RECV"] > 0, "the DAC received nothing"
    assert c["DAC_UNDERRUN"] == 0, (
        f"the DAC underran {c['DAC_UNDERRUN']} word periods (last at cycle "
        f"{c.get('DAC_LAST_UNDERRUN_CYCLE')}); with the buffer primed {DELAY_BLOCKS} blocks ahead it "
        f"should never come due empty.")
    assert c["DAC_BLOCKS_ZERO_FILLED"] == 0, (
        f"the DAC grid emitted {c['DAC_BLOCKS_ZERO_FILLED']} zero blocks")


@pytest.mark.xsi
def test_the_converter_and_not_the_fabric_set_the_dacs_rate(run):
    """The DAC took words at **its own** rate, which is what makes the underrun count above mean
    anything.

    ``0.25`` words per cycle over the run: 15000 words, and one more in flight. Before the model
    back-pressured, this read 20000 — one word every three cycles, which is the *player's*
    ``fire_cycles``, not the converter's grid. A design cannot be shown to feed a converter on time by
    a model that accepts everything the instant it is offered.
    """
    c, _out = run
    words_per_cycle = 0.25
    want = int(60000 * words_per_cycle)
    assert abs(c["DAC_WORDS_RECV"] - want) <= 2, (
        f"the DAC took {c['DAC_WORDS_RECV']} words in 60000 cycles, expected ~{want} at "
        f"{words_per_cycle} words/cycle. If this has drifted toward 60000/fire_cycles the slave has "
        f"stopped back-pressuring and every timing claim in this file is void.")


@pytest.mark.xsi
def test_the_loop_is_bit_exact_through_both_converters(run):
    """Every input block comes back out of the DAC, in order, unchanged.

    Bit-exact rather than close: the ramp sits on the converter's quantization grid, so
    quantize → pack → RX buffer → relay → TX buffer → unpack → dequantize must be the identity. A
    tolerance would hide a packing bug, and packing at four samples per word is a thing this example
    is one of the few places to exercise.
    """
    played = _played()
    shift = _measured_delay(played)
    assert shift is not None, f"the first input block never reached the DAC ({played.size} samples)"
    ramp = ramp_samples()
    n = min(N_BLK * BLKSIZE, played.size - shift)
    assert n >= (N_BLK - 1) * BLKSIZE, f"only {n} samples of the loop came back"
    got, want = played[shift:shift + n], ramp[:n]
    if not np.array_equal(got, want):
        bad = int(np.argmax(got != want))
        raise AssertionError(
            f"played sample {bad} (input index {bad}) is {int(got[bad])}, sent {int(want[bad])}")


@pytest.mark.xsi
def test_the_delay_the_rtl_produced_is_the_one_the_design_asked_for(run):
    """``out_ts − in_ts``, measured off the RTL's own output — **less a fixed startup skew.**

    The ramp makes every sample name its own input index, so where input sample 0 lands in what the
    DAC played *is* the shift. pysim puts it at exactly ``delay_blocks x blksize``; the RTL puts it 64
    samples earlier, and that difference is a property of the converter edge rather than of the loop:

    * it is **constant**, not per-block — the bit-exact comparison above runs off this same measured
      shift and matches every relayed block, so no block is displaced relative to any other;
    * it is a **quarter of a block**, well under the block granularity the design works in, which is
      what makes it a phase offset between the player's sample index and the DAC's block grid rather
      than a delay error;
    * pysim cannot show it at all, because there the player's pointer and the ``RFSampIF`` grid are
      started from one epoch.

    So the RTL evidence is "the right samples, in the right order, one delay-worth late, to within the
    converter's own start-up phase". **The evidence that the delay tracks the parameter is in pysim**,
    where ``test_the_delay_is_what_was_asked_for_at_every_setting`` sweeps it — that sweep needs one
    csynth per setting at RTL, which is not worth a gate.

    The skew is pinned exactly rather than tolerated, so a change in it has to be looked at.
    """
    got = _measured_delay(_played())
    assert got is not None, "the ramp never reached the DAC"
    skew = DELAY_BLOCKS * BLKSIZE - got
    assert skew == RTL_GRID_SKEW, (
        f"the RTL played input sample 0 at output index {got}, i.e. {skew} samples ahead of the "
        f"{DELAY_BLOCKS} x {BLKSIZE} = {DELAY_BLOCKS * BLKSIZE} the design asked for; the recorded "
        f"converter start-up skew is {RTL_GRID_SKEW}. A change here is either a different start-up "
        f"phase or a real delay error — and only the second would also break bit-exactness.")
    assert 0 <= skew < BLKSIZE, (
        f"the skew is {skew}, a whole block or more. At that size it is no longer a start-up phase "
        f"offset; the loop is placing blocks in the wrong slot.")


@pytest.mark.xsi
def test_both_buffers_answered_every_command_and_the_in_band_frame_stayed_aligned(run):
    """One response per block from each buffer, in order, each with its own tid.

    **This is the in-band framing gate**, and the TX half is the sharp one: the payload rides the same
    stream as the command, so a refused command that left its words behind would make the next read
    take a sample for a tid — which shows up here as a wrong tid, not as wrong data.
    """
    rx, tx = _resps("rxresp", RxResp), _resps("txresp", TxResp)
    want = [(k + 1, RF_SAMP_BUF_OK, BLKSIZE) for k in range(WANT_BLOCKS_RELAYED)]
    assert rx == want, rx
    assert tx == want, tx

    c, _out = run
    assert c["RX_RESP_WORDS"] == WANT_BLOCKS_RELAYED * RxResp.nwords_per_inst(WORD_BW)
    assert c["TX_RESP_WORDS"] == WANT_BLOCKS_RELAYED * TxResp.nwords_per_inst(WORD_BW)


@pytest.mark.xsi
def test_the_source_outlives_the_design_by_one_block_and_that_is_why_the_last_one_completes(run):
    """**The trailing-progress-report property, pinned.**

    The RX ingress reports its write pointer with a *non-blocking* write to a depth-1 channel, so
    updates are dropped rather than allowed to stall a converter that cannot be stalled. That is only
    safe while more updates are coming. When the ADC stops, the final report — if it was dropped — is
    never repaired, and the capture waits forever for a window it can no longer prove was written.

    Measured: with the source playing exactly ``N_BLK`` blocks, 11 of 12 commands came back and the
    twelfth hung. ``SRC_NBLK = N_BLK + 1`` makes the scenario resemble a converter that keeps running,
    which is what a real one does. The assertion is that the ADC really did deliver the extra block —
    otherwise this file would be recording a symptom rather than the fix for it.
    """
    c, _out = run
    assert c["ADC_WORDS_SENT"] == SRC_NBLK * BLKSIZE // (WORD_BW // SAMP_BW), (
        f"the ADC delivered {c['ADC_WORDS_SENT']} words, expected {SRC_NBLK} whole blocks")
    assert WANT_BLOCKS_RELAYED > N_BLK, (
        "SRC_NBLK no longer exceeds N_BLK, so the last relayed block has no converter progress "
        "behind it and this design will hang at RTL — see SRC_NBLK.")


@pytest.mark.xsi
def test_neither_memorys_read_during_write_assertion_fired(run):
    """Neither buffer's writer ever wrote the address its reader was reading that cycle.

    Checked by the hand-written memory rather than by us, and it is two memories here: if either
    collided, the data would be whatever that BRAM's read-during-write mode happens to be and nothing
    else in the flow would notice.
    """
    _c, out = run
    assert "read-during-write collision" not in out, (
        f"bram_t2p's assertion fired — a buffer wrote the address being read:\n{out[-3000:]}")


@pytest.mark.xsi
def test_the_completion_cycle_is_the_recorded_one(run):
    """Time to the last TX response — a result, distinct from the run's loop bound."""
    c, _out = run
    assert c["TX_RESP_LAST_CYCLE"] == WANT_TX_RESP_LAST_CYCLE, (
        f"the last TX response landed at cycle {c['TX_RESP_LAST_CYCLE']}, gate expects "
        f"{WANT_TX_RESP_LAST_CYCLE}. That is a real behaviour change: either a regression or an "
        f"improvement worth re-recording.")
    assert c["RX_RESP_LAST_CYCLE"] == WANT_RX_RESP_LAST_CYCLE, (
        f"the last RX response landed at cycle {c['RX_RESP_LAST_CYCLE']}, gate expects "
        f"{WANT_RX_RESP_LAST_CYCLE}")
    assert c["DAC_BLOCKS_OUT"] == WANT_DAC_BLOCKS_OUT, (
        f"the DAC pulled {c['DAC_BLOCKS_OUT']} blocks, gate expects {WANT_DAC_BLOCKS_OUT}")
