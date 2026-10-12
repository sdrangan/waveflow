"""P6a of ``plans/harmonize_calib.md`` — every number a page quotes must be reproducible.

A documentation figure is a claim about the code, and it is the one kind of claim nothing normally
checks.  Prose rots quietly: a model improves, a corpus gains a point, and the page keeps quoting last
month's error — indistinguishable, to a reader, from a page that is right.  Worse for this project
specifically, because the argument these pages make *is* the numbers: "24/24 exact" and "3.2% mean"
are the evidence, not decoration.

So the figures are recomputed here from the committed corpora and matched against the literal strings
in the pages.  A model change that moves a number now fails a test that names the page to edit, rather
than silently making a document wrong.

Scope is deliberately the **load-bearing** figures — the ones a reader would act on — not every
integer that appears in the docs.  A test asserting a page's every digit would be edited into
uselessness the first time someone rephrased a sentence.
"""
from __future__ import annotations

import re
import statistics
from pathlib import Path

import numpy as np
import pytest

from waveflow.build.elaborate import elaborate
from waveflow.calib.platform import Platform
from waveflow.calib.resource_model import compose

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"
FIR_PLATFORM = REPO / "examples" / "fir_block" / "calib" / "platforms" / "zynq7020_bfm_100mhz"


def _page(rel: str) -> str:
    p = DOCS / rel
    if not p.is_file():
        pytest.skip(f"{rel} not present")
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# vecmult — the measured BRAM sweep
# ---------------------------------------------------------------------------

def test_vecmult_sweep_table_matches_the_corpus():
    """`sweep.md`'s BRAM table is the example's whole point — the block-RAM knee, measured.

    Checked cell by cell rather than in aggregate, because the *shape* is the claim: banks shallower
    than a block, the rounding plateau, and the LUTRAM corner where it drops to zero.  An aggregate
    check would pass on a table that got the interesting cell wrong.
    """
    from examples.vecmult.vecmult_corpus import GRID

    text = _page("examples/vecmult/sweep.md")
    rows = re.findall(r"^\|\s*\*\*(\d+)\*\*\s*\|(.+)\|\s*$", text, flags=re.M)
    assert rows, "no bolded vlen rows found in sweep.md — has the table been restructured?"

    dwids = [32, 64, 128, 256]                      # LW = 2, 4, 8, 16 at samp_w=16
    checked = 0
    for vlen_s, body in rows:
        vlen = int(vlen_s)
        cells = [c.strip().strip("*") for c in body.split("|")]
        for dwid, cell in zip(dwids, cells):
            want = GRID[(vlen, dwid)]["bram"]
            assert int(cell) == want, (
                f"sweep.md quotes bram={cell} at (vlen={vlen}, dwid={dwid}); "
                f"the committed corpus says {want}")
            checked += 1
    assert checked == 16, f"expected all 16 sweep points to be quoted, checked {checked}"


# ---------------------------------------------------------------------------
# fir_block — the composed-estimate validation figures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fir_validation() -> dict:
    """Recompute what `resource_fit.md` reports: exactness, relative error, rank correlation."""
    from examples.fir_block.fir_block import FirBlock
    from examples.fir_block.fir_block_corpus import GRID

    if not FIR_PLATFORM.is_dir():
        pytest.skip("committed fir_block platform not present")
    plat = Platform(name="zynq7020_bfm_100mhz", dir=FIR_PLATFORM,
                    part="xc7z020clg484-1", clk_freq=100e6)

    pred, meas = [], []
    for (ntap, samp_w, unroll), m in sorted(GRID.items()):
        top = elaborate(FirBlock, {"mem_dwidth": 32, "ntap": ntap, "samp_w": samp_w,
                                   "samp_i": 2, "unroll_lane": unroll}, name="fir_block")
        top.add_rm(plat)
        pred.append(compose(top).total)
        meas.append(m)

    out: dict = {"n": len(pred)}
    for c in ("dsp", "bram"):
        out[f"{c}_exact"] = sum(int(p.get(c, 0)) == int(m[f"top_{c}"]) for p, m in zip(pred, meas))
    for c in ("lut", "ff"):
        errs = [abs(p[c] - m[f"top_{c}"]) / m[f"top_{c}"] for p, m in zip(pred, meas)]
        out[f"{c}_mean"] = 100 * statistics.mean(errs)
        out[f"{c}_worst"] = 100 * max(errs)
        a = np.argsort(np.argsort([p[c] for p in pred]))
        b = np.argsort(np.argsort([m[f"top_{c}"] for m in meas]))
        out[f"{c}_rho"] = float(np.corrcoef(a, b)[0, 1])
    return out


def test_firblock_exactness_claims_hold(fir_validation):
    """"24/24 exact" is the strongest claim on the page, so it is checked literally."""
    text = _page("examples/firblock/resource_fit.md")
    n = fir_validation["n"]
    for c in ("dsp", "bram"):
        got = fir_validation[f"{c}_exact"]
        assert got == n, f"{c.upper()} is exact on only {got}/{n} points"
        assert f"**{n}/{n} exact**" in text, (
            f"resource_fit.md should quote **{n}/{n} exact** for {c.upper()}")


def test_firblock_error_figures_are_the_ones_quoted(fir_validation):
    """The mean/worst relative errors on the page must be what the corpus produces today."""
    text = _page("examples/firblock/resource_fit.md")
    for c in ("lut", "ff"):
        claim = f"{fir_validation[f'{c}_mean']:.1f}% mean, {fir_validation[f'{c}_worst']:.1f}% worst"
        assert claim in text, (
            f"resource_fit.md does not quote the current {c.upper()} figures: expected {claim!r}. "
            f"Recompute and update the page rather than relaxing this test.")


def test_firblock_rank_correlations_match_the_documented_estimator(fir_validation):
    """Rank correlation is the *decision fidelity* claim — the one an exploration actually relies on.

    Pinned to the estimator the repo uses: Pearson over ``argsort(argsort(...))`` ranks, matching
    ``tests/examples/test_fir_block_compose.py``.

    That the estimator is named matters, because the choices disagree where it counts.  Tie-corrected
    Spearman (``scipy.stats.spearmanr``) gives 0.951 / 0.987 on this same data, against 0.954 / 0.988
    here — the predictions contain ties, and the two conventions rank them differently.  A third
    decimal that depends on an unstated convention is not reproducible, so the convention is stated.
    """
    text = _page("examples/firblock/resource_fit.md")
    for c in ("lut", "ff"):
        rho = fir_validation[f"{c}_rho"]
        assert rho > 0.93, f"{c} rank correlation fell to {rho:.3f}"
        assert f"{rho:.3f}" in text, (
            f"resource_fit.md should quote {c.upper()} rank correlation as {rho:.3f}")


# ---------------------------------------------------------------------------
# rf_loopback / rf sampling — the metronome demonstration and the loss counts
# ---------------------------------------------------------------------------
#
# These pages are the first non-calibration ones covered here, and deliberately so: the reason the
# stale ``2835 / 3469`` cycle gates survived for weeks in two docs pages with every test green is
# that this file only ever checked calibration figures.  A number in a doc that nothing recomputes
# *will* rot, whatever kind of number it is.
#
# Every figure below is **recomputed by running the thing**, never read from a constant the page and
# the test could drift from together.


def test_sampling_page_metronome_table_is_recomputed():
    """``guide/rf/rfdc/sampling.md``'s table is the page's load-bearing claim: a relative ``timeout``
    loop slips and the absolute grid does not.  It is quoted as a *demonstrated* result, so the
    demonstration is re-run here and the page's cells matched against it.
    """
    import simpy

    from waveflow.hw.clock import Clock
    from waveflow.hw.rf_sample_if import RFSampIFRx, RFSampIFTx
    from waveflow.simulation.simulation import Simulation
    from tests.hw.test_rf_sample_if import TracingRFSampIF, _feeder

    text = _page("guide/rf/rfdc/sampling.md")
    n, period, body = 6, 1.0, 0.1

    # (a) the rejected scheduler, run.
    env = simpy.Environment()
    naive: list[float] = []

    def loop():
        while len(naive) < n:
            yield env.timeout(period)
            naive.append(float(env.now))
            yield env.timeout(body)

    env.process(loop())
    env.run()

    # (b) the real edge, same yielding body.
    sim = Simulation()
    iface = TracingRFSampIF(name="doc_if", sim=sim, samp_clk=Clock(freq=8.0 / period), n_ch=1,
                            blksize=8, n_blk=n, body_delay=body)
    tx, rx = RFSampIFTx(name="tx", sim=sim), RFSampIFRx(name="rx", sim=sim, depth=n + 1)
    iface.bind("tx", tx)
    iface.bind("rx", rx)
    sim.env.process(_feeder(iface, tx, n)())
    sim.run_sim()

    # The page states both rows' first, second and last cells.  Matched as whole TABLE CELLS
    # (`| 1.0 s |`), not as substrings: "1 s" is a substring of "0.1 s", so a loose match would pass
    # on a table whose cells were wrong -- which is how the first draft of this page shipped with two
    # incorrect cells and a green test.
    def _row(label: str) -> str:
        for line in text.splitlines():
            if line.startswith(f"| {label}"):
                return line
        raise AssertionError(f"sampling.md no longer has a '{label}' row in the metronome table")

    slipped, on_grid = _row("`timeout(period)`"), _row("absolute grid")
    for t in (naive[0], naive[1], naive[-1]):
        assert f"| {t:.1f} s " in slipped, f"the slipped row no longer quotes {t:.1f} s"
    for t in (iface.ticks[0], iface.ticks[1], iface.ticks[-1]):
        assert f"| {t:.1f} s " in on_grid, f"the absolute-grid row no longer quotes {t:.1f} s"

    drift = naive[-1] - n * period
    assert drift == pytest.approx((n - 1) * body)
    assert f"**{drift:g} s" in text, f"sampling.md no longer quotes the {drift:g}s cumulative error"
    assert f"`(k-1)·{body:g} s`" in text
    assert f"{n} blocks of a {period:.1f} s period" in text
    assert f"yields for {body:g} s" in text
    assert iface.ticks == pytest.approx([k * period for k in range(1, n + 1)])


def _leading_zero_blocks(captured) -> int:
    """Whole blocks of zero-fill at the head of a capture — what the late-producer figure shows."""
    for k, b in enumerate(captured):
        if np.any(b):
            return k
    return len(captured)


def test_rf_loopback_run_page_loss_counts_are_recomputed():
    """``examples/rf_loopback/run.md`` quotes two counter dicts, three predicted counts and a table.

    Each is produced by actually injecting the fault, because the whole argument of that page is
    that these numbers are *predictions the model meets* rather than observations written down.
    """
    from examples.rf_loopback.rf_loopback import RfLoopbackSim

    text = _page("examples/rf_loopback/run.md")

    clean = RfLoopbackSim(n_src_blk=8)
    clean.run()
    clean.check()
    for line in (f"adc  {clean.tb.adc_if.counters()}", f"dac  {clean.tb.dac_if.counters()}"):
        assert line in text, f"run.md no longer quotes the clean-run counters: {line}"

    late = RfLoopbackSim(n_src_blk=8, blksize=64)
    late.tb.source.start_delay = 2.5 * late.tb.blk_period
    late.run()
    assert f"`adc_if.underrun == {late.tb.adc_if.underrun}`" in text

    stalled = RfLoopbackSim(n_src_blk=8, blksize=64, sink_stall_after=1, sink_depth=2)
    stalled.run()
    deeper = RfLoopbackSim(n_src_blk=8, blksize=64, sink_stall_after=1, sink_depth=4)
    deeper.run()
    assert f"`dac_if.overrun == {stalled.tb.dac_if.overrun}`" in text
    assert "`8 − 1 − 2`" in text and f"= {deeper.tb.dac_if.overrun}`" in text

    # The loop's cost, which is what the shift on the loopback figure is.
    n_lat = int(clean.tb.loop_blk_latency)
    assert clean.tb.loop_blk_latency == int(clean.tb.dut.blk_latency) + 1
    assert f"{n_lat} whole blocks** — {n_lat * int(clean.tb.blksize)} samples" in text, (
        f"run.md no longer states the loop's shift as {n_lat} blocks "
        f"({n_lat * int(clean.tb.blksize)} samples)")


def test_the_late_producer_table_counts_the_blocks_the_figure_shows():
    """The zero-fill table on ``run.md`` is the late-producer figure, in numbers.

    **This is the check that was missing**, and its absence is why the page it replaces claimed
    *three* leading zero blocks when the run produces four.  Every other figure on that page was
    covered here and none of them rotted; this sentence was not, and it did.  The two rows are
    matched as whole table CELLS so a loose substring cannot pass on a wrong table.
    """
    from examples.rf_loopback.rf_loopback import RfLoopbackSim

    text = _page("examples/rf_loopback/run.md")

    def _run(delay_blocks: float) -> RfLoopbackSim:
        sim = RfLoopbackSim(n_src_blk=8, blksize=64, waveform="grid")
        sim.tb.source.start_delay = delay_blocks * sim.tb.blk_period
        sim.run()
        return sim

    clean, late = _run(0.0), _run(2.5)
    rows = {"on time": clean, "source 2.5 block periods late": late}
    for label, sim in rows.items():
        want = f"| {label} | {_leading_zero_blocks(sim.captured)} | {sim.tb.adc_if.underrun} |"
        assert want in text, (
            f"run.md's zero-fill table no longer has the measured row {want!r} — the figure and "
            f"the table are drawn from the same run, so one cannot move without the other")

    # The fault must actually ADD zero-fill, or the figure proves nothing.
    assert _leading_zero_blocks(late.captured) > _leading_zero_blocks(clean.captured)


def test_the_build_page_describes_the_source_waveform_the_figure_draws():
    """``build.md``'s source figure has a shape — 8 blocks, a 4-block window — and prose beside it.

    Recomputed from the bundle the source is actually fed, because that is what the figure plots.
    """
    import tempfile

    from examples.rf_loopback.rf_loopback import RfLoopbackSim
    from waveflow.simulation.rf_tb import read_rf_bundle

    text = _page("examples/rf_loopback/build.md")

    sim = RfLoopbackSim(n_src_blk=8, waveform="sine")
    with tempfile.TemporaryDirectory() as root:
        sim.write_scenario(root)
        blocks = read_rf_bundle(Path(root) / "vectors" / "rf_in", 1, sim.tb.blksize)

    blk = int(sim.tb.blksize)
    live = [k for k, b in enumerate(blocks) if np.any(b)]
    assert live == list(range(live[0], live[-1] + 1)), "the window is no longer contiguous"
    assert f"{len(blocks)} blocks of {blk} samples" in text
    assert f"middle {len(live)} of them — {len(live) * blk} samples" in text, (
        f"build.md no longer states the window as {len(live)} blocks ({len(live) * blk} samples)")

    # The MODULE's own declaration, distinct from the LOOP's cost (`loop_blk_latency`), which adds
    # one block for the ADC hop -- a converter cannot emit samples it has not collected.  The two
    # were the same number until the ADC's burst was paced at the converter's rate rather than the
    # fabric clock, and conflating them is what this line used to do.
    assert f"blk_latency: HwParam[int] = {int(sim.tb.dut.blk_latency)}" in text


def test_the_two_waveforms_still_test_different_things():
    """``build.md``'s "two waveforms" paragraph is a claim about the quantizer, so it is run.

    The grid waveform must survive ``from_real``/``to_real`` unchanged (making the packing check
    strict and the quantizer untested) and the sine must NOT (exercising rounding).  If that ever
    stopped holding, the page's reason for having two waveforms would be false.
    """
    import tempfile

    from examples.rf_loopback.rf_loopback import RfLoopbackSim
    from waveflow.hw.fixpoint import from_real, to_real

    text = _page("examples/rf_loopback/build.md")

    def _first_block(waveform: str):
        sim = RfLoopbackSim(n_src_blk=4, blksize=64, waveform=waveform)
        with tempfile.TemporaryDirectory() as root:
            sim.write_scenario(root)
        x = sim.sent[2]
        st = sim.tb.rfdc.SampType
        return x, to_real(from_real(x / float(sim.tb.full_scale), st)) * float(sim.tb.full_scale)

    grid_x, grid_q = _first_block("grid")
    sine_x, sine_q = _first_block("sine")
    assert np.array_equal(grid_x, grid_q), "the grid waveform is supposed to be a quantizer no-op"
    assert not np.array_equal(sine_x, sine_q), "the sine no longer exercises the quantizer"
    assert "`from_real` a no-op" in text, (
        "build.md no longer says why there are two waveforms — that is the whole paragraph")


# ---------------------------------------------------------------------------
# behavioral edges — the latency a channel hop adds
# ---------------------------------------------------------------------------

def test_the_hop_latency_the_edge_pages_quote_is_the_one_the_channel_has():
    """"each hop costs exactly one cycle" is a figure a reader designs against.

    It is what makes an N-hop chain N cycles slower in XSI than in pysim, which is a real disagreement
    between the two backends and one someone will budget for. So it is recomputed from the C++
    primitive rather than asserted in prose: the same push/pop sequence the pages describe is run, and
    the measured latency must be the number both pages quote.
    """
    import shutil
    import subprocess

    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ not on PATH — cannot measure the channel's hop latency")

    xsi_src = REPO / "waveflow" / "build" / "xsi"
    tmp = REPO / "docs" / "_hoplat_check"          # cleaned up below; no tmp_path in a plain function
    tmp.mkdir(exist_ok=True)
    try:
        src = tmp / "hop.cpp"
        src.write_text(
            '#include "xsi_channel.h"\n#include <cstdio>\n'
            "int main(){ wfbfm::BlockChannel<int> ch(4); int v=0, push_c=3, pop_c=-1;\n"
            "  for (int c=1;c<=8;++c){ ch.sample();\n"
            "    if (pop_c<0 && ch.pop(v)) pop_c=c;\n"
            "    if (c==push_c) ch.push(7); }\n"
            "  std::printf(\"%d\\n\", pop_c-push_c); return 0; }\n", encoding="utf-8")
        exe = tmp / "hop.exe"
        subprocess.run([gxx, "-std=c++17", f"-I{xsi_src}", str(src), "-o", str(exe)],
                       check=True, capture_output=True, text=True)
        measured = int(subprocess.run([str(exe)], check=True, capture_output=True,
                                      text=True).stdout.strip())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    assert measured == 1, f"a channel hop now costs {measured} cycles, not 1"
    word = {1: "one", 2: "two", 3: "three"}[measured]
    for page in ("guide/custom_hooks/behavioral.md", "guide/build/bfm.md"):
        text = _page(page)
        assert f"costs exactly {word} cycle" in text, (
            f"{page} no longer quotes the measured hop latency of {word} cycle")


# ---------------------------------------------------------------------------
# rf_pass_through — the RTL cycle gate quoted on the example page
# ---------------------------------------------------------------------------
#
# A CYCLE COUNT, not a calibration figure — and cycle counts are precisely the class this file did
# not cover, which is how the stale 2835/3469 gates survived for weeks in two docs pages with every
# test green.  Read from the gate table rather than retyped, so the page and the gate cannot drift.


def test_rf_pass_through_page_quotes_the_recorded_cycle_gate():
    """``examples/rf_loopback/rtl.md`` states the RTL completion cycle and the scenario size.

    The source of truth is ``test_xsi_bfm.py``'s ``GATES`` table — the same tuple the XSI run
    asserts against — so a re-recorded gate fails here until the page is updated, and a page edited
    to a number nobody measured fails immediately.
    """
    from examples.rf_loopback.rf_dut_build import XSI_NBURST, XSI_NWORDS_BLK
    from tests.examples.test_xsi_bfm import GATES

    want = {top: cycles for top, _tb, cycles, _m in GATES}
    assert "rf_pass_through" in want, "the rf_pass_through gate has been removed from GATES"
    cycles = want["rf_pass_through"]

    text = _page("examples/rf_loopback/rtl.md")
    assert f"**{cycles}**" in text, (
        f"rtl.md no longer quotes the recorded cycle gate of {cycles}")
    assert f"{XSI_NBURST} bursts × {XSI_NWORDS_BLK} words = {XSI_NBURST * XSI_NWORDS_BLK} words" \
        in text, "rtl.md no longer states the scenario the gate measures"


def test_the_rtl_page_reports_the_hook_findings_the_code_actually_produces():
    """``rtl.md`` shows ``check`` / ``potential_targets`` output, which is *executable* prose.

    **This is the second check that was missing.**  The page it replaces used ``RfDataSource`` as
    its "declares no ``bfm_model()``" example.  That was true when it was written and stopped being
    true when the RF environment nodes acquired C++ models; the module that carries the finding now
    is the DUT, which belongs *inside* the cut.  Nothing caught the inversion, because a ``pycon``
    block is not run by anything unless a test runs it.
    """
    from waveflow.build.codegen_check import check, potential_targets
    from waveflow.hw.hw_module import declares_hook
    from waveflow.simulation.rf_tb import RfDataSink, RfDataSource

    from examples.rf_loopback.rf_loopback import RfSampPassThrough
    from waveflow.hw.rfdc import Rfdc

    text = _page("examples/rf_loopback/rtl.md")

    ok, msg = check(RfSampPassThrough, "xsi_bfm_model")
    assert ok is False, "the DUT now declares a bfm_model(); rtl.md's finding is stale"
    assert "RfSampPassThrough declares no bfm_model() hook" in text, (
        "rtl.md must name the module that carries the finding today, not one that used to")
    assert msg.splitlines()[0].split(".")[0] in text.replace("\n ", " "), (
        "rtl.md quotes a check() message the code no longer produces")

    assert "composite_kernel" in potential_targets(RfSampPassThrough)
    assert "frozenset({'composite_kernel'})" in text

    # The contrast the page draws: the environment nodes DO name a model, and claim no target.
    for mod in (RfDataSource, RfDataSink, Rfdc):
        assert declares_hook(mod, "bfm_model"), (
            f"{mod.__name__} no longer declares a model; rtl.md's contrast is stale")
        assert potential_targets(mod) == frozenset()
    assert ">>> potential_targets(RfDataSource)\nfrozenset()" in text


# ---------------------------------------------------------------------------
# guide/rf/rfdc/rules.md — the measurement behind each rule
# ---------------------------------------------------------------------------
#
# The rules page states each law with one sentence of evidence.  That sentence is the whole reason a
# reader believes the rule, so it is the part that must not rot.  Every live number below is read out
# of the gate that measured it; the historical ones are checked only for *presence*, because pinning
# a number about a design that no longer exists would mean the constant could never move again.


def test_rule_1_quotes_the_recorded_rtl_loss():
    """Rule 1's evidence is what the loopback's RTL run found, before and after the task split.

    Read out of ``test_rf_loopback_xsi.py``'s constants — the same values the XSI gate asserts —
    rather than retyped, so a change fails here until the page is updated.

    The page now tells **three** numbers, and only the last is live. 72 (store-and-forward) and 512
    (after the task split, against a converter model that never back-pressured) are prose about
    states this design has been in; what is checked is that the page has not quietly dropped them,
    because a page showing only the current number teaches nothing — and in this case the middle one
    is the instructive one, since it was a *green result that was not earned*.
    """
    from tests.examples.test_rf_loopback_xsi import (WANT_ADC_DROPPED_IS_STRUCTURAL,
                                                     WANT_ADC_WORDS)

    text = _page("guide/rf/rfdc/rules.md")
    assert WANT_ADC_DROPPED_IS_STRUCTURAL, (
        "the gate no longer expects pattern A to drop; rule 1's evidence rests on it, and if the "
        "pass-through has genuinely been fixed this page needs rewriting rather than re-numbering")
    assert f"produced **{WANT_ADC_WORDS}** words" in text, (
        "rules.md no longer states what the ADC produces")
    assert "accepts **450** and drops **62**" in text, (
        "rules.md no longer states what pattern A does against a converter that back-pressures — "
        "which is the live number and the whole point of the rule")
    assert "**72 were dropped**" in text and "accepted **440**" in text, (
        "rules.md no longer tells the before/after — the drop finding is the reason the counter "
        "contract exists, and a rule stated without it is an assertion")
    assert "not earned" in text, (
        "rules.md no longer records that the intermediate 512 was an artefact of a converter model "
        "that accepted everything; without it the page reads as a design regression rather than as "
        "a measurement becoming honest")


def test_the_converter_parameter_split_matches_the_class():
    """Two pages state which `Rfdc` parameters are `HwParam` and which are plain fields.

    **This is the third check that was missing.**  Both pages said `full_scale` was a `DynParam`.
    It never was one: `DynParam` means *emitted as a member assignment*, and this value's C++
    realization is a constructor argument inside an `RfdcFormat` literal, so tagging it would assign
    a member that does not exist.  The class says so in a comment and nothing compared the two.

    Checked against ``Rfdc.__annotations__`` — the declaration itself — and on **both** pages, so
    the guide and the example cannot drift apart either.
    """
    from waveflow.hw.rfdc import Rfdc

    hw = {n for n, a in Rfdc.__annotations__.items() if "HwParam" in str(a)}
    plain = set(Rfdc.__annotations__) - hw
    assert hw == {"n_rx", "n_tx"}, (
        f"Rfdc's HwParam set changed to {sorted(hw)}; both parameter tables need updating")
    assert "full_scale" in plain, "full_scale is now a parameter type the pages do not describe"
    # `word` is plain for a MECHANICAL reason, and the pages have to say which: HwModule wraps every
    # HwParam value in `HwParamValue(int(value))`, so a type-valued parameter cannot be one.
    assert "word" in plain
    for page in ("examples/rf_loopback/build.md", "guide/rf/rfdc/converter.md"):
        assert "HwParamValue" in _page(page), (
            f"{page} must say WHY `word` is not an HwParam — 'it is a type' is the symptom, "
            f"`HwParamValue(int(value))` is the reason")

    for page in ("examples/rf_loopback/build.md", "guide/rf/rfdc/converter.md"):
        text = _page(page)
        assert "`full_scale` is *not* a `DynParam`" in text \
            or "`full_scale` is not a `DynParam`" in text, (
                f"{page} must say plainly that full_scale is not a DynParam — it reads as one "
                f"otherwise, and that is exactly how this went wrong")
        for name in sorted(plain):
            assert f"`{name}`" in text, f"{page} no longer mentions the plain field {name}"


def test_rule_4_quotes_the_capture_designs_measured_shortfall():
    """Rule 4's evidence is the capture design's first RTL run, and its firing cost is live code.

    ``cycles_per_word`` is a class attribute, so the arithmetic on the page (``f_axis * samp_per_word /
    cycles_per_word``) is only right while that number is what the page says.
    """
    from waveflow.hw.rf_samp_buf import RfSampBufIngress
    from tests.examples.test_rf_samp_buf_rx_xsi import WANT_ADC_WORDS

    text = _page("guide/rf/rfdc/rules.md")
    # The "**2** cycles" on the page is HISTORY -- the ingress that lost 1695 of 4096 -- and it is
    # checked for presence, not against the live constant.  The ingress is 1 today, and pinning the
    # historical figure to the current attribute is what would make the page rewrite its own past.
    assert "firing every **2** cycles" in text, (
        "rules.md no longer states the firing cost of the design that lost the samples; without it "
        "the arithmetic in the evidence block does not follow")
    assert f"**1695 of {WANT_ADC_WORDS}** samples" in text, (
        "rules.md no longer states the shortfall that motivated the design-capacity check")
    # The LIVE claim: the page must divide by the buffer's slowest stage, not by a boundary task.
    assert "RfSampBufRx.cycles_per_word" in text, (
        "rules.md no longer shows the capacity being divided by the buffer's own cycles_per_word; "
        "dividing by the ingress's alone is the error this rule now exists to prevent")
    assert RfSampBufIngress.cycles_per_word == 1, (
        "the ingress is no longer II=1, so rules.md's live claim about the boundary keeping up to "
        "the port needs re-deriving")


def test_the_pysim_loss_the_rf_pages_now_quote_is_recomputed():
    """Two pages claim the paced twin *sees* the loss its predecessor could not, and quote a count.

    That number is the whole evidence for the claim, so it is produced by running the fault rather
    than transcribed: an over-rate configuration in the same band as the run that lost 1695 of 4096.

    Both pages are checked, because a claim that pysim can now see something is exactly the kind that
    rots the moment the model changes back — and a reader who trusts it stops looking.
    """
    from examples.rf_samp_buf_rx.rf_samp_buf_rx import RfSampBufRxTB, run_pysim
    from waveflow.hw.rf_samp_buf import RfSampBufRx
    from waveflow.simulation.simulation import Simulation

    # The probe rate has to move with the ceiling, and it has moved twice.  It was 256 MSa/s at a
    # 300 MHz fabric; 200 when the fabric became 250 MHz (over the 125 MSa/s design ceiling, under
    # the 250 port).  The ingress is now II=1, so 200 is comfortably absorbed and shows nothing --
    # the rate that still backs the boundary port up is the ingress's own ceiling, spw * f_axis.
    from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ
    over = RFSOC4X2_CLK_HZ * 1          # spw = 1 in this testbench
    tb = run_pysim(tb=RfSampBufRxTB(name="doc_over", sim=Simulation(), samp_rate=over,
                                    enforce_rate=False))
    dropped = int(tb.adc_axis.dropped)
    assert dropped > 0, "the paced twin no longer sees the loss; both pages claim it does"

    for page in ("guide/rf/rfdc/rules.md",):   # capture.md retired with the BRAM design
        text = _page(page)
        assert f"**{dropped} of 4096**" in text, (
            f"{page} quotes a pysim drop count that is not the one the model produces ({dropped})")

    # The threshold is the claim underneath the number: below the declared capacity, clean.
    clean = run_pysim(tb=RfSampBufRxTB(name="doc_ok", sim=Simulation()))
    assert clean.adc_axis.dropped == 0
    assert RfSampBufRx(name="c", sim=Simulation(), bitwidth=16, samp_per_word=1,
                       depth=1024).max_samp_rate(300e6) == 150e6


def test_rule_6_quotes_a_case_where_index_agrees_and_arrival_does_not():
    """Rule 6's evidence: the two backends agree on the sample index and differ on arrival.

    **The evidence has now been moved twice, and the history is the rule's best argument.**

    * It was the loopback's startup transient (2 blocks in pysim against 1 at RTL) until 2026-08-17,
      when the XSI DAC learned to withhold ``TREADY`` and the RTL figure became 2 as well.
    * It moved to ``rf_blk_delay``'s 4-sample grid skew, which held until 2026-08-31 — when a cycle
      of phase was corrected in the converter *model* and the skew went to 0.
    * It is back on the loopback, whose transient is 1 at RTL again, because that same correction
      removed the coincidence that had been cancelling the real edge-vs-source difference.

    So the page now quotes **both halves separately**: ``rf_blk_delay`` for the index relation, which
    has never moved through any of it, and the loopback for the arrival difference, which is what the
    rule is warning about.  This test pins each half to the constant that is actually asserted by a
    gate, and refuses a page whose arrival evidence has quietly stopped differing.
    """
    from examples.rf_blk_delay.rf_blk_delay import BLKSIZE, DELAY_BLOCKS
    from tests.examples.test_rf_loopback_xsi import RTL_STARTUP_BLOCKS

    text = _page("guide/rf/rfdc/rules.md")

    # -- the index half: rf_blk_delay's delay, quoted as the number it asks for --------------------
    pysim_delay = DELAY_BLOCKS * BLKSIZE
    assert f"`k·256 + {pysim_delay}`" in text, (
        f"rules.md should quote the delay relation with the measured {pysim_delay}")

    # -- the arrival half: the loopback's transient, which must still DIFFER -----------------------
    pysim_transient = 2      # RfLoopbackTB.loop_blk_latency, asserted by the XSI gate
    assert RTL_STARTUP_BLOCKS != pysim_transient, (
        "the backends agree on the loopback's transient too; rule 6's arrival evidence needs "
        "rewriting again rather than re-numbering — find a case where they still differ, or drop "
        "the rule")
    assert (f"**{pysim_transient}** blocks in pysim and **{RTL_STARTUP_BLOCKS}** at RTL") in text, (
        f"rules.md should quote the measured transients as {pysim_transient} (pysim) and "
        f"{RTL_STARTUP_BLOCKS} (RTL)")
    assert f"A **{pysim_transient - RTL_STARTUP_BLOCKS}**-block difference" in text, (
        f"rules.md should name the difference itself ({pysim_transient - RTL_STARTUP_BLOCKS}), "
        f"which is the quantity a reader would otherwise have to subtract for themselves")


def test_the_rf_guide_quotes_the_fmax_its_examples_actually_close():
    """``guide/rf/rfdc/axis_side.md`` grounds its 250 MHz premise in three measured Fmax figures.

    A premise is only worth writing against if it is reachable, so the page says which part reaches
    it and by how much — and those numbers are recomputed here from each solution's own
    ``csynth.xml`` rather than transcribed.  Rounded to whole MHz on the page because the estimate
    moves in the third significant figure between runs; matched here to +/-2 MHz for the same reason.
    """
    from waveflow.utils.csynthparse import synth_target

    ex = REPO / "examples"
    solutions = {
        "capture buffer": ex / "rf_samp_buf_rx" / "rf_samp_buf_rx_proj",
        "playout buffer": ex / "rf_samp_buf_tx" / "rf_samp_buf_tx_proj",
        "loopback DUT": ex / "rf_loopback" / "rf_pass_through_proj",
    }
    reports = {k: v / "solution1" / "syn" / "report" for k, v in solutions.items()}
    if not all(r.is_dir() for r in reports.values()):
        pytest.skip("the RF examples are not all built here — run each build --through csynth")

    text = _page("guide/rf/rfdc/axis_side.md")
    for label, rep in reports.items():
        got = synth_target(rep)
        assert got and got["fmax_mhz"], f"{label}: no timing estimate in {rep}"
        quoted = round(got["fmax_mhz"])
        # The page writes them as a list: "400 MHz on the capture buffer, 483 on the playout
        # buffer, 547 on the loopback DUT".  Match the number near its label rather than the whole
        # sentence, so a rephrasing does not fail while a wrong figure still does.
        assert any(str(q) in text for q in (quoted, quoted - 1, quoted + 1, quoted - 2, quoted + 2)), (
            f"guide/rf/rfdc/axis_side.md quotes no Fmax near {quoted} MHz for the {label}, which "
            f"is what "
            f"{rep} reports ({got['fmax_mhz']:.2f} MHz)")
        # Against the TARGET the run itself records, not a literal 300.  The examples target 250 MHz
        # (4.0 ns) and the playout buffer fell to 282 when its bodies were pipelined to II=1 -- an
        # II=1 loop does in one cycle what took three, so the path through it is longer.  It still
        # closes with margin; a hard-coded 300 would have failed a design that meets its own clock.
        target_mhz = 1000.0 / got["target_period_ns"]
        assert got["fmax_mhz"] >= target_mhz, (
            f"{label} no longer closes the {target_mhz:.0f} MHz it targets ({got['fmax_mhz']:.1f} MHz)")


def test_the_rf_guide_names_the_part_its_examples_target():
    """The page names a part; the reports say what was built. They must agree."""
    from waveflow.utils.csynthparse import synth_target

    rep = REPO / "examples" / "rf_samp_buf_rx" / "rf_samp_buf_rx_proj" / "solution1" / "syn" / "report"
    if not rep.is_dir():
        pytest.skip("rf_samp_buf_rx is not built here — run its build --through csynth")
    got = synth_target(rep)
    assert got is not None
    assert got["part"] in _page("guide/rf/rfdc/axis_side.md"), (
        f"guide/rf/rfdc/axis_side.md does not name {got['part']}, which is what the RF examples "
        f"are built for")


# ---------------------------------------------------------------------------
# guide/linalg/systolic.md — the cost model's measured accuracy and the limits it quotes
# ---------------------------------------------------------------------------
#
# The page's accuracy table is the evidence for a cost model a reader will price designs with, so it
# is read back cell by cell from the tables the calibration study committed
# (examples/mimo_cg/paper_data/), not from its build records, which are not committed.

_LINALG_DATA = REPO / "examples" / "mimo_cg" / "paper_data"


def _linalg_rows(name: str) -> list:
    import csv

    with (_LINALG_DATA / name).open(encoding="utf-8") as f:
        return list(csv.DictReader(line for line in f if not line.startswith("#")))


def _linalg_metrics() -> dict:
    return {r["metric"]: r["value"] for r in _linalg_rows("linalg_validation_metrics_v2.csv")}


def _systolic_page() -> str:
    """The page with its line breaks folded, so a quoted figure may wrap."""
    return " ".join(_page("guide/linalg/systolic.md").split())


def test_systolic_page_quotes_the_held_out_scores():
    """Every cell of the accuracy table, and the build counts above it."""
    m = _linalg_metrics()

    def pct(key: str) -> str:
        return f"{float(m[key]):.1f}%"

    def whole(key: str) -> str:
        return f"{float(m[key]):.0f}%"

    n = int(m["n_builds"])
    assert int(m["n_clean"]) == n, "a held-out build is not clean; the page says all were bit-exact"
    bram_exact = round(float(m["unit_bram_exact_pct"]) * n / 100)
    rows = [
        f"| DSP exact | {whole('unit_dsp_exact_pct')} of builds | {whole('core_dsp_exact_pct')} of builds |",
        (f"| block RAM exact | {pct('unit_bram_exact_pct')} of builds ({bram_exact} of {n}) "
         f"| {whole('core_bram_exact_pct')} of builds |"),
        (f"| LUT, mean error (worst) | {pct('unit_lut_mape_pct')} ({pct('unit_lut_max_pct')}) "
         f"| {pct('core_lut_mape_pct')} ({pct('core_lut_max_pct')}) |"),
        (f"| FF, mean error (worst) | {pct('unit_ff_mape_pct')} ({pct('unit_ff_max_pct')}) "
         f"| {pct('core_ff_mape_pct')} ({pct('core_ff_max_pct')}) |"),
        (f"| steady interval, mean error (worst), {len(_linalg_rows('linalg_validation_cycles_v2.csv'))} "
         f"job shapes | {pct('interval_mape_pct')} ({pct('interval_max_pct')}) | |"),
        (f"| rejected requests back to back, mean error (worst) | {pct('reject_mape_pct')} "
         f"({pct('reject_max_pct')}) | |"),
    ]
    text = _systolic_page()
    for row in rows:
        assert row in text, f"systolic.md's accuracy table no longer has the row {row!r}"
    assert f"(worst held-out error {pct('core_lut_max_pct')})" in text

    roles = [r["role"] for r in _linalg_rows("linalg_builds.csv")]
    n_cal = sum(r in ("fit", "holdout", "fit2") for r in roles)
    assert f"**{n_cal} builds**" in text and f"**{roles.count('holdout2')} held-out builds**" in text


def test_systolic_page_quotes_the_measured_centre_build():
    """The example's configuration as measured: the unit's resources and its full-size interval."""
    build = "su_m8_k8_n32_r4_c8_l4_w12_f4_b64"
    unit = next(r for r in _linalg_rows("linalg_modules.csv")
                if r["build"] == build and r["what"] == "unit")
    full = next(r for r in _linalg_rows("linalg_cycles.csv")
                if r["build"] == build and r["op"] == "1" and (r["m"], r["k"], r["n"]) == ("8", "8", "32"))
    want = (f"{int(unit['lut']):,} LUT, {int(unit['ff']):,} FF, {unit['dsp']} DSP and {unit['bram']} "
            f"block RAM, and {full['interval']} cycles per `8 × 8 × 32` message")
    assert want in _systolic_page(), f"systolic.md no longer quotes the measured centre build: {want!r}"


def test_systolic_page_quotes_the_isolated_rejections_and_the_run_they_spoil():
    """A rejected request alone between served jobs: one reply gap whatever its length, and the
    recorded run whose prediction it spoils."""
    import itertools

    rows = _linalg_rows("linalg_unit_7_4_cycles.csv")
    gaps = {int(cur["reply_cycle"]) - int(prev["reply_cycle"])
            for prev, cur in itertools.pairwise(rows)
            if prev["run"] == cur["run"] and cur["status"] != "OK"}
    assert len(gaps) == 1, f"the isolated rejections no longer take one time: {sorted(gaps)}"
    m = _linalg_metrics()
    centre = [r for r in rows if r["run"] == "centre"]
    words = {4: "four", 10: "ten"}
    text = _systolic_page()
    assert f"every such reply came {gaps.pop()} cycles after the previous one" in text
    assert (f"one recorded run of {words[len(centre)]} jobs, "
            f"{words[sum(r['status'] != 'OK' for r in centre)]} of them rejected, was predicted at "
            f"{float(m['run_7_4.centre.predicted']):,.0f} cycles against "
            f"{int(m['run_7_4.centre.measured']):,} measured") in text


def test_systolic_page_names_the_block_ram_misses_and_their_buffers():
    """The two held-out block RAM misses: which builds, and the buffer the rule over-counts."""
    from waveflow.linalg import cost
    from waveflow.linalg.systolic import SystolicUnit
    from waveflow.simulation.simulation import Simulation
    from waveflow.utils.fixputils import Format, OMode, QMode

    misses = [r["build"] for r in _linalg_rows("linalg_validation_v2.csv")
              if int(r["unit_bram"]) != round(float(r["unit_bram_pred"]))]
    assert len(misses) == 2, f"the page explains two block RAM misses, the table has {misses}"
    bits, counted = [], []
    for name in misses:
        p = dict(re.findall(r"_([a-z])(\d+)", name))
        assert (p["m"], p["k"], p["b"]) == ("4", "4", "32"), f"{name} is not the case the page names"

        def reg(W: int, I: int) -> Format:
            return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)

        W = int(p["w"])
        u = SystolicUnit(name="u", sim=Simulation(), Mmax=4, Kmax=4, Nmax=int(p["n"]), R=int(p["r"]),
                         C=int(p["c"]), L=int(p["l"]), form=int(p["f"]), word_bits=32,
                         a=reg(W, 3), b=reg(W, 4), c=reg(W, 5))
        a_buf = next(mem for mem in cost.channel_memories(u) if mem.name == "a_blk")
        bits.append(a_buf.banks * a_buf.depth * a_buf.elem_bits)
        counted.append(cost.buffer_blocks(a_buf.depth, a_buf.elem_bits, a_buf.banks, 32, True))
    text = _systolic_page()
    assert f"({bits[0]} and {bits[1]} bits in all)" in text or f"({bits[1]} and {bits[0]} bits in all)" in text
    assert (f"counts {counted[0]} and {counted[1]} block RAMs" in text
            or f"counts {counted[1]} and {counted[0]} block RAMs" in text)


def test_systolic_page_quotes_the_first_round_and_how_the_builds_were_chosen():
    """The first model's held-out misses, and the three groups the 61 calibration builds came from."""
    v1 = {r["metric"]: r["value"] for r in _linalg_rows("linalg_validation_metrics.csv")}
    n = int(v1["n_builds"])
    exact = round(float(v1["unit_bram_exact_pct"]) * n / 100)
    roles = [r["role"] for r in _linalg_rows("linalg_builds.csv")]
    text = _systolic_page()
    assert (f"block RAM exact on {exact} of {n} units, core LUT error "
            f"{float(v1['core_lut_mape_pct']):.1f}%") in text
    assert (f"**{len(roles) - roles.count('holdout2')} builds**: {roles.count('fit')} by rule, the "
            f"first round's {roles.count('holdout')} held-out builds, and {roles.count('fit2')} more "
            "by rule") in text


def test_systolic_page_quotes_the_model_figures_it_states_in_prose():
    """Figures the prose states outside the example's output: pysim's share against the memory-fed
    sum for the example's job, the adapters' block RAM, and the calibrated range."""
    import json

    from examples.mimo_cg.hw.linalg_cal import MAX_PES, VALUES
    from waveflow.linalg import cost
    from waveflow.linalg.systolic import MatmulOp, SystolicUnit
    from waveflow.simulation.simulation import Simulation
    from waveflow.utils.fixputils import Format, OMode, QMode

    def reg(W: int, I: int) -> Format:
        return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)

    u = SystolicUnit(name="mm", sim=Simulation(), Mmax=8, Kmax=8, Nmax=32, R=4, C=8, L=4, form=4,
                     word_bits=64, a=reg(12, 3), b=reg(12, 4), c=reg(12, 5))
    coef = cost.message_model()
    share = cost.core_interval(coef, 8, 8, 32, L=4, R=4, C=8)
    total = cost.message_interval(coef, cost.message_features(u, MatmulOp.MUL, 8, 8, 32))
    text = _systolic_page()
    assert (f"{round(share)} cycles for the job of the example, where the memory-fed sum is "
            f"{round(total)}") in text

    chan = json.loads((cost.platform_dir() / "models" / cost.CHANNELS / "params.json")
                      .read_text(encoding="utf-8"))["adapters_bram"]
    assert f"the `m_axi` adapters: {chan['64']} with 64-bit words, {chan['32']} with 32-bit" in text

    def values(key: str) -> str:
        return "{" + ", ".join(str(v) for v in VALUES[key]) + "}"

    assert VALUES["M"] == VALUES["K"]
    for phrase in (f"`Mmax` and `Kmax` in {values('M')}", f"`Nmax` in {values('N')}",
                   f"`R` in {values('R')}", f"`C` in {values('C')} with `R·C` ≤ {MAX_PES}",
                   f"`L` in {values('L')}", f"`W` in {values('W')}"):
        assert phrase in text, f"systolic.md no longer states the calibrated range as {phrase!r}"


def test_systolic_page_quotes_the_synthesis_timing_of_every_build():
    rows = _linalg_rows("linalg_builds.csv")
    ests = sorted({r["est_ns"] for r in rows})
    assert all(float(e) <= 4.0 for e in ests)
    assert (f"Every one of the {len(rows)} builds met 4 ns in synthesis (estimated {' or '.join(ests)} ns"
            in _systolic_page())


# ---------------------------------------------------------------------------
# guide/linalg/cg_vector.md — the CG unit's cost model, measured runs and limits
# ---------------------------------------------------------------------------


def _cg_page() -> str:
    return " ".join(_page("guide/linalg/cg_vector.md").split())


def _cg_metrics() -> dict:
    return {r["metric"]: r["value"] for r in _linalg_rows("cg_validation_metrics.csv")}


def test_cg_page_quotes_the_held_out_scores():
    """Every cell of the accuracy table, the build counts, and the rejection figures."""
    m = _cg_metrics()

    def pct(key: str) -> str:
        return f"{float(m[key]):.1f}%"

    def whole(key: str) -> str:
        return f"{float(m[key]):.0f}%"

    assert int(m["n_clean"]) == int(m["n_builds"])
    rows = [
        f"| DSP exact | {whole('unit_dsp_exact_pct')} of builds | {whole('core_dsp_exact_pct')} of builds |",
        f"| block RAM exact | {whole('unit_bram_exact_pct')} of builds | {whole('core_bram_exact_pct')} of builds |",
        (f"| LUT, mean error (worst) | {pct('unit_lut_mape_pct')} ({pct('unit_lut_max_pct')}) "
         f"| {pct('core_lut_mape_pct')} ({pct('core_lut_max_pct')}) |"),
        (f"| FF, mean error (worst) | {pct('unit_ff_mape_pct')} ({pct('unit_ff_max_pct')}) "
         f"| {pct('core_ff_mape_pct')} ({pct('core_ff_max_pct')}) |"),
        (f"| served intervals, mean error (worst) | {pct('interval_mape_pct')} "
         f"({pct('interval_max_pct')}) | |"),
    ]
    text = _cg_page()
    for row in rows:
        assert row in text, f"cg_vector.md's accuracy table no longer has the row {row!r}"
    roles = [r["role"] for r in _linalg_rows("cg_builds.csv")]
    assert f"**{roles.count('fit')} builds**" in text
    assert f"**{roles.count('holdout')} held-out builds**" in text
    after_reject = (f"off by {float(m['reject_after_reject_mape_pct']):.0f}% on average and "
                    f"{float(m['reject_after_reject_max_pct']):.0f}% at worst")
    assert after_reject in text
    assert f"a rejection was off by {pct('reject_after_served_mape_pct')} on average" in text


def test_cg_page_quotes_the_step_8_3_runs():
    m = _cg_metrics()
    c = "run_8_3.centre"
    text = _cg_page()
    assert (f"centre {float(m[c + '.predicted']):,.1f} cycles against {int(m[c + '.measured']):,} "
            f"measured ({float(m[c + '.ape_pct']):.1f}%)") in text
    for name in ("smallest", "stress"):
        assert f"{name} {float(m[f'run_8_3.{name}.ape_pct']):.1f}%" in text
    lo, hi = float(m["first_message.over_steady_min"]), float(m["first_message.over_steady_max"])
    assert f"it came {lo:.0f} to {hi:.0f} cycles after the model's steady `START` interval" in text


def test_cg_page_quotes_the_measured_centre_build():
    build = "cg_k8_n32_l4_w12_g8_b64"
    unit = next(r for r in _linalg_rows("cg_modules.csv")
                if r["build"] == build and r["what"] == "unit")
    steps = [int(r["interval"]) for r in _linalg_rows("cg_cycles.csv")
             if r["build"] == build and r["op"] == "2" and r["status"] == "OK"
             and r["prev_status"] == "OK" and (r["k"], r["n"]) == ("8", "32")]
    want = (f"{int(unit['lut']):,} LUT, {int(unit['ff']):,} FF, {unit['dsp']} DSP and {unit['bram']} "
            f"block RAM, and its `8 × 32` steps came {min(steps):,} to {max(steps):,} cycles apart")
    assert want in _cg_page(), f"cg_vector.md no longer quotes the measured centre build: {want!r}"


def test_cg_page_quotes_the_figures_it_states_in_prose():
    """pysim's share against the memory-fed sum, the adapters, the calibrated range, the synthesis
    timing and the golden vectors."""
    import json

    from examples.mimo_cg.hw.cg_cal import VALUES
    from waveflow.linalg import cg, cg_cost, cost
    from waveflow.linalg.cg_vector import CgOp, CgVectorUnit
    from waveflow.simulation.simulation import Simulation
    from waveflow.utils.fixputils import Format, OMode, QMode

    def reg(W: int, I: int) -> Format:
        return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)

    f = cg.CgFormats(A=reg(12, 3), B=reg(12, 4), P=reg(12, 4), R=reg(12, 4), S=reg(12, 5),
                     X=reg(12, 3), ps=reg(20, 10), rz=reg(20, 9), alpha=reg(12, 5),
                     beta=reg(12, 3), g_div=6)
    u = CgVectorUnit(name="cg", sim=Simulation(), Kmax=8, Nmax=32, nitmax=8, L=4, formats=f)
    coef = cg_cost.message_model()
    share = cg_cost.core_interval(coef, CgOp.STEP, 8, 32, L=4, formats=f)
    total = cg_cost.message_interval(coef, cg_cost.message_features(u, CgOp.STEP, 8, 32))
    text = _cg_page()
    assert (f"{round(share):,} cycles per step for the job of the example, where the memory-fed sum "
            f"is {round(total):,}") in text

    chan = json.loads((cost.platform_dir() / "models" / cg_cost.CHANNELS / "params.json")
                      .read_text(encoding="utf-8"))["adapters_bram"]
    assert f"the `m_axi` adapters: {chan['64']} with 64-bit words, {chan['32']} with 32-bit" in text

    def values(key: str) -> str:
        return "{" + ", ".join(str(v) for v in VALUES[key]) + "}"

    for phrase in (f"`Kmax` in {values('K')}", f"`Nmax` in {values('N')}", f"`L` in {values('L')}",
                   f"`W` in {values('W')}", f"`g` in {values('g')}"):
        assert phrase in text, f"cg_vector.md no longer states the calibrated range as {phrase!r}"

    builds = _linalg_rows("cg_builds.csv")
    ests = sorted({r["est_ns"] for r in builds})
    assert all(float(e) <= 4.0 for e in ests)
    assert f"Every one of the {len(builds)} builds met 4 ns in synthesis (estimated {' or '.join(ests)} ns" in text

    with np.load(REPO / "tests" / "linalg" / "data" / "cg_golden.npz", allow_pickle=False) as z:
        meta = json.loads(str(z["meta"]))
    assert f"{len(meta['cases'])} golden cases" in text
    assert f"{len(meta['sets'])} format sets" in text


# ---------------------------------------------------------------------------
# The guard on the guard
# ---------------------------------------------------------------------------

def test_the_pages_still_contain_tables_to_check():
    """A page rewritten into prose would make every check above vacuously pass.

    Cheap insurance: a check that silently stops checking is worse than no check, because the green
    tick is then evidence of nothing.
    """
    assert re.search(r"^\|\s*\*\*\d+\*\*\s*\|", _page("examples/vecmult/sweep.md"), flags=re.M)
    assert "rank correlation" in _page("examples/firblock/resource_fit.md")
    # The RF pages' claims live in tables, a counter dict and two pycon blocks; prose would make
    # every one of them vacuous.
    assert "| absolute grid |" in _page("guide/rf/rfdc/sampling.md")
    run = _page("examples/rf_loopback/run.md")
    assert "'underrun':" in run
    assert "| leading flat blocks at the sink |" in run
    assert ">>> " in _page("examples/rf_loopback/rtl.md")


# ---------------------------------------------------------------------------
# Symbols — a page naming an API that does not exist
# ---------------------------------------------------------------------------

_SRC_ROOTS = ("waveflow", "examples", "tests")

#: Excluded from the identifier scan, each for a reason:
#:
#: * ``tests/docs`` is documentation *about* documentation — and it defeated this check the first
#:   time it ran, because the docstring below names the very phantom symbol the check exists to
#:   catch, which put that name into the set and made the check pass.
#: * ``_archive`` is a snapshot; a name surviving only there is not an API.
_SCAN_SKIP = ("_archive", "tests/docs")


@pytest.fixture(scope="module")
def source_identifiers() -> set:
    """Every identifier appearing in the **Python** source, plus builtins and numpy.

    Deliberately a *token* set rather than a resolved symbol table.  Resolving properly would mean
    importing every module and following re-exports; a token set answers the weaker question this
    check needs — *does this name exist at all?* — with essentially no false positives.

    Python only, because the check reads Python code blocks.  Including the generated ``.h`` / ``.cpp``
    made the scan take 28 seconds and found nothing extra: 0.5s here.
    """
    import builtins

    import numpy

    names: set = set(dir(builtins)) | set(dir(numpy)) | set(dir(numpy.testing))
    for root in _SRC_ROOTS:
        for p in (REPO / root).rglob("*.py"):
            if any(x in p.as_posix() for x in _SCAN_SKIP):
                continue
            names |= set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b",
                                    p.read_text(encoding="utf-8", errors="ignore")))
    return names


def test_example_pages_do_not_document_symbols_that_do_not_exist(source_identifiers):
    """An example page shows code a reader is meant to run, so its names must be real.

    This caught three phantoms in one page: `vecmult/resource_model.md` documented an `add_rm_self`
    override and a `vec_mult_fitted()` constructor that the example never had, and
    `vecmult_resource.py` described a `VecMultResourceModel` class that was never written.  Nothing
    flagged them — the links resolved and the numbers were right, because the *prose* was wrong.

    Scoped to `docs/examples/`, whose code blocks quote real modules.  Guide pages are exempt: they
    use illustrative snippets (`def transform(self, params): ...` on a class that does not exist), and
    checking those would produce noise that gets the whole check suppressed.
    """
    missing: dict = {}
    for p in sorted((DOCS / "examples").rglob("*.md")):
        for block in re.findall(r"```python\n(.*?)```", p.read_text(encoding="utf-8"), flags=re.S):
            for name in set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\s*\(", block)):
                if name not in source_identifiers:
                    missing.setdefault(p.relative_to(DOCS).as_posix(), set()).add(name)
    assert not missing, "example pages call names that exist nowhere in the source:\n  " + "\n  ".join(
        f"{f}: {', '.join(sorted(s))}" for f, s in sorted(missing.items()))


def test_mm_fir_pages_quote_the_recorded_cycle_gates():
    """The mm_fir pages quote the RTL cycle counts of both adaptor topologies; they must be the ones
    the gate pins (``EXPECTED_CYCLES`` in tests/examples/test_mm_fir_xsi.py).  Read with ``ast`` rather
    than imported, so this check needs no toolchain and cannot be skipped by one."""
    import ast

    gate = (REPO / "tests" / "examples" / "test_mm_fir_xsi.py").read_text(encoding="utf-8")
    expected = None
    for node in ast.parse(gate).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "EXPECTED_CYCLES"
                                                for t in node.targets):
            expected = ast.literal_eval(node.value)
    assert expected and set(expected) == {"per_view", "one_front"}, expected
    for page in ("protocol.md", "pysim.md", "rtlsim.md"):
        text = (DOCS / "examples" / "mm_fir" / page).read_text(encoding="utf-8")
        for topology, cycles in expected.items():
            assert re.search(rf"\b{cycles}\b", text), (
                f"docs/examples/mm_fir/{page} does not quote the {topology} gate ({cycles} cycles)")


# ---------------------------------------------------------------------------
# The processor model (plans/cpu_model.md) — docs/guide/cpu/ and docs/examples/cpu_sched/
# ---------------------------------------------------------------------------

CPU_PLATFORM = REPO / "waveflow" / "calib" / "platforms" / "a53_hpi_1200mhz_gem5v25_1" / "cpu"


def _cpu_page(rel: str) -> str:
    p = DOCS / rel
    assert p.is_file(), f"{rel} is missing"  # these pages are the plan's deliverable: not a skip
    return p.read_text(encoding="utf-8")


def _pct(x: float) -> str:
    return f"{100 * x:.1f} %"


def test_cpu_accuracy_table_matches_the_one_time_test():
    """`calibration.md`'s table, cell by cell, against the committed `accuracy.csv`."""
    import pandas as pd

    from waveflow.cpu.calib.calibrate import summarize

    s = summarize(pd.read_csv(CPU_PLATFORM / "accuracy.csv"))
    text = _cpu_page("guide/cpu/calibration.md")
    rows = {
        fam: cells
        for fam, cells in re.findall(r"^\| `([\w.]+)` \|(.+)\|\s*$", text, flags=re.MULTILINE)
    }
    checked = 0
    for fam in s[~s["informational"]]["family"].unique():
        assert fam in rows, f"calibration.md has no row for {fam}"
        cells = [c.strip().strip("*") for c in rows[fam].split("|")]
        cyc = s[(s["family"] == fam) & (s["target"] == "cycles")].iloc[0]
        en = s[(s["family"] == fam) & (s["target"] == "energy_pj")].iloc[0]
        assert cells == [_pct(cyc["median"]), _pct(cyc["max"]), _pct(en["median"]), _pct(en["max"])], fam
        checked += 1
    assert checked == 8


def test_cpu_area_and_leakage_figures_match():
    import pandas as pd

    from waveflow.cpu.calib.calibrate import summarize

    s = summarize(pd.read_csv(CPU_PLATFORM / "area" / "accuracy.csv")).set_index("target")
    want = (
        f"area\nmedian {_pct(s.loc['area_mm2', 'median'])}, max {_pct(s.loc['area_mm2', 'max'])}; "
        f"leakage median {_pct(s.loc['leak_mw', 'median'])}, max {_pct(s.loc['leak_mw', 'max'])}"
    )
    assert " ".join(want.split()) in " ".join(_cpu_page("guide/cpu/calibration.md").split())


def test_cpu_replay_figures_match_the_recorded_replay():
    import json

    rec = json.loads((REPO / "examples" / "cpu_sched" / "replay_result.json").read_text())
    measured = f"{rec['total_mode']['measured_total_cycles']:,.0f}"
    predicted = f"{rec['model_total_cycles']:,.0f}"
    err = f"{100 * rec['total_mode']['total_rel_err']:.1f} %"
    for page in ("guide/cpu/calibration.md", "examples/cpu_sched/index.md"):
        text = " ".join(_cpu_page(page).split())
        assert measured in text and predicted in text and err in text, page
    hot = rec["per_op"]["in_context_marker_cycles_median"]
    assert "8 when it is hot" in _cpu_page("guide/cpu/calibration.md") and hot == 8.0


def test_cpu_cross_configuration_figures_match():
    import pandas as pd

    df = pd.read_csv(CPU_PLATFORM / "cross_config.csv")
    g = df[df["family"] == "gather_hist"]
    slow = f"{(g['measured'] / g['measured_at_reference']).median():.2f}x slower"
    within = f"within {100 * g['rel_err'].max():.1f} % (median {100 * g['rel_err'].median():.1f} %)"
    text = " ".join(_cpu_page("guide/cpu/dse.md").split())
    assert slow in text and within in text


def test_cpu_example_report_matches_a_fresh_run():
    from examples.cpu_sched.cpu_sched import run

    res = run(200, 2)
    rep = res.report
    text = _cpu_page("examples/cpu_sched/index.md")
    assert f"200 jobs in {res.sim_s * 1e6:.1f} us simulated; all correct: True" in text
    assert f"CPU utilization [{rep.utilization[0]:.3f}]; energy {rep.total_pj / 1e6:.3f} uJ" in text
    levels = rep.functions["sched_ops.add"].levels
    assert f"exactly the {levels['EXTRAPOLATED']} arrivals" in " ".join(text.split())


# ---------------------------------------------------------------------------
# The RF environment (plans/sionna_rt/sionna_rt.md) — docs/guide/rf/rfenv/
# ---------------------------------------------------------------------------
#
# AC11 asks that *every* number on these pages be tied to a committed file or test, so the check runs
# both ways: each number in the prose must have an entry below (a new number fails until it is tied),
# and each entry must still hold.  The fenced blocks are left out: the python fences are code, and
# their output fences are checked by tests/docs/test_doc_snippets.py, which runs them.


def _prose_numbers(text: str) -> list[str]:
    """The numbers a page's prose and tables state: front matter, fences, inline code and link
    targets removed; identifiers such as ``AC7``, ``4x2`` or ``P.2040`` are not numbers."""
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", "", text)
    text = re.sub(r"\]\([^)]*\)", "]", text)
    return re.findall(r"(?<![\w.])[−-]?\d+(?:[.,]\d+)*(?:e[−-]?\d+)?(?!\w)", text)


def _src(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _rfenv_page(rel: str) -> str:
    p = DOCS / rel
    assert p.is_file(), f"{rel} is missing"  # the plan's deliverable: not a skip
    return p.read_text(encoding="utf-8")


def _bound(rel: str, pattern: str) -> str:
    """The number a committed source states, captured by *pattern*'s one group."""
    m = re.search(pattern, _src(rel), flags=re.MULTILINE)
    assert m, f"/{pattern}/ no longer matches in {rel}"
    return m.group(1)


def _rfenv_guide_phrases() -> list[str]:
    """Each figure on ``docs/guide/rf/rfenv/index.md``, rebuilt from what states it into the phrase
    the page uses: a number swapped for another, even one the page states elsewhere, fails."""
    from examples.rt_sounder import rt_sounder as rs
    from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
    from waveflow.rfenv.apply import DEFAULT_FILT_LEN

    ref, app, live = (
        "tests/rfenv/test_sionna_reference.py",
        "tests/rfenv/test_apply.py",
        "tests/rfenv/test_live.py",
    )

    def ps(s: str) -> str:  # a bound in seconds, quoted in picoseconds
        return f"{float(s) * 1e12:g} ps"

    d_div = _bound("waveflow/rfenv/nodes.py", r"int\(self\.filt_len\) // (\d+)")
    floor_db = float(_bound(app, r"err_db <= (-\d+\.\d+)"))
    band = 2 * float(_bound(app, r"rng\.uniform\(-(0\.\d+), "))  # |f| <= x fs, of fs/2
    sounder_bound = _bound("tests/examples/test_rt_sounder.py", r"cmp\.worst_db <= (\d\.\d+)")
    iq_bits = int(Rfsoc4x2SampWord.specialize(samp_per_word=2, iq_mode=True).bitwidth)
    assert int(Rfsoc4x2SampWord.specialize(samp_per_word=4).bitwidth) == iq_bits
    snippet_dbfs = float(
        _bound("docs/guide/rf/rfenv/index.md", r"gain_db_for_dbfs\(p_strongest, (-\d+\.\d+),")
    )
    taps_bound = _bound(ref, r"rel_err\(mine, theirs\) <= (\S+)$")
    los_s = _bound(ref, r"abs\(tau\[0\] - d / C\) <= (\S+)$")
    friis_db = _bound(ref, r"friis_db\(d\)\) <= (\S+)$")
    reflect_s = _bound(ref, r"abs\(tau\[0\] - d_los / C\) <= (\S+) and")
    reflect_db = _bound(ref, r"abs\(measured - expected\) <= (\S+),")
    blocks_rel = _bound(app, r"rel_err\(got, want\) <= (\S+)$")
    doppler_hz = _bound(ref, r"abs\(doppler\[0\] - v / LAM\) <= (\S+)$")
    live_rel = _bound(live, r"rel_err\(y_live, y_cached\) <= (\S+)$")

    tb = rs.run_channel_mode()
    cmp = rs.compare_taps(tb.paths, rs.estimate_taps(tb))
    return [
        f"**D = `filt_len` / {d_div}** samples",
        (
            f"The default, {DEFAULT_FILT_LEN}, keeps a sub-sample delay's error at or below "
            f"{_neg(floor_db, '.0f')} dB of a signal that occupies up to {band:g} of the Nyquist band"
        ),
        f"to compare taps {rs.TAP_WINDOW_DB:.0f} dB down with Sionna's",
        f"(the sounder uses {rs.FILT_LEN})",
        f"two complex samples in a {iq_bits}-bit beat, the width of the real word with four samples",
        f"A source on an ADC, with the strongest tap at {_neg(snippet_dbfs, '.0f')} dBFS:",
        f"| free space | d/c and Friis | {ps(los_s)}; {friis_db} dB |",
        (
            f"| one reflector | the image method; the ITU-R P.2040 slab | "
            f"{ps(reflect_s)}; {reflect_db} dB |"
        ),
        f"| block by block | the whole signal at once | relative error {blocks_rel} |",
        f"| sub-sample delay | the analytically delayed signal | {_neg(floor_db, '.0f')} dB |",
        f"| Doppler | v·cos θ / λ, Sionna's sign | {doppler_hz} Hz |",
        f"| `taps()` | Sionna's `Paths.taps()` | relative error {taps_bound} |",
        f"| live mode | a channel file traced by the same install | relative error {live_rel} |",
        (
            f"| the sounder | `taps()`, every tap within {rs.TAP_WINDOW_DB:.0f} dB of the strongest | "
            f"{sounder_bound} dB |"
        ),
        f"The sounder's measured worst case is {cmp.worst_db:.3f} dB, over {len(cmp.rows)} taps.",
        f"agree far more closely than the {taps_bound} above",
    ]


def test_rfenv_guide_quotes_its_sources():
    text = " ".join(_rfenv_page("guide/rf/rfenv/index.md").split())
    missing = [p for p in _rfenv_guide_phrases() if " ".join(p.split()) not in text]
    assert not missing, "docs/guide/rf/rfenv/index.md no longer states:\n  " + "\n  ".join(missing)


def test_rfenv_guide_states_no_untied_number():
    text = _rfenv_page("guide/rf/rfenv/index.md")
    tied = {n for p in _rfenv_guide_phrases() for n in _prose_numbers(p)}
    untied = sorted(set(_prose_numbers(text)) - tied)
    assert not untied, (
        f"docs/guide/rf/rfenv/index.md states {untied}, which no phrase in "
        f"_rfenv_guide_phrases() ties to a source")


# ---------------------------------------------------------------------------
# docs/examples/rt_sounder/ — the channel sounder's page
# ---------------------------------------------------------------------------
#
# Each figure is rebuilt from what states it -- the committed channel file, the example's constants,
# the gates' recorded expectations -- into the phrase the page uses, and the phrases' numbers are the
# only ones the prose may state.  The tap table in the page's output fence is the example's own
# output, run by tests/docs/test_doc_snippets.py.


def _neg(x: float, fmt: str) -> str:
    """A number as the page prints it: a minus sign, not a hyphen."""
    return format(x, fmt).replace("-", "−")


def _rt_sounder_phrases() -> list[str]:
    import math

    from examples.rf_loopback.rf_dut_build import XSI_NWORDS_BLK
    from examples.rt_sounder import rt_sounder as rs
    from tests.examples import test_rf_loopback_xsi as iq
    from tests.examples import test_rt_sounder_xsi as gate
    from waveflow.rfenv.apply import DEFAULT_FILT_LEN, ChannelApplier, taps

    ps = rs.channel()
    ok = ps.valid[0, 0]
    tau = ps.tau[0, 0][ok] * 1e9
    db = 20 * np.log10(np.abs(ps.a[0, 0][ok]))
    order = np.argsort(tau)
    rows = [f"| {tau[i]:.1f} ns | {_neg(db[i], '.1f')} dB |" for i in order]
    tx, rx = rs.SPEC.transmitters[0].position, rs.SPEC.receivers[0].position
    dist = math.dist(tx, rx)
    cluster = np.sort(tau)[:4]

    n = math.ceil(ps.max_delay() * rs.SAMP_RATE) + 64
    exact = np.abs(taps(ps, rs.SAMP_RATE, 0, n - 1)[0, 0, 0]) ** 2
    inside = 10 * np.log10(exact / exact.max()) > -rs.TAP_WINDOW_DB
    lags = np.flatnonzero(inside).tolist()
    assert lags == list(range(23, 38)) + [43, 44]  # the shape the prose describes

    def worst_window_db(filt_len: int) -> float:
        app = ChannelApplier(ps, rs.SAMP_RATE, filt_len=filt_len)
        x = np.zeros((1, n + filt_len), dtype=complex)
        x[0, 0] = 1.0
        d = app.group_delay_samp
        win = np.abs(app.process(x, 0)[0][d : d + n]) ** 2
        return float(np.max(np.abs(10 * np.log10(win[inside] / exact[inside]))))

    hop, d = rs.BLKSIZE, rs.FILT_LEN // 2
    return rows + [
        f"canyon` at {rs.SPEC.frequency_hz / 1e9:g} GHz",
        f"transmitter {tx[2]:.0f} m up",
        f"canyon, {dist:.1f} m away",
        f"Four paths arrive within {cluster[-1] - cluster[0]:.1f} ns",
        (
            f"at {rs.SAMP_RATE / 1e6:g} MSa/s they blur into a cluster of taps over lags {lags[0]} to "
            f"{lags[14]}; the fifth is a separate echo at lags {lags[15]} and {lags[16]}"
        ),
        f"one block of {hop} samples",
        f"`filt_len` / 2 = {d} samples",
        f"sequence of length {rs.ZC.length} at half full scale",
        f"strongest tap at {_neg(rs.TARGET_DBFS, '.0f')} dBFS at the ADC",
        f"read {hop + d} samples earlier",
        f"Every tap within {rs.TAP_WINDOW_DB:.0f} dB of the strongest",
        "within 0.5 dB,",
        f"channel has taps {rs.TAP_WINDOW_DB:.0f} dB down",
        (
            f"with the default {DEFAULT_FILT_LEN} taps, the taps above the window differ from Sionna's "
            f"exact sinc by up to {worst_window_db(DEFAULT_FILT_LEN):.2f} dB"
        ),
        f"with {rs.FILT_LEN}, by {worst_window_db(rs.FILT_LEN):.3f} dB",
        f"sent at {rs.SOURCE_TX_POWER_DBM:.0f} dBm",
        (
            f"a {rs.SOURCE_NOISE_FIGURE_DB:.0f} dB noise figure, and the strongest tap at "
            f"{_neg(rs.TARGET_DBFS, '.0f')} dBFS"
        ),
        (
            f"{rs.SOURCE_SAMP_RATE / 1e6:.0f} MSa/s, {rs.SOURCE_NBLK} blocks of {rs.SOURCE_BLKSIZE} "
            f"samples"
        ),
        f"reads a whole {XSI_NWORDS_BLK}-word block",
        (
            f"drops {gate.WANT_ADC_DROPPED} of the {iq.WANT_IQ_ADC_WORDS} words, in "
            f"{len(gate.WANT_SRC_GAPS)} gaps, and {iq.WANT_IQ_DATA_BLOCKS_OUT} data blocks reach the "
            f"sink, among {iq.WANT_IQ_SINK_BLOCKS} grid periods"
        ),
    ]


def test_rt_sounder_page_quotes_its_sources():
    text = " ".join(_rfenv_page("examples/rt_sounder/index.md").split())
    missing = [p for p in _rt_sounder_phrases() if p not in text]
    assert not missing, "docs/examples/rt_sounder/index.md no longer states:\n  " + "\n  ".join(
        missing)
    assert re.search(r"cmp\.worst_db <= 0\.5", _src("tests/examples/test_rt_sounder.py"))


def test_rt_sounder_page_states_no_untied_number():
    text = _rfenv_page("examples/rt_sounder/index.md")
    tied = {n for p in _rt_sounder_phrases() for n in _prose_numbers(p)}
    untied = sorted(set(_prose_numbers(text)) - tied)
    assert not untied, (
        f"docs/examples/rt_sounder/index.md states {untied}, which no phrase in "
        f"_rt_sounder_phrases() ties to a source")
