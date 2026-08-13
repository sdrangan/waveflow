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

import numpy as np
import pytest

from waveflow.build.codegen_check import check
from waveflow.simulation.rf_tb import RfDataSink, RfDataSource, read_rf_bundle, write_rf_bundle
from waveflow.simulation.simulation import Simulation

from examples.rf_loopback.rf_loopback import RfLoopbackSim, RfLoopbackTB, RfSampPassThrough
from examples.rf_loopback.rfdc import Rfdc


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
        """
        sim = RfLoopbackSim(n_src_blk=4, nbits=nbits, samp_per_word=samp_per_word, blksize=64)
        sim.run()
        sim.check()

    def test_a_non_unit_full_scale_still_round_trips(self):
        sim = RfLoopbackSim(n_src_blk=4, full_scale=0.5, blksize=64)
        sim.run()
        sim.check()

    def test_the_axis_word_is_samp_per_word_times_nbits(self):
        tb = RfLoopbackTB(name="w", sim=Simulation(), nbits=12, samp_per_word=4, blksize=64,
                          n_blk=1)
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
# Gate 3 — the counters, non-vacuous
# --------------------------------------------------------------------------------------------

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
        sim = RfLoopbackSim(n_src_blk=2, blksize=64, samp_rate=1.6e9, samp_per_word=4,
                            axis_freq=300e6)            # 1.6e9 > 4 * 300e6
        with pytest.raises(ValueError, match="exceeds what the AXIS port can carry"):
            sim.run()

    def test_a_blksize_that_splits_a_sample_across_words_fails_loud(self):
        sim = RfLoopbackSim(n_src_blk=2, blksize=66, samp_per_word=4)
        with pytest.raises(ValueError, match="not a multiple of samp_per_word"):
            sim.run()

    def test_multichannel_axis_is_refused_and_names_the_open_question(self):
        with pytest.raises(NotImplementedError, match="open question"):
            Rfdc(name="r", sim=Simulation(), n_rx=2)

    def test_interleaved_iq_is_refused_at_stage_1(self):
        with pytest.raises(NotImplementedError, match="real samples only"):
            Rfdc(name="r", sim=Simulation(), iq_mode=1)

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
