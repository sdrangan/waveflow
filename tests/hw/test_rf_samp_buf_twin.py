"""The RF sample buffer's pysim twin: does it model the RATE, and may it legally exist?

Two separate questions, and they are the two halves of ``plans/pipelined_ops.md``.

**1. Is the twin honest?**  ``RfSampBufIngress.run_iter`` reads a whole burst, because a burst is
pysim's quantum, but the hardware moves one *word* per ``cycles_per_word`` cycles.  The predecessor
charged nothing for that, so the boundary port never backed up and ``dropped`` was zero for a design
that lost 41% of its samples at RTL.  The demonstration below is the acceptance criterion: at a
converter rate the ingress cannot absorb, the paced twin reports loss and the burst-granular one
reports none, on the same graph and the same scenario.

**2. May the body legally be what it is?**  A leaf whose ``kernel_task()`` names a hand-written
header is never extracted, so its ``run_iter`` is free to use constructs ``extract_kernel`` rejects.
That exemption is a **property of a code path, not a stated rule** — ``composite_gen`` simply never
calls the extractor for such a leaf — so a change there could withdraw it silently and this design
would stop generating with no test to say why.  It is already load-bearing: today's shipped body
fails ``extract_kernel``.  So it is pinned here.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.rf_samp_buf_rx.rf_samp_buf_rx import RfSampBufRxTB, run_pysim
from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ, composite_top_spec
from waveflow.build.elaborate import elaborate
from waveflow.hw.rf_samp_buf import IDX_BW, RfSampBufIngress, RfSampBufRx
from waveflow.simulation.simulation import Simulation

#: The elaboration the RTL gate uses — one sample per word.
_ELAB = {"bitwidth": 16, "samp_per_word": 1, "depth": 1024, "horizon_margin": 16}

#: The rate at which the twin still reports loss — and **the band it used to live in has closed.**
#:
#: This probe has to sit above the DESIGN ceiling and at or below the PORT's, or ``Rfdc`` refuses it
#: before the design check is reached and the test measures a different refusal.  Those were
#: ``spw * f_axis / 2`` (125 MSa/s) and ``spw * f_axis`` (250 MSa/s), and 200 MSa/s sat comfortably
#: between them.  Since the ingress became an II=1 loop on 2026-08-18 **the two ceilings are the same
#: number**: there is no rate the port carries and the design refuses.
#:
#: So the probe is the ceiling itself.  At exactly ``spw * f_axis`` the twin drops 1280 of 4096, and
#: the reason is pysim's own quantum rather than the declared cost: :meth:`StreamIFSlave.get` returns
#: a whole burst, so the twin cannot begin draining block *k* until all of it has landed, and at
#: exactly one word per cycle block *k+1* arrives into a 2-deep port with nowhere to go.  **RTL does
#: not have that limit** — ``plans/witness/task_loop/`` measures the same body sustaining 1.000
#: words/cycle with zero gaps — so this is the twin being conservative in its last few percent, and
#: the direction is the safe one.
#:
#: Measured knee: clean to 0.995x the ceiling at one sample per word, 0.98x at four.
F_AXIS = RFSOC4X2_CLK_HZ
OVER_RATE = F_AXIS            # = spw * f_axis at spw = 1: the design ceiling AND the port's
#: Words the converter delivers in the gate scenario (16 blocks x 256 samples, one sample per word).
N_WORDS = 4096


def _burst_granular_run_iter(self):
    """The predecessor twin, reproduced exactly: drain a whole burst, charge nothing for it.

    Kept here rather than in history because a demonstration that the model improved needs the old
    model to compare against, and a description of it would not run.
    """
    words = yield from self.s_in.get()
    mask = int(self.depth) - 1
    spw = int(self.samp_per_word)
    wrap = 1 << IDX_BW
    for x in np.asarray(words).ravel():
        self.buf_w.mem_write((self.wr // spw) & mask, int(x))
        self.wr = (self.wr + spw) % wrap
    yield from self.wr_out.offer(np.array([self.wr], dtype=np.uint64))


def _dropped_at(samp_rate: float, **tb_kwargs) -> int:
    tb = run_pysim(tb=RfSampBufRxTB(name="rate", sim=Simulation(), samp_rate=samp_rate,
                                    enforce_rate=False, **tb_kwargs))
    return int(tb.adc_axis.dropped)


# ---------------------------------------------------------------------------
# 1. The twin models the rate — the acceptance criterion
# ---------------------------------------------------------------------------

def test_the_paced_twin_sees_loss_the_burst_granular_one_could_not(monkeypatch):
    """**The demonstration.**  Same graph, same scenario, same converter rate; only the body differs.

    This is the whole point of the change, so it is measured rather than argued: the predecessor
    drained a burst in zero time, so the 2-deep boundary port never filled and no ``offer`` was ever
    refused.  Charging ``cycles_per_word`` per word makes the ingress occupy the time it really
    occupies,
    the port backs up, and the converter's offers start being refused.
    """
    paced = _dropped_at(OVER_RATE)
    monkeypatch.setattr(RfSampBufIngress, "run_iter", _burst_granular_run_iter)
    burst_granular = _dropped_at(OVER_RATE)

    assert burst_granular == 0, (
        "the burst-granular twin is supposed to be blind here — if it now reports loss, this test "
        "no longer demonstrates anything and the reason needs finding")
    assert paced > 0, (
        f"the paced twin still reports no loss at {OVER_RATE:g} samples/s, which is the ceiling "
        f"itself. The twin is not modelling rate at all.")


def test_the_loss_the_twin_reports_is_the_right_size():
    """Not just nonzero — the shortfall the arithmetic predicts.

    The loss here is NOT a shortfall in the declared rate -- at the ceiling the declared rate is
    exactly the offered one.  It is pysim's burst quantum: the twin cannot start draining block *k*
    until all of it has landed, so block *k+1* meets a 2-deep port.  The size that produces is a
    property of the model, so it is asserted as a band rather than a prediction, and it lands on a
    block boundary because pysim drops whole offers.

    Measured 1280 of 4096 = 31%.  The RTL body has no such limit (1.000 words/cycle, zero gaps in
    ``plans/witness/task_loop/``), so the twin is conservative here -- the safe direction.
    """
    dropped = _dropped_at(OVER_RATE)
    frac = dropped / N_WORDS
    assert 0.20 < frac < 0.45, (
        f"dropped {dropped}/{N_WORDS} = {frac:.1%}; the twin's burst-quantum loss at the ceiling "
        f"was measured at 31%. A big move means the quantum or the port depth changed.")
    assert dropped % 256 == 0, (
        f"pysim drops whole blocks, so {dropped} should be a multiple of the 256-word block")


def test_the_gated_scenario_stays_clean():
    """The pacing must not invent loss in a design that fits: 64 MSa/s against a 250 MSa/s ingress."""
    tb = run_pysim(tb=RfSampBufRxTB(name="ok", sim=Simulation()))
    assert tb.adc_axis.dropped == 0
    # The buffer's capacity is its SLOWEST stage -- the capture, at 2 cycles/word --
    # not the ingress's 1.  See RfSampBufRx.cycles_per_word.
    assert tb.rate_util == pytest.approx(64e6 / (F_AXIS / 2))


def test_the_drop_threshold_is_the_declared_capacity():
    """`check_rate` and the simulation now agree, which is the point of pacing at all.

    Below the declared capacity pysim is clean; above it, pysim loses samples.  Both terms of the
    firing cost scale with the burst length, so the threshold is the *design's* capacity and does not
    depend on the block size.
    """
    dut = RfSampBufRx(name="cap", sim=Simulation(), **_ELAB)
    cap = dut.max_samp_rate(F_AXIS)
    assert cap == F_AXIS / 2, "the buffer's capacity is its slowest stage, the capture's 2 cycles/word"

    # **The two numbers have come apart, and that is the finding.**  `dropped` counts refusals at the
    # ADC BOUNDARY, and what drains that port is the INGRESS alone -- now 1 cycle/word, so the
    # boundary keeps up all the way to `F_AXIS`.  `max_samp_rate` is the whole buffer's, and the
    # capture behind the memory is half that.  They agreed while both stages cost 2.
    #
    # So a rate between them is one `check_rate` refuses and the ADC-boundary counter does not see:
    # the loss moves from "refused at the port" to "overwritten in the buffer before the capture
    # reaches it", which is a `too_old` on a command rather than a drop.  Asserting both keeps the
    # distinction visible instead of letting one number stand in for the other.
    assert _dropped_at(cap * 0.8) == 0, "a rate inside the declared capacity must not lose samples"
    assert _dropped_at(cap) == 0, (
        "at the BUFFER's capacity the ADC boundary is still comfortable -- the ingress is twice as "
        "fast as the buffer as a whole, so this counter is not what binds")
    assert _dropped_at(F_AXIS) > 0, (
        "at the INGRESS's own capacity the boundary port must finally back up -- if it does not, the "
        "twin has stopped charging per word")


def test_widening_the_word_removes_the_loss():
    """The throughput lever, end to end: the rate that overran one sample per word fits four.

    Same converter, same scenario — only the geometry changes, and the loss goes away because the
    ingress now absorbs 2 samples per cycle instead of 0.5.
    """
    assert _dropped_at(OVER_RATE, samp_per_word=1) > 0
    assert _dropped_at(OVER_RATE, samp_per_word=4) == 0


def test_the_refusal_message_no_longer_claims_pysim_is_blind():
    """`check_rate` used to say "pysim will not show it". That was true and is now false.

    A message that tells a reader not to bother looking is worse than no message once looking works.
    """
    dut = RfSampBufRx(name="msg", sim=Simulation(), **_ELAB)
    # ABOVE the ceiling, not at it: `check_rate` refuses only what it cannot absorb, and since
    # 2026-08-18 that is also more than the port carries -- so this is a rate no simulation can
    # be built at.  The message is still worth pinning; it is what a user sees at build time.
    with pytest.raises(ValueError) as err:
        dut.check_rate(OVER_RATE * 1.5, F_AXIS)
    assert "pysim will not show it" not in str(err.value)
    assert "pysim WILL now show it" in str(err.value)


# ---------------------------------------------------------------------------
# 2. The codegen exemption, pinned
# ---------------------------------------------------------------------------

def test_the_composite_generates_even_though_the_leaf_body_would_not_extract():
    """**The pin.**  The generator must keep never extracting a hooked leaf's ``run_iter``.

    Both halves are asserted together, because either one alone is uninformative:

    * ``composite_top_spec`` — the real generator — **succeeds**; this design ships because of it.
    * ``extract_kernel`` on the same leaf **raises**; so the exemption is load-bearing rather than
      incidental, and a change to ``composite_gen`` that started extracting hooked leaves would
      break this design with nothing else to say why.

    Note what is deliberately NOT used as evidence: ``check(leaf, "composite_kernel")`` returns True
    for an unambiguously illegal body, because that gate never runs extraction for this combination.
    A verdict from it would prove nothing here.
    """
    from waveflow.build.hwcodegen import extract_kernel

    comp = elaborate(RfSampBufRx, dict(_ELAB), name="rf_samp_buf_rx")
    spec = composite_top_spec(comp, width=16)
    assert spec.top_name == "rf_samp_buf_rx"
    assert any("rf_samp_buf_ingress_task" in t.task_fn for t in spec.tasks), (
        f"the ingress task is not in the generated top: {[t.task_fn for t in spec.tasks]}")

    leaf = RfSampBufIngress(name="leaf", sim=Simulation(), bitwidth=16, samp_per_word=1, depth=1024)
    with pytest.raises(Exception) as err:
        extract_kernel(leaf)
    assert "SynthesisError" in type(err.value).__name__, (
        f"extract_kernel now accepts this body ({type(err.value).__name__}). If that is deliberate "
        f"the exemption is no longer load-bearing and this test should be rewritten, not deleted.")


def test_a_pipelined_body_in_a_hooked_leaf_also_survives_composite_codegen():
    """The same exemption, for the construct ``plans/pipelined_ops.md`` measured it with.

    The shipped ingress does not use ``get_pipelined`` — see :meth:`RfSampBufIngress.run_iter` for
    the two reasons it cannot — but the plan's finding was specifically about pipelined ops, and a
    future body (the TX player, an II=1 rewrite) may well want them.  So the property is pinned for
    that construct too, on a subclass, rather than left as a measurement in a document.
    """
    from waveflow.hw.dataschema import IntField

    word_t = IntField.specialize(bitwidth=16, signed=False)

    class PipelinedIngress(RfSampBufIngress):
        def run_iter(self):
            words, tstart = yield from self.s_in.get_pipelined(word_t, count=4)
            yield from self.wr_out.offer(np.array([int(tstart >= 0)], dtype=np.uint64))
            del words

    class PipelinedBuf(RfSampBufRx):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.ingress.__class__ = PipelinedIngress

    comp = elaborate(PipelinedBuf, dict(_ELAB), name="rf_samp_buf_rx")
    spec = composite_top_spec(comp, width=16)
    assert any("rf_samp_buf_ingress_task" in t.task_fn for t in spec.tasks), (
        "composite codegen no longer tolerates a pipelined body in a hooked leaf — the exemption "
        "plans/pipelined_ops.md relies on has been withdrawn")
