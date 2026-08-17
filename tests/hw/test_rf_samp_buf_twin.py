"""The RF sample buffer's pysim twin: does it model the RATE, and may it legally exist?

Two separate questions, and they are the two halves of ``plans/pipelined_ops.md``.

**1. Is the twin honest?**  ``RfSampBufIngress.run_iter`` reads a whole burst, because a burst is
pysim's quantum, but the hardware moves one *word* per ``fire_cycles`` cycles.  The predecessor
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

#: 256 MSa/s on a 300 MHz fabric at one sample per word: the converter offers 0.853 words/cycle and
#: the ingress absorbs 0.5.  This is the configuration whose FIRST RTL RUN lost 1695 of 4096 samples
#: while pysim reported a clean run.
#: Derived from the fabric clock rather than written as a literal, so re-clocking the examples
#: cannot leave a probe that is over the PORT instead of over the DESIGN.  The two ceilings are
#: ``spw * f_axis`` (250 MSa/s at one sample per word) and ``spw * f_axis / fire_cycles``
#: (125 MSa/s); the probe has to sit strictly between them, or ``Rfdc`` refuses it before the design
#: check is ever reached and the test would be measuring a different refusal.
#:
#: 1.6x the design ceiling.  It was 256 MSa/s at a 300 MHz fabric, which was 1.7x -- the same band.
F_AXIS = RFSOC4X2_CLK_HZ
OVER_RATE = 200e6
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
    refused.  Charging ``fire_cycles`` per word makes the ingress occupy the time it really occupies,
    the port backs up, and the converter's offers start being refused.
    """
    paced = _dropped_at(OVER_RATE)
    monkeypatch.setattr(RfSampBufIngress, "run_iter", _burst_granular_run_iter)
    burst_granular = _dropped_at(OVER_RATE)

    assert burst_granular == 0, (
        "the burst-granular twin is supposed to be blind here — if it now reports loss, this test "
        "no longer demonstrates anything and the reason needs finding")
    assert paced > 0, (
        f"the paced twin still reports no loss at {OVER_RATE:g} samples/s, which is 1.6x what the "
        f"ingress can absorb. The twin is not modelling rate.")


def test_the_loss_the_twin_reports_is_the_right_size():
    """Not just nonzero — the shortfall the arithmetic predicts.

    The ingress absorbs 0.5 samples/cycle at 300 MHz = 150 MSa/s; the converter offers 256 MSa/s, so
    roughly ``1 - 150/256`` = 41% of the stream has nowhere to go.  pysim quantises loss to whole
    BLOCKS (it drops an offer or takes it), so the count lands on a block boundary rather than on the
    exact fraction — this asserts the right ballpark, not a false precision.

    For reference, the RTL run of this configuration lost 1695 of 4096 (41.4%). pysim is close and
    slightly optimistic, which is the expected direction: it cannot lose part of a block.
    """
    dropped = _dropped_at(OVER_RATE)
    frac = dropped / N_WORDS
    predicted = 1.0 - (F_AXIS / 2 / OVER_RATE)
    assert 0.25 < frac < 0.55, (
        f"dropped {dropped}/{N_WORDS} = {frac:.1%}, predicted about {predicted:.1%}")
    assert dropped % 256 == 0, (
        f"pysim drops whole blocks, so {dropped} should be a multiple of the 256-word block")


def test_the_gated_scenario_stays_clean():
    """The pacing must not invent loss in a design that fits: 64 MSa/s against a 150 MSa/s ingress."""
    tb = run_pysim(tb=RfSampBufRxTB(name="ok", sim=Simulation()))
    assert tb.adc_axis.dropped == 0
    assert tb.rate_util == pytest.approx(64e6 / (F_AXIS / 2))


def test_the_drop_threshold_is_the_declared_capacity():
    """`check_rate` and the simulation now agree, which is the point of pacing at all.

    Below the declared capacity pysim is clean; above it, pysim loses samples.  Both terms of the
    firing cost scale with the burst length, so the threshold is the *design's* capacity and does not
    depend on the block size.
    """
    dut = RfSampBufRx(name="cap", sim=Simulation(), **_ELAB)
    cap = dut.max_samp_rate(F_AXIS)
    assert cap == F_AXIS / 2
    assert _dropped_at(cap * 0.8) == 0, "a rate inside the declared capacity must not lose samples"
    assert _dropped_at(cap * 1.7) > 0, "a rate outside it must"


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
    with pytest.raises(ValueError) as err:
        dut.check_rate(OVER_RATE, F_AXIS)
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
