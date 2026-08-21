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
    RfSampBufCapture,
    RF_SAMP_BUF_OK,
    RF_SAMP_BUF_TOO_LATE,
    RfSampBufIngress,
    unpack_samples,
)
from waveflow.hw.rf_samp_buf_tx import RfSampBufLoader, RfSampBufPlayer
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
    late = [s for _t, s, _n in resp if s == RF_SAMP_BUF_TOO_LATE]
    # SOME miss, not all: one block below the floor the loop is marginal rather than hopeless, and
    # how many miss depends on how far below.  Measured at MIN_DELAY_BLOCKS - 1 = 5: five of twelve.
    # Asserting "all" would pin a number that is a property of how deep the sweep went.
    assert late, (
        f"a delay of {MIN_DELAY_BLOCKS - 1} blocks was expected to miss at least one slot, got {resp}")
    assert below.tx.n_too_late == len(late)
    placed = sum(n for _t, _s, n in resp)
    assert 0 < placed < N_BLK * BLKSIZE, (
        f"{placed} of {N_BLK * BLKSIZE} samples placed; the failure below the floor is a block cut "
        f"short by the player, so it should be neither total nor absent")
    # **A refusal is a block cut short, never a block silently dropped or silently completed**, and
    # that is the invariant worth pinning rather than the shape of the shortfall.  The shape depends
    # on the rate: at 250 MSa/s one below the floor the refusals alternate 244 / 0, at 400 MSa/s they
    # degrade 232 / 176 / 152 / ... as the loop falls further behind.  Asserting either pattern would
    # pin a number that belongs to a configuration rather than to the contract.
    refused = [n for _t, s, n in resp if s == RF_SAMP_BUF_TOO_LATE]
    assert all(0 <= n < BLKSIZE for n in refused), (
        f"a refused command placed a FULL block: {resp}. Refused means the player reached the slot "
        f"first, so it cannot have taken everything.")
    assert any(n > 0 for n in refused), (
        f"every refused command placed nothing: {resp}. The loader is supposed to keep draining the "
        f"frame after the verdict, so at least a partial block should land -- all-or-nothing here "
        f"would mean the drain-after-refusal path is gone, and the next command would desynchronise.")

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


def test_the_loop_ceiling_is_the_slowest_of_all_four_stages(tb):
    """**The ceiling of a loop is its slowest STAGE, not its slowest half — and not a boundary task.**

    This test has now been wrong twice in the same way, so the shape of the error is worth naming.
    It first said the ceiling was the ingress's; then that it was the player's, because the player
    cost 3 cycles per word against the ingress's 2. Both readings looked only at the tasks that touch
    a converter, and both missed the point: **every sample crosses all four stages**, so the loop
    sustains what the slowest one does.

    "The loader and capture may block freely" is a *safety* property — they are allowed to stall
    because nothing upstream loses data while they wait. It is not a throughput exemption, and
    reading it as one is what hid the real ceiling.

    Measured per-word costs (achieved ``PipelineII``, see ``test_rf_samp_buf_cycles_per_word.py``):
    ingress 1, loader 1, player 1, **capture 2**. So the RX half binds, at ``spw * f_axis / 2``.
    """
    f = RFSOC4X2_CLK_HZ
    port = f * SAMP_PER_WORD

    # Each half's ceiling is its own slowest stage...
    assert tb.rx.cycles_per_word == max(RfSampBufIngress.cycles_per_word,
                                        RfSampBufCapture.cycles_per_word)
    assert tb.tx.cycles_per_word == max(RfSampBufPlayer.cycles_per_word,
                                        RfSampBufLoader.word_cycles)
    assert tb.rx.max_samp_rate(f) == port / tb.rx.cycles_per_word
    assert tb.tx.max_samp_rate(f) == port / tb.tx.cycles_per_word

    # ...and TWO stages sit at 2, so either half alone would set the same ceiling.  That is worth
    # asserting rather than picking a "binding half": if one of them is fixed, the ceiling does NOT
    # move, and a test that named a single culprit would suggest otherwise.
    loop_ceiling = min(tb.rx.max_samp_rate(f), tb.tx.max_samp_rate(f))
    slow = [n for n, c in (("capture", RfSampBufCapture.cycles_per_word),
                           ("loader", RfSampBufLoader.word_cycles),
                           ("ingress", RfSampBufIngress.cycles_per_word),
                           ("player", RfSampBufPlayer.cycles_per_word)) if c == 2]
    assert sorted(slow) == ["capture", "loader"], (
        f"the stages at 2 cycles/word are {slow}; SAMP_RATE's justification names capture and loader "
        f"and needs rewriting if that changed")
    assert loop_ceiling == tb.rx.max_samp_rate(f) == tb.tx.max_samp_rate(f) == port / 2

    # The shipped rate sits inside it, with margin, and BOTH checks run un-bypassed.
    assert SAMP_RATE <= loop_ceiling
    assert tb.rx_util == pytest.approx(SAMP_RATE / tb.rx.max_samp_rate(f))
    assert tb.tx_util == pytest.approx(SAMP_RATE / tb.tx.max_samp_rate(f))

    # And the port's own rate -- what a reader would assume the loop can take now that three of the
    # four stages are at II=1 -- is refused.
    with pytest.raises(ValueError, match="exceeds what the ingress can absorb"):
        tb.rx.check_rate(port, f)


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


def test_the_user_block_is_not_what_binds_the_loop(tb):
    """Pattern B's claim, as arithmetic: **the user's relay is not the bottleneck.**

    The comparison had to change when the buffers did.  It used to be "cheaper than both buffers",
    which was easy when they cost 2 and 3 cycles per word; three of the four stages are now at 1, and
    this relay costs ``1 + 3/64`` = 1.047, so it is no longer cheaper than *every* stage.  What still
    holds — and is the claim pattern B actually makes — is that it is not the one that BINDS.

    If that ever stops being true, the loop's ceiling becomes a property of the user's code rather
    than of the buffers, which is precisely the situation pattern B exists to prevent.
    """
    per_word = BlkDelay.word_cycles + BlkDelay.fire_overhead / (BLKSIZE / SAMP_PER_WORD)
    binding = max(RfSampBufIngress.cycles_per_word, RfSampBufCapture.cycles_per_word,
                  RfSampBufLoader.word_cycles, RfSampBufPlayer.cycles_per_word)
    assert per_word < binding, (
        f"the relay costs {per_word:.3f} cycles/word and the slowest buffer stage {binding}; the "
        f"user's block has become the loop's bottleneck, which pattern B exists to prevent")


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
