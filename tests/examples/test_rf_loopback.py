"""The stage-1 RF gates (``plans/adc_model.md`` staging item 1), on the ``rf_loopback`` example.

Four claims, in the order they matter:

1. a **byte-identical** loopback through source → ``Rfdc`` → DUT → ``Rfdc`` → sink;
2. ``underrun == 0 and overrun == 0`` on that clean run;
3. both counters **non-vacuous** — a deliberately starved producer and a deliberately stalled
   consumer, each asserted against a predicted count;
4. the component-kind finding: an RF-environment node declares neither realization hook, so
   ``check(mod, "xsi_bfm_model")`` is ``False`` and says why.

The metronome half of the stage-1 gate lives in ``tests/hw/test_rf_sample_if.py``, with the edge it
belongs to.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from waveflow.build.codegen_check import check
from waveflow.hw.arrayutils import array
from waveflow.simulation.rf_tb import RfDataSink, RfDataSource, read_rf_bundle, write_rf_bundle
from waveflow.hw.fixpoint import from_real, to_real
from waveflow.simulation.simulation import Simulation

from examples.rf_loopback.rf_loopback import RfLoopbackSim, RfLoopbackTB, RfSampPassThrough
from examples.rf_loopback.rfdc import Rfdc
from waveflow.hw.rfdc_samp_word import RfdcSampWord, Rfsoc4x2SampWord as WORD, pack, unpack


# --------------------------------------------------------------------------------------------
# Gates 1 and 2 — byte-identical loopback, zero loss
# --------------------------------------------------------------------------------------------

class TestLoopback:

    def test_loopback_is_byte_identical_with_no_loss(self):
        sim = RfLoopbackSim(n_src_blk=8)
        sim.run()
        tb = sim.check()                        # data, counters, block counts and alignment
        assert tb.adc_if.counters() == {"blocks_sent": 8, "blocks_delivered": 8,
                                        "underrun": 0, "overrun": 0}
        # The DAC edge underruns exactly TWICE: one block for the ADC hop (a converter cannot emit
        # samples it has not collected, so a block is transmitted across the period AFTER its grid
        # tick) and one for the DUT.  That is the fidelity contract's "one block per converter hop",
        # and the ADC term was invisible until the transfer was paced by the converter's own rate
        # instead of the fabric clock.
        assert tb.dac_if.counters() == {"blocks_sent": 8, "blocks_delivered": 8,
                                        "underrun": 2, "overrun": 0}
        assert tb.dac_if.last_underrun_idx == 2         # the transient, and nothing after it
        blk_bytes = tb.blksize * 8                      # n_ch=1, float64
        assert sim._out_bytes[2 * blk_bytes:] == sim._in_bytes[:-2 * blk_bytes]

    @pytest.mark.parametrize("nbits,samp_per_word", [(8, 8), (16, 4), (12, 4), (16, 2)])
    def test_bit_widths_round_trip_exactly(self, nbits, samp_per_word):
        """The point of a bit-exact converter model: change the width, the answer stays exact.

        Quantization runs through the integer-backed ``FixedField`` and packing through the generated
        array serializers, so the degenerate widths that break hand-rolled ``.range()`` packing are
        covered by construction rather than by hope.

        **The sample rate is scaled with the word, and that is not cosmetic.**  This pass-through
        reads a whole block and then writes it, so it occupies the port for *twice* its utilisation
        ``samp_rate / (samp_per_word * f_axis)`` — and it declares ``blk_latency = 1``, which only
        holds while that stays under one block period.  Held at a fixed rate, the narrow-word cases
        drift over that line as the fabric slows: at 250 MHz, ``(16, 2)`` at 256 MSa/s is 0.512
        utilisation, so the read-then-write costs 1.02 block periods and the DAC edge underruns a
        third time.  Scaling the rate keeps every geometry at the same utilisation, so the sweep
        measures **packing**, which is what it is for, rather than a timing accident of one width.
        (That the line exists at all is pattern A's cost, and the reason ``RfSampBuf`` exists.)
        """
        # 64 MSa/s per sample-per-word -> utilisation 0.256 at every geometry, comfortably inside
        # the pass-through's one-block budget.
        samp_rate = 64e6 * samp_per_word
        # A geometry is a WORD, and here effective == container: the sweep is about packing, and
        # a separated effective width is the next commit's subject, tested where it belongs.
        sim = RfLoopbackSim(n_src_blk=4, blksize=64, samp_rate=samp_rate,
                            word=RfdcSampWord.specialize(samp_per_word=samp_per_word,
                                                         bits_per_samp=nbits,
                                                         bits_per_samp_pack=nbits))
        sim.run()
        sim.check()

    def test_a_non_unit_full_scale_still_round_trips(self):
        sim = RfLoopbackSim(n_src_blk=4, full_scale=0.5, blksize=64)
        sim.run()
        sim.check()

    def test_the_axis_word_is_samp_per_word_times_nbits(self):
        tb = RfLoopbackTB(name="w", sim=Simulation(), blksize=64, n_blk=1,
                          word=RfdcSampWord.specialize(samp_per_word=4, bits_per_samp=12,
                                                       bits_per_samp_pack=12))
        assert tb.rfdc.axis_bitwidth == 48
        assert tb.dut.s_in.bitwidth == 48
        assert tb.nwords_blk == 16                      # blksize / samp_per_word

    def test_samples_land_on_the_grid_t0_defines(self):
        """Alignment is arithmetic on ``t0`` and the rate, not an artifact of the run.

        Both tiles share one epoch -- what MTS gives you -- so the grids are aligned.  The loop's
        one-block cost is a separate quantity (``loop_blk_latency``) and is deliberately NOT paid by
        staggering an epoch: which block a tick carries is not the same question as when it ticks.
        """
        sim = RfLoopbackSim(n_src_blk=4, blksize=64)
        sim.run()
        tb = sim.check()
        assert tb.dac_if.t0 == pytest.approx(tb.adc_if.t0)
        assert tb.adc_if.samp_time(128) == pytest.approx(128 / tb.samp_rate)
        assert tb.dac_if.samp_time(128) == pytest.approx(128 / tb.samp_rate)
        # ...and the loop still costs whole block indices, carried where they belong: one per
        # converter hop plus the DUT's own.
        assert tb.loop_blk_latency == 2


# --------------------------------------------------------------------------------------------
# The tile — Stage A's gate (plans/adc_model.md, "Stage A — the tile")
# --------------------------------------------------------------------------------------------

class TestTheTile:
    """``Rfdc`` presents one AXIS port per channel, and a two-channel loopback is byte-identical.

    The claim that needs a *second* channel is not "does it still work" — it is that **row ``ch`` of
    the block is port ``ch``** all the way through: quantize, pack, offer, relay, take, unpack,
    dequantize, and out to the bundle. One channel cannot state that at all, because every wrong
    answer (a transpose, a swap, a broadcast) agrees with the right one when there is only one row.

    So the scenarios here are deliberately **asymmetric across channels** — different random draws in
    the grid waveform, different harmonics in the sine — and the checks below are the ones that fail
    on a swap rather than passing on symmetry.
    """

    def test_the_converter_presents_one_axis_port_per_channel(self):
        rfdc = Rfdc(name="tile", sim=Simulation(), n_rx=2, n_tx=3)
        assert len(rfdc.rx_streams) == 2 and len(rfdc.tx_streams) == 3
        # The list and the indexed attributes are two views of ONE set of objects -- BfmModel.ports
        # names endpoints by attribute, and `rx_streams[0]` is not an attribute name.
        assert rfdc.rx_stream_0 is rfdc.rx_streams[0]
        assert rfdc.tx_stream_2 is rfdc.tx_streams[2]
        # ...and every one of them is on the module's surface, or nothing could resolve it.
        registered = {id(e) for e in rfdc.endpoints.values()}
        assert all(id(e) in registered for e in (*rfdc.rx_streams, *rfdc.tx_streams))
        # Indexed even at one channel: one spelling, no special case for the count everybody tests.
        assert Rfdc(name="one", sim=Simulation()).rx_stream_0 is not None

    def test_a_two_channel_loopback_is_byte_identical_with_no_loss(self):
        """Stage A's gate: both channels through, both edges clean, the same declared transient."""
        sim = RfLoopbackSim(n_src_blk=8, n_ch=2)
        sim.run()
        tb = sim.check()                        # data, counters, block counts and alignment
        assert tb.rfdc.n_rx == 2 and tb.rfdc.n_tx == 2
        assert tb.adc_if.counters() == {"blocks_sent": 8, "blocks_delivered": 8,
                                        "underrun": 0, "overrun": 0}
        # The SAME transient as at one channel, and that is the result: the lanes run concurrently,
        # so a second channel costs block indices only if the model serialized them -- which is
        # exactly the mistake a `for` loop over `offer()` would have made.
        assert tb.dac_if.counters() == {"blocks_sent": 8, "blocks_delivered": 8,
                                        "underrun": 2, "overrun": 0}
        assert tb.dac_if.last_underrun_idx == 2
        blk_bytes = 2 * tb.blksize * 8          # n_ch=2, float64
        assert sim._out_bytes[2 * blk_bytes:] == sim._in_bytes[:-2 * blk_bytes]

    def test_the_two_channels_carry_different_data_and_do_not_get_swapped(self):
        """The check one channel cannot make.

        Every block is compared row by row against the row that was sent, so a swapped pair fails
        here even though it would be byte-identical in total.
        """
        sim = RfLoopbackSim(n_src_blk=6, n_ch=2, blksize=64)
        sim.run()
        tb = sim.check()
        lat = int(tb.loop_blk_latency)
        for k in range(lat, 6):
            sent, got = sim.sent[k - lat], sim.captured[k]
            assert got.shape == (2, 64)
            assert not np.array_equal(sent[0], sent[1]), "the scenario must be asymmetric"
            assert np.array_equal(got[0], sent[0]), f"block {k} channel 0"
            assert np.array_equal(got[1], sent[1]), f"block {k} channel 1"
            # ...and the swap really would be caught: the rows are not interchangeable.
            assert not np.array_equal(got[0], sent[1])

    def test_a_two_channel_sine_exercises_the_quantizer_on_both_lanes(self):
        """Different harmonics per lane, so neither channel's golden is the other's."""
        sim = RfLoopbackSim(n_src_blk=8, n_ch=2, waveform="sine")
        tb = sim.run()
        lat = int(tb.loop_blk_latency)
        tb.adc_if.assert_clean()
        tb.dac_if.assert_clean(startup_blocks=lat)
        fs = float(tb.full_scale)
        for k in range(lat, 8):
            want = to_real(from_real(np.asarray(sim.sent[k - lat]) / fs, tb.rfdc.SampType)) * fs
            assert np.array_equal(sim.captured[k], want), f"block {k}"

    def test_the_two_channel_dut_is_a_top_of_its_own(self):
        """A second channel is a different RTL module, so it gets a different name.

        That is what keeps ``rf_pass_through`` — its project, its ports header and every cycle count
        recorded against them — untouched by this stage.
        """
        from examples.rf_loopback.rf_loopback import RfSampPassThrough2Ch

        one = RfSampPassThrough(name="one", sim=Simulation())
        two = RfSampPassThrough2Ch(name="two", sim=Simulation())
        assert one.cpp_kernel_name == "rf_pass_through"
        assert two.cpp_kernel_name == "rf_pass_through_2ch"
        # Unsuffixed at one channel: these are RTL port names, and renaming them re-synthesizes a
        # design that did not change.
        def names(mod):
            return [e[0] if isinstance(e, tuple) else e for e in mod.boundary]

        assert names(one) == ["s_in", "s_out"]
        assert names(two) == ["s_in_0", "s_out_0", "s_in_1", "s_out_1"]
        assert check(RfSampPassThrough2Ch, "composite_kernel")[0]

    def test_the_two_channel_graph_lowers_to_one_model_per_direction(self):
        """One model spanning BOTH AXIS ports plus the RF edge — not one model per port.

        Forced rather than chosen: the RF edge carries every channel in one block, so ``n_ch``
        independent models cannot each own it. The AXIS ports are therefore a port *group*, one
        constructor argument, and the C++ model takes an ``AxisPortList``.
        """
        from waveflow.build.composite_gen import tb_top_spec

        tb = RfLoopbackTB(name="two_ch_tb", sim=Simulation(), n_ch=2, blksize=64, n_blk=2)
        by = {m.cls: m for m in tb_top_spec(tb).models}
        ns = "rf_pass_through_2ch_ports"
        assert by["RfdcAdcMaster"].binds == (
            "sim.dut()", f"{{{ns}::s_in_0, {ns}::s_in_1}}", "two_ch_tb_adc_if")
        assert by["RfdcDacSlave"].binds == (
            "sim.dut()", f"{{{ns}::s_out_0, {ns}::s_out_1}}", "two_ch_tb_dac_if")
        # blk_samples is the WHOLE block -- n_ch * blksize -- because that is the unit the RF edge
        # moves; the model divides it by its own port count.
        assert by["RfdcDacSlave"].args[-1] == str(2 * 64)

    def test_one_channel_still_renders_a_bare_port_name(self):
        """A group of one is the bare argument, so a one-channel harness is unchanged.

        Not cosmetic: every committed RF testbench names a single converter port, and rendering the
        braced form would restate all of them for a design whose shape did not change.
        """
        from waveflow.build.composite_gen import tb_top_spec

        tb = RfLoopbackTB(name="one_ch_tb", sim=Simulation(), blksize=64, n_blk=2)
        by = {m.cls: m for m in tb_top_spec(tb).models}
        assert by["RfdcAdcMaster"].binds == (
            "sim.dut()", "rf_pass_through_ports::s_in", "one_ch_tb_adc_if")


# --------------------------------------------------------------------------------------------
# Gate 3 — the counters, non-vacuous
# --------------------------------------------------------------------------------------------

class TestSineWaveform:
    """The windowed-sinusoid scenario — and why it is not just a prettier picture.

    ``_grid_blocks`` draws samples exactly on the quantization grid so the loopback is bit-identical
    to its input.  That makes the packing check strict, but it also makes ``from_real`` a **no-op**:
    rounding and saturation are never exercised by that waveform at all.  A sine does not land on the
    grid, so it covers the path the grid scenario skips — while keeping an EXACT golden, just stated
    against the quantized input rather than the raw one.
    """

    def _quantized(self, tb, blk):
        fs = float(tb.full_scale)
        return to_real(from_real(np.asarray(blk) / fs, tb.rfdc.SampType)) * fs

    def test_the_sine_loopback_is_exact_against_the_quantized_input(self):
        sim = RfLoopbackSim(n_src_blk=8, waveform="sine")
        tb = sim.run()
        lat = int(tb.loop_blk_latency)
        tb.adc_if.assert_clean()
        tb.dac_if.assert_clean(startup_blocks=lat)
        for k in range(sim.n_src_blk - lat):
            assert np.array_equal(sim.captured[k + lat], self._quantized(tb, sim.sent[k])), (
                f"block {k} does not survive the loopback")

    def test_the_sine_actually_exercises_the_quantizer(self):
        """The claim above, made non-vacuous.

        If the sine happened to land on the grid this test would pass trivially and prove nothing --
        so assert the opposite of what the GRID scenario guarantees: quantization must CHANGE the
        samples.  It is the difference between the two waveforms that is the point.
        """
        sine = RfLoopbackSim(n_src_blk=4, blksize=64, waveform="sine")
        sine.write_scenario(tempfile.mkdtemp())
        assert not np.array_equal(sine.sent[2], self._quantized(sine.tb, sine.sent[2])), (
            "the sine landed on the quantization grid, so it tests nothing the grid waveform does not")

        grid = RfLoopbackSim(n_src_blk=4, blksize=64, waveform="grid")
        grid.write_scenario(tempfile.mkdtemp())
        assert np.array_equal(grid.sent[2], self._quantized(grid.tb, grid.sent[2])), (
            "the grid waveform is supposed to be a no-op through the quantizer")

    def test_the_figure_script_renders_from_a_real_run(self):
        """The committed SVG is an output of the model, not a drawing.  If this import-and-render
        breaks, the figure in the guide has silently stopped matching what the example does."""
        from examples.rf_loopback.rf_loopback_figures import render
        out = Path(tempfile.mkdtemp()) / "fig.svg"
        assert render(out).exists() and out.stat().st_size > 1000


class TestCountersAreNonVacuous:
    """Each counter is driven off zero by a fault whose count is predicted, not observed.

    Both faults are *silent* without the counters, which is the whole reason they exist: a starved
    grid emits well-formed zero blocks and a stalled consumer simply sees fewer of them.  Every
    functional check downstream still passes on the data that did arrive.
    """

    def test_a_late_producer_underruns_by_the_periods_it_missed(self):
        """The source starts 2.5 block periods late, so periods 1 and 2 have nothing to send."""
        sim = RfLoopbackSim(n_src_blk=8, blksize=64)
        sim.tb.source.start_delay = 2.5 * sim.tb.blk_period
        sim.run()
        tb = sim.tb

        assert tb.adc_if.underrun == 2                  # exactly the two missed periods
        assert tb.adc_if.overrun == 0
        assert tb.adc_if.blocks_sent == 8
        # ...and the padding is visible in the RF output at the far end of the loopback.
        # ...and the padding is visible in the RF output at the far end of the loopback, one block
        # later than it happened: the DAC's own startup zero comes first, then the two the ADC
        # zero-filled, then real data.
        captured = sim.captured
        z = np.zeros((1, tb.blksize))
        for k in range(4):                               # 2 structural + 2 the ADC zero-filled
            assert np.array_equal(captured[k], z), f"block {k} should be zero-fill"
        assert np.array_equal(captured[4], sim.sent[0])  # real data resumes, from source block 0
        # The clean-run gate would have caught this; that is the point of asserting it every time.
        with pytest.raises(AssertionError, match="underrun=2"):
            tb.adc_if.assert_clean()

    def test_a_stalled_consumer_overruns_by_everything_past_its_queue(self):
        """The sink takes one block and stops; its queue then holds ``depth`` more and the rest drop.

        Predicted: ``n_blk - 1 - depth`` = ``8 - 1 - 2`` = 5 dropped.
        """
        sim = RfLoopbackSim(n_src_blk=8, blksize=64, sink_stall_after=1, sink_depth=2)
        sim.run()
        tb = sim.tb

        assert tb.dac_if.overrun == 5
        assert tb.dac_if.blocks_delivered == 3          # 1 consumed + 2 sitting in the queue
        assert tb.dac_if.blocks_sent == 8
        assert tb.dac_if.blocks_sent == tb.dac_if.blocks_delivered + tb.dac_if.overrun
        assert tb.dac_if.underrun == 2   # its structural startup blocks, nothing more
        assert len(tb.sink.blocks) == 1
        with pytest.raises(AssertionError, match="overrun=5"):
            tb.dac_if.assert_clean()

    def test_a_deeper_receiver_queue_drops_fewer(self):
        """The prediction tracks the depth, so the count is a model of the buffer, not a constant."""
        sim = RfLoopbackSim(n_src_blk=8, blksize=64, sink_stall_after=1, sink_depth=4)
        sim.run()
        assert sim.tb.dac_if.overrun == 8 - 1 - 4


# --------------------------------------------------------------------------------------------
# Gate 5 — the component-kind finding
# --------------------------------------------------------------------------------------------

def _bound_rfdc():
    """An ``Rfdc`` inside a built graph — ``bfm_model()`` reads its rates off the bound clocks."""
    return RfLoopbackTB(name="k", sim=Simulation()).rfdc


class TestKinds:
    """``check`` answers per module; a pysim-only node is a **finding**, not a declaration."""

    @pytest.mark.parametrize("mod", [RfDataSource, RfDataSink, Rfdc])
    def test_rf_environment_nodes_now_declare_their_models(self, mod):
        """**Inverted deliberately.** This used to assert the opposite, and that was correct then.

        At stage 1 none of these had a C++ realization, and ``check`` reporting ``False`` was the
        *finding* — the third row of the kinds table, a node that exists in the Python graph and
        nowhere else. They now have one, so the finding has changed and the assertion follows it.
        A module acquires a hook when somebody writes one; nothing else about it changed.
        """
        from waveflow.hw.hw_module import declares_hook

        assert declares_hook(mod, "bfm_model"), f"{mod.__name__} should now name a C++ model"

    def test_the_converter_declares_one_model_per_data_path(self):
        """Two, not one: the classes differ per path, which a single declaration cannot express."""
        from waveflow.build.composite_gen import bfm_models

        rfdc = _bound_rfdc()
        assert [m.cls for m in bfm_models(rfdc)] == ["RfdcAdcMaster", "RfdcDacSlave"]

    def test_a_module_with_neither_hook_is_still_a_finding(self):
        """The row itself is not retired — only these modules moved off it.

        Kept pointed at something real so the kinds table keeps a live example: the DUT declares no
        ``bfm_model()`` because it belongs *inside* the cut.
        """
        ok, msg = check(RfSampPassThrough, "xsi_bfm_model")
        assert ok is False
        assert "declares no bfm_model()" in msg
        assert "kernel_task" in msg or "outside" in msg

    def test_an_rf_environment_node_claims_no_codegen_target(self):
        """Neither hook is not an error — it is the third row of the kinds table."""
        from waveflow.build.codegen_check import potential_targets
        assert potential_targets(RfDataSource) == frozenset()
        assert potential_targets(RfDataSink) == frozenset()
        assert potential_targets(Rfdc) == frozenset()

    def test_the_digital_logic_does_claim_one(self):
        """The contrast that makes the finding meaningful: the DUT is a free-running kernel."""
        from waveflow.build.codegen_check import potential_targets
        assert "composite_kernel" in potential_targets(RfSampPassThrough)


# --------------------------------------------------------------------------------------------
# The bundle: one on-disk source, both directions
# --------------------------------------------------------------------------------------------

class TestRfBundle:

    def test_blocks_round_trip_through_a_bundle_bit_exactly(self, tmp_path):
        blocks = [np.array([[1.5, -0.25, 0.0, 3.75]]), np.array([[-1.0, 2.5, 0.125, -0.5]])]
        write_rf_bundle(blocks, tmp_path / "b")
        got = read_rf_bundle(tmp_path / "b", n_ch=1, blksize=4)
        assert len(got) == 2
        assert all(np.array_equal(a, b) for a, b in zip(blocks, got))

    def test_multichannel_blocks_keep_their_shape(self, tmp_path):
        blk = np.arange(12.0).reshape(3, 4)
        write_rf_bundle([blk], tmp_path / "b")
        assert np.array_equal(read_rf_bundle(tmp_path / "b", n_ch=3, blksize=4)[0], blk)

    def test_a_bundle_written_for_another_blksize_is_refused_not_reshaped(self, tmp_path):
        write_rf_bundle([np.zeros((1, 4))], tmp_path / "b")
        with pytest.raises(ValueError, match="words but the interface carries"):
            read_rf_bundle(tmp_path / "b", n_ch=1, blksize=8)

    def test_the_source_refuses_to_run_without_a_bundle(self):
        sim = RfLoopbackSim(n_src_blk=2, blksize=32)
        sim.tb.source.in_bundle = ""
        with pytest.raises(ValueError, match="in_bundle is not set"):
            sim.tb.sim.run_sim()


# --------------------------------------------------------------------------------------------
# The converter's own guards
# --------------------------------------------------------------------------------------------

class TestRfdcGuards:

    def test_a_sample_rate_the_axis_port_cannot_carry_fails_loud(self):
        """A rate ratio above 1 is a design error, not something to simulate."""
        sim = RfLoopbackSim(n_src_blk=2, blksize=64, samp_rate=1.6e9,
                            word=WORD.specialize(samp_per_word=4),
                            axis_freq=300e6)            # 1.6e9 > 4 * 300e6
        with pytest.raises(ValueError, match="exceeds what the AXIS port can carry"):
            sim.run()

    def test_a_blksize_that_splits_a_sample_across_words_fails_loud(self):
        sim = RfLoopbackSim(n_src_blk=2, blksize=66, word=WORD.specialize(samp_per_word=4))
        with pytest.raises(ValueError, match="not a multiple of samp_per_word"):
            sim.run()

    def test_the_channel_count_is_one_number_and_the_edge_must_agree(self):
        """``n_rx`` and the RF edge's ``n_ch`` are the same quantity stated twice.

        Row ``ch`` of the block is what port ``ch`` carries, so a disagreement is not a mismatch to
        broadcast over — it is one of the two declarations being wrong. Caught at bind, where both
        numbers are first in the same room, rather than as a shape error inside ``pack``.
        """
        from waveflow.hw.rf_sample_if import RFSampIF
        from waveflow.hw.clock import Clock

        sim = Simulation()
        rfdc = Rfdc(name="r", sim=sim, n_rx=2, n_tx=2)
        iface = RFSampIF(name="edge", sim=sim, samp_clk=Clock(name="c", freq=1e6),
                         n_ch=1, blksize=8, n_blk=1)
        with pytest.raises(ValueError, match="carries 1 channel"):
            iface.bind("rx", rfdc.rx_rf)

    def test_interleaved_iq_is_refused_at_stage_1(self):
        """The WORD can already say "interleaved"; the CONVERTER still cannot do it.

        That split is the point of moving ``iq_mode`` onto the type: the packing rule is stated and
        tested (see ``tests/hw/test_rfdc_samp_word.py``) while the converter's two halves — the
        complex RF bundle format and the real-only conformance twin — remain the open work, and the
        refusal says which is which.
        """
        with pytest.raises(NotImplementedError, match="real samples only"):
            Rfdc(name="r", sim=Simulation(), word=RfdcSampWord.specialize(samp_per_word=2,
                                                                          iq_mode=True))

    def test_a_word_that_is_not_a_word_type_is_refused(self):
        """``word`` is a type, not a width; handing it a width is the mistake this catches."""
        with pytest.raises(TypeError, match="must be an RfdcSampWord subclass"):
            Rfdc(name="r", sim=Simulation(), word=64)

    def test_a_zero_full_scale_is_refused_because_it_is_also_falsy(self):
        with pytest.raises(ValueError, match="positive amplitude reference"):
            Rfdc(name="r", sim=Simulation(), full_scale=0.0)

    def test_full_scale_rides_the_format_literal_not_a_member_assignment(self):
        """``full_scale`` is an init-time knob but deliberately **not** a ``DynParam``.

        ``DynParam`` does not mean "binds at init" — it means "emitted as ``<model>.<field> = ...;``".
        This value's C++ realization is a *constructor argument* inside the ``RfdcFormat`` literal,
        so tagging it would emit an assignment to a member that does not exist. Found the moment it
        had a real consumer, which is the only way that obligation is ever found.
        """
        from waveflow.hw.hw_module import discover_dyn_params

        r = Rfdc(name="r", sim=Simulation(), full_scale=0.5)
        assert discover_dyn_params(r) == {}, "the Rfdc should emit no member assignments"
        assert "0.5" in r._fmt_literal() and r._fmt_literal().startswith("RfdcFormat{")

    def test_the_converter_reads_the_sample_rate_and_pushes_t0_at_bind(self):
        """The two opposite-direction reads, each where the quantity physically lives."""
        tb = RfLoopbackTB(name="b", sim=Simulation(), samp_rate=128e6, blksize=64, n_blk=1)
        assert tb.rfdc.rx_samp_rate == 128e6            # read OFF the interface clock
        assert tb.rfdc.tx_samp_rate == 128e6
        assert tb.rfdc.rx_blksize == 64
        assert tb.adc_if.t0 == pytest.approx(tb.rfdc.t0_rx)      # pushed ONTO the interface
        assert tb.dac_if.t0 == pytest.approx(tb.rfdc.t0_tx)

    def test_a_zero_block_latency_loop_is_refused_not_reported(self):
        """A loop through the RF grids that claims zero block latency is not a slow system -- it is
        not a system.  Block *k* cannot be played in the period it was captured, however fast the
        fabric is, so the graph refuses to be built rather than reporting a symptom later.
        """
        with pytest.raises(ValueError, match=r"blk_latency must be >= 1"):
            RfSampPassThrough(name="d", sim=Simulation(), blk_latency=0)

    def test_the_dac_underruns_exactly_its_declared_startup_transient(self):
        """The declaration is *checked*, not trusted: the DAC edge must underrun exactly
        ``blk_latency`` times, at the start and nowhere else."""
        sim = RfLoopbackSim(n_src_blk=4, blksize=64)
        sim.run()
        tb = sim.tb
        assert tb.dac_if.underrun == tb.loop_blk_latency == 2
        assert tb.dac_if.last_underrun_idx == 2          # the transient, not a steady-state fault
        assert tb.adc_if.underrun == 0                   # fed straight from the source
        # A module that over-declares its latency fails the same gate.
        with pytest.raises(AssertionError, match="declared startup transient is 3"):
            tb.dac_if.assert_clean(startup_blocks=3)


class TestTheConverterDelegates:
    """``Rfdc._pack`` / ``_unpack`` must be the same implementation, not a second one.

    Two implementations of a packing convention is how the convention comes to mean two things —
    which is the defect ``RfdcSampWord`` was built to end.  So the converter's private pair is
    checked *against* the public one rather than independently.
    """

    def test_the_converters_pair_agrees_with_the_public_one(self):
        """At **two channels**, because that is where a per-channel adapter could still be hiding.

        The pair used to ``reshape(1, -1)`` on the way in and index ``[0]`` on the way out — a
        one-channel wrapper around a channel-major function, which agrees with the public pair for
        exactly the shape it was written for.  Checking the whole ``(n_ch, n_samp)`` array is what
        makes "the same implementation" mean the same thing at any channel count.
        """
        W = WORD.specialize(samp_per_word=4)
        rfdc = Rfdc(name="rfdc_pack_delegation", sim=Simulation(), n_rx=2, n_tx=2, word=W,
                    full_scale=1.0)
        x = np.array([[0.0, 0.25, -0.25, 0.75, -0.75, 0.5, -0.5, 0.125],
                      [0.125, -0.5, 0.5, -0.75, 0.75, -0.25, 0.25, 0.0]])

        words = np.asarray(rfdc._pack(x))
        stored = np.asarray(from_real(x, W.samp_type()), dtype=np.int64)
        assert words.shape == (2, 2)                      # (n_ch, n_samp / samp_per_word)
        assert np.array_equal(words, pack(W, stored))

        # ...and its inverse takes WORDS, which is what a DAC is handed.  Before this pair the two
        # were not inverses in signature at all: _pack produced words while _unpack consumed slots.
        assert np.array_equal(unpack(W, words), stored)
        assert np.allclose(rfdc._unpack(words), to_real(array(W.samp_type(), stored)))
        # Row ch is port ch: the two channels carry DIFFERENT samples here, so a transpose or a
        # swapped row fails rather than passing on symmetry.
        assert not np.array_equal(words[0], words[1])
