"""The pattern-B loop in pysim — ``Rfdc → RfSampBuf(RX) → BlkDelay → RfSampBuf(TX) → Rfdc``.

``plans/adc_model.md`` § *Two design patterns*.  What this example is for is not that samples survive
a round trip — ``rf_loopback`` already shows that — but that they survive it **through two sample
buffers, with a timestamp relationship the design chose**.  So the assertions are about the
relationship, and the strongest of them is measured rather than declared: ``out_ts − in_ts`` is read
off what the DAC actually played.

The counters are the other half.  A loop like this can lose samples at four distinct places — the
ADC's boundary port, the RX capture's horizon, the TX loader's deadline, and the DAC's grid — and a
run that reports zero at all four is only evidence if at least one of them has been driven off zero
somewhere.  ``test_the_delay_floor_is_measured_not_assumed`` does that to ``too_late``, and the
player's ``n_underrun`` is nonzero in every run by construction.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.rf_blk_delay.rf_blk_delay import (
    BLKSIZE,
    DELAY_BLOCKS,
    MIN_DELAY_BLOCKS,
    N_BLK,
    RX_DEPTH,
    SAMP_PER_WORD,
    SAMP_RATE,
    BlkDelay,
    RfBlkDelayTB,
    measured_delay,
    played_samples,
    ramp_samples,
    run_pysim,
    rx_responses,
    tx_responses,
)
from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ
from waveflow.hw.rf_samp_buf import (
    RF_SAMP_BUF_OK,
    RF_SAMP_BUF_TOO_LATE,
    RfSampBufIngress,
    unpack_samples,
)
from waveflow.hw.rf_samp_buf_tx import RfSampBufPlayer
from waveflow.simulation.simulation import Simulation


@pytest.fixture(scope="module")
def tb() -> RfBlkDelayTB:
    """One run, shared: the scenario is fixed and the run is the expensive part."""
    return run_pysim()


# ---------------------------------------------------------------------------
# The delay — the thing this example exists to demonstrate
# ---------------------------------------------------------------------------

def test_the_delay_is_measured_end_to_end_not_asserted(tb):
    """``out_ts − in_ts`` read off the DAC's output, through both converters.

    The source plays a ramp, so every sample names its own input index; finding where input sample 0
    lands in what the DAC played gives the shift directly.  It must be exactly
    ``delay_blocks × blksize`` — no fixed offset, which is the part that would be easy to miss:
    a loop that added a constant converter latency on top would still "have a delay", just not the
    one it was asked for.
    """
    got = measured_delay(tb)
    assert got is not None, "the first input block never reached the DAC"
    assert got == DELAY_BLOCKS * BLKSIZE, (
        f"the DAC played input sample 0 at output index {got}; the design asked for "
        f"{DELAY_BLOCKS} x {BLKSIZE} = {DELAY_BLOCKS * BLKSIZE}")


@pytest.mark.parametrize("delay", [4, 5, 6])
def test_the_delay_is_what_was_asked_for_at_every_setting(delay):
    """Not one lucky value: the measured shift tracks the parameter.

    A design that ignored ``delay_blocks`` and happened to produce four blocks of latency would pass
    the test above and fail here, which is the point of sweeping it.
    """
    tb = run_pysim(tb=RfBlkDelayTB(name=f"d{delay}", sim=Simulation(), delay_blocks=delay))
    assert measured_delay(tb) == delay * BLKSIZE


def test_the_loop_is_bit_exact_through_both_converters(tb):
    """Every input block comes back out, in order, unchanged.

    Bit-exact rather than close: the ramp is drawn on the converter's quantization grid, so
    quantize → pack → RX buffer → BlkDelay → TX buffer → unpack → dequantize must be the identity.
    A tolerance here would hide a packing bug, and packing at four samples per word is a thing this
    example is one of the few places to exercise.
    """
    played = played_samples(tb)
    ramp = ramp_samples()
    shift = measured_delay(tb)
    n = min(N_BLK * BLKSIZE, played.size - shift)
    assert n >= (N_BLK - 1) * BLKSIZE, f"only {n} samples of the loop came back"
    got, want = played[shift:shift + n], ramp[:n]
    if not np.array_equal(got, want):
        bad = int(np.argmax(got != want))
        raise AssertionError(
            f"played sample {bad} (input index {bad}) is {int(got[bad])}, sent {int(want[bad])}")


def test_the_tx_buffer_holds_each_block_at_in_ts_plus_delay(tb):
    """The contract on the buffer itself, independent of the converter's own latency.

    The end-to-end measurement above is the better evidence, but it can only see what played; this
    sees where every block was *placed*, including any the run ended before playing.
    """
    store = np.asarray(tb.tx.mem.storage, dtype=np.uint64)
    ramp = ramp_samples()
    w, spw = int(tb.rfdc.axis_bitwidth), SAMP_PER_WORD
    for k in range(N_BLK):
        in_ts = k * BLKSIZE
        out_ts = in_ts + tb.dut.delay_samples
        addr = (out_ts // spw) % int(tb.tx_depth)
        got = unpack_samples(store[addr:addr + BLKSIZE // spw], w, spw)
        assert np.array_equal(got, ramp[in_ts:in_ts + BLKSIZE]), (
            f"block {k} is not at sample index {out_ts} in the TX buffer")


# ---------------------------------------------------------------------------
# The counters — all four places a loop can lose samples
# ---------------------------------------------------------------------------

def test_nothing_was_lost_anywhere_in_the_loop(tb):
    """The four losses a pattern-B loop can suffer, each asserted at its own boundary.

    They are different failures with different causes, which is why they are four assertions and not
    a single "it worked": an ADC drop is a stalled ingress, a ``too_old`` is a capture that arrived
    after its window was overwritten, a ``too_late`` is a load that missed its slot, and an
    ``overrun`` is a sink that stopped consuming.
    """
    assert tb.adc_axis.dropped == 0, (
        f"the ADC offered {tb.adc_axis.dropped} words the fabric would not take — the RX ingress "
        f"stalled, which its BRAM write is supposed to make impossible")
    assert tb.rx.n_too_old == 0, "a capture window had already been overwritten"
    assert tb.tx.n_too_late == 0, "a load arrived after its slot had played"
    assert tb.tx.n_misaligned == 0, (
        "a window was not word-aligned — a block-granular delay is supposed to make that impossible")
    assert tb.adc_if.counters()["overrun"] == 0 and tb.adc_if.counters()["underrun"] == 0
    assert tb.dac_if.counters()["overrun"] == 0


def test_the_dac_grid_never_underran_once_the_buffer_was_primed(tb):
    """The DAC played continuously: **zero** grid periods with nothing to play.

    This is the pattern-B payoff and it is worth being precise about. In ``rf_loopback`` the DAC
    underruns structurally, because the pass-through hands it blocks only as fast as the loop turns
    them round. Here the TX buffer is primed ``delay_blocks`` ahead, so the player always has the
    slot the grid asks for — and the edge counter is what says so rather than a claim.
    """
    assert tb.dac_if.counters()["underrun"] == 0, (
        f"the DAC grid underran {tb.dac_if.counters()['underrun']} periods; with the buffer primed "
        f"{DELAY_BLOCKS} blocks ahead it should never be short")


def test_every_command_was_answered_with_the_predicted_status(tb):
    """One ``RxResp`` and one ``TxResp`` per block, all OK, tids in order.

    The tids are what makes this more than a count: an in-band frame that slipped would answer with
    somebody else's tid rather than answer fewer times.
    """
    rx, tx = rx_responses(tb), tx_responses(tb)
    assert rx == [(k + 1, RF_SAMP_BUF_OK, BLKSIZE) for k in range(N_BLK)]
    assert tx == [(k + 1, RF_SAMP_BUF_OK, BLKSIZE) for k in range(N_BLK)]


def test_the_player_underran_before_the_buffer_was_primed(tb):
    """A counter that has never counted is not evidence — and this one counts by construction.

    The player is free-running, so it walks slots from *t=0* while the first block is still crossing
    the loop. Those slots are stale and counted. It is the visible half of the never-miss-a-deadline
    contract, and it must not be the whole run.
    """
    assert 0 < tb.tx.n_underrun < tb.tx.n_played, (
        f"underrun {tb.tx.n_underrun} of {tb.tx.n_played} played")


def test_the_delay_floor_is_measured_not_assumed():
    """**The fault injection**, and it drives ``too_late`` off zero.

    Below the floor the TX loader refuses every command: the play pointer reaches the slot while the
    block is still being written into it. The signature is *partial* placement rather than none —
    measured at ``MIN_DELAY_BLOCKS - 1``, every block gets 116 of its 256 samples in before the player
    overtakes it — because the loader keeps draining the frame after the verdict, which is what keeps
    the next command aligned. A refusal that abandoned the payload would desynchronise everything
    after it, so "refused" has to mean "refused and drained".

    That is the contract working, not a tuning inconvenience, and it is what makes
    ``MIN_DELAY_BLOCKS`` a measurement rather than a guess.
    """
    below = run_pysim(tb=RfBlkDelayTB(name="short", sim=Simulation(),
                                      delay_blocks=MIN_DELAY_BLOCKS - 1))
    resp = tx_responses(below)
    assert resp and all(s == RF_SAMP_BUF_TOO_LATE for _t, s, _n in resp), (
        f"a delay of {MIN_DELAY_BLOCKS - 1} blocks was expected to miss every slot, got {resp}")
    assert below.tx.n_too_late == N_BLK
    placed = sum(n for _t, _s, n in resp)
    assert 0 < placed < N_BLK * BLKSIZE, (
        f"{placed} of {N_BLK * BLKSIZE} samples placed; the failure below the floor is a block cut "
        f"short by the player, so it should be neither total nor absent")
    assert all(n < BLKSIZE for _t, _s, n in resp), f"a block was placed in full anyway: {resp}"

    at = run_pysim(tb=RfBlkDelayTB(name="floor", sim=Simulation(),
                                   delay_blocks=MIN_DELAY_BLOCKS))
    assert all(s == RF_SAMP_BUF_OK and n == BLKSIZE for _t, s, n in tx_responses(at))
    assert at.tx.n_too_late == 0


# ---------------------------------------------------------------------------
# The geometry, and why each number is what it is
# ---------------------------------------------------------------------------

def test_the_word_is_exactly_at_the_ceiling(tb):
    """4 samples x 16 bits = 64, which is the widest word ``Rfdc`` accepts — and the reason a real
    1 GSPS RFDC (128 bits or wider) cannot be modelled yet."""
    assert tb.rfdc.axis_bitwidth == 64
    assert SAMP_PER_WORD * 16 == 64


def test_both_halves_rate_checks_are_enforced_and_the_tx_half_is_the_binding_one(tb):
    """**The ceiling of a loop is the slower half, and it is the TX half.**

    The obvious arithmetic divides the port capacity by the *ingress's* ``fire_cycles`` and gets
    500 MSPS. A loop also contains the player, which costs 3 rather than 2 — measured, see
    ``test_rf_samp_buf_fire_cycles.py`` — so the real ceiling is 333 MSPS and 500 would be refused.
    Both checks run in ``__post_init__`` and neither is bypassed.
    """
    f = RFSOC4X2_CLK_HZ
    assert tb.rx.max_samp_rate(f) == f * SAMP_PER_WORD / RfSampBufIngress.fire_cycles
    assert tb.tx.max_samp_rate(f) == f * SAMP_PER_WORD / RfSampBufPlayer.fire_cycles
    assert tb.tx.max_samp_rate(f) < tb.rx.max_samp_rate(f), (
        "the TX half is supposed to be the binding one; if the player got cheaper, this example's "
        "rate can rise and the comment explaining 250 MSPS needs revisiting")
    assert SAMP_RATE <= tb.tx.max_samp_rate(f)
    assert tb.rx_util == pytest.approx(SAMP_RATE / tb.rx.max_samp_rate(f))
    assert tb.tx_util == pytest.approx(SAMP_RATE / tb.tx.max_samp_rate(f))

    # ...and the rate the naive arithmetic suggests really is refused.
    with pytest.raises(ValueError, match="exceeds what the player can sustain"):
        tb.tx.check_rate(f * SAMP_PER_WORD / RfSampBufIngress.fire_cycles, f)


def test_a_block_granular_delay_is_word_aligned_by_construction():
    """Why the delay is in blocks: the TX loader refuses a window that is not a whole number of
    words, and a block is a multiple of ``samp_per_word`` by construction.

    A sample-granular delay would need the loader to unpack, select and re-pack inside a loop that
    must stay cheap — which that module deliberately does not do.
    """
    assert BLKSIZE % SAMP_PER_WORD == 0
    for d in range(1, 8):
        assert (d * BLKSIZE) % SAMP_PER_WORD == 0

    with pytest.raises(ValueError, match="whole number of"):
        BlkDelay(name="odd", sim=Simulation(), bitwidth=64, samp_per_word=4, blksize=250)


def test_a_delay_below_the_arithmetic_floor_is_refused_at_construction():
    """One period for the ADC to finish the block, one for the capture to serve it."""
    with pytest.raises(ValueError, match="at least 2"):
        BlkDelay(name="zero", sim=Simulation(), delay_blocks=1)


# ---------------------------------------------------------------------------
# The declared cost, re-derived from the reports — a number you cannot source is a number to drop
# ---------------------------------------------------------------------------

REPORT_DIR = (Path(__file__).resolve().parents[2] / "examples" / "rf_blk_delay"
              / "rf_blk_delay_proj" / "solution1" / "syn" / "report")
#: The synthesized instantiation, named by its template arguments — the geometry the gate builds.
BLK_DELAY_MOD = f"blk_delay_task_{SAMP_PER_WORD * 16}_{SAMP_PER_WORD}_{BLKSIZE}_{DELAY_BLOCKS}_16"


def _report(module: str):
    from waveflow.utils.csynthparse import module_latency

    if not (REPORT_DIR / f"{module}_csynth.xml").is_file():
        pytest.skip(f"no csynth report at {REPORT_DIR / (module + '_csynth.xml')} — "
                    f"run rf_blk_delay_build.py --through csynth")
    return module_latency(REPORT_DIR, module)


def _relay_loop_module() -> str:
    """The relay loop's report name, **found rather than spelled out**.

    Vitis names a pipelined loop's report after the SOURCE LINE it sits on
    (``..._Pipeline_VITIS_LOOP_97_1``), so editing a comment above the loop renames the artifact. A
    hard-coded name does not fail when that happens — it stops matching, and the test *skips*, which
    reads as a pass in a summary line. Found by glob, with exactly-one asserted, so a rename is
    invisible and a genuinely missing report is loud.
    """
    hits = sorted(REPORT_DIR.glob(f"{BLK_DELAY_MOD}_Pipeline_VITIS_LOOP_*_csynth.xml"))
    if not hits:
        pytest.skip(f"no csynth reports under {REPORT_DIR} — run rf_blk_delay_build.py "
                    f"--through csynth")
    assert len(hits) == 1, (
        f"expected exactly one pipelined loop in blk_delay_task, found {[h.name for h in hits]}. "
        f"The body has one loop; more than one means it was restructured and this test is now "
        f"measuring something other than the relay.")
    return hits[0].name[: -len("_csynth.xml")]


def test_the_relay_loop_reaches_one_word_per_cycle():
    """``word_cycles = 1`` is the **achieved** II of the relay loop, not the pragma's target.

    A per-word cost read off the target rather than the achievement is a wish. This one is met — a
    stream-to-stream copy with no memory in the path is the shape that reaches II=1 — which is why
    ``BlkDelay`` is the cheapest task in the loop and pattern B's user block never has to think about
    rate.
    """
    lat = _report(_relay_loop_module())
    assert lat is not None, "Vitis could not bound the relay loop; its per-word cost is not sourced"
    iters = BLKSIZE // SAMP_PER_WORD
    ii = lat["interval_max"] / iters
    assert ii == pytest.approx(BlkDelay.word_cycles, abs=0.05), (
        f"the relay loop achieved II={ii:g} over {iters} iterations "
        f"(interval {lat['interval_max']}), but BlkDelay declares word_cycles="
        f"{BlkDelay.word_cycles}. Fix the declaration, not this test.")


def test_the_fixed_per_firing_cost_is_what_csynth_measured():
    """``fire_overhead`` = firing latency − ``word_cycles`` × words, from the report.

    Measured rather than estimated, and checkable, because the pysim twin charges it: a body that
    relays a burst and pays nothing for it cannot report a rate at all.
    """
    lat = _report(f"{BLK_DELAY_MOD}_s")
    assert lat is not None, (
        "Vitis reports the firing as unbounded, so BlkDelay cannot declare a fixed cost — drop "
        "fire_overhead rather than keep a number nothing sources.")
    words = BLKSIZE // SAMP_PER_WORD
    overhead = lat["latency_max"] - words * BlkDelay.word_cycles
    assert overhead == BlkDelay.fire_overhead, (
        f"csynth reports latency {lat['latency_max']} for a {words}-word firing, so the fixed cost is "
        f"{overhead}; BlkDelay declares fire_overhead={BlkDelay.fire_overhead}.")


def test_the_user_block_is_cheaper_than_both_buffers_which_is_the_point(tb):
    """Pattern B's claim, as arithmetic: the relay is not the bottleneck.

    Per word the ingress costs 2 cycles and the player 3 (measured, see
    ``test_rf_samp_buf_fire_cycles.py``); this relays at 1 plus a fixed 3 per block. If that ever
    stops being true the loop's ceiling is no longer the TX half and ``SAMP_RATE``'s justification
    needs rewriting.
    """
    per_word = BlkDelay.word_cycles + BlkDelay.fire_overhead / (BLKSIZE / SAMP_PER_WORD)
    assert per_word < RfSampBufIngress.fire_cycles
    assert per_word < RfSampBufPlayer.fire_cycles


def test_the_rx_buffer_holds_more_history_than_the_delay_needs(tb):
    """The capture must still find block *k* when ``BlkDelay`` asks for it.

    ``RX_DEPTH`` words is ``RX_DEPTH * samp_per_word`` samples of history; the delay costs
    ``delay_blocks`` blocks of it. Asserting the inequality is what turns "1024 looked fine" into a
    statement that fails if either number moves.
    """
    history_samples = RX_DEPTH * SAMP_PER_WORD
    assert history_samples > DELAY_BLOCKS * BLKSIZE * 2, (
        f"the RX buffer holds {history_samples} samples of history against a "
        f"{DELAY_BLOCKS * BLKSIZE}-sample delay; that is not enough margin to be sure a capture is "
        f"served before its window is overwritten")
    assert tb.rx.nsamp_held == history_samples
