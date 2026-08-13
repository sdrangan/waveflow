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
    """``guide/rf/sampling.md``'s table is the page's load-bearing claim: a relative ``timeout``
    loop slips and the absolute grid does not.  It is quoted as a *demonstrated* result, so the
    demonstration is re-run here and the page's cells matched against it.
    """
    import simpy

    from waveflow.hw.clock import Clock
    from waveflow.hw.rf_sample_if import RFSampIFRx, RFSampIFTx
    from waveflow.simulation.simulation import Simulation
    from tests.hw.test_rf_sample_if import TracingRFSampIF, _feeder

    text = _page("guide/rf/sampling.md")
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


def test_rf_loopback_page_loss_counts_are_recomputed():
    """``examples/rf_loopback/python.md`` quotes three predicted counts and one counter dict.

    Each is produced by actually injecting the fault, because the whole argument of that section is
    that these numbers are *predictions the model meets* rather than observations written down.
    """
    from examples.rf_loopback.rf_loopback import RfLoopbackSim

    text = _page("examples/rf_loopback/python.md")

    clean = RfLoopbackSim(n_src_blk=8)
    clean.run()
    clean.check()
    for line in (f"adc  {clean.tb.adc_if.counters()}", f"dac  {clean.tb.dac_if.counters()}"):
        assert line in text, f"python.md no longer quotes the clean-run counters: {line}"

    late = RfLoopbackSim(n_src_blk=8, blksize=64)
    late.tb.source.start_delay = 2.5 * late.tb.blk_period
    late.run()
    assert f"`adc_if.underrun == {late.tb.adc_if.underrun}`" in text
    assert np.array_equal(late.captured[0], np.zeros((1, late.tb.blksize)))    # "all zeros"

    stalled = RfLoopbackSim(n_src_blk=8, blksize=64, sink_stall_after=1, sink_depth=2)
    stalled.run()
    deeper = RfLoopbackSim(n_src_blk=8, blksize=64, sink_stall_after=1, sink_depth=4)
    deeper.run()
    assert f"`dac_if.overrun == {stalled.tb.dac_if.overrun}`" in text
    assert "`8 − 1 − 2`" in text and f"= {deeper.tb.dac_if.overrun}`" in text

    # The MODULE's own declaration, which is what the page quotes.  Distinct from the LOOP's cost
    # (`loop_blk_latency`), which adds one block for the ADC hop -- a converter cannot emit samples
    # it has not collected.  The two were the same number until the ADC's burst was paced at the
    # converter's rate rather than the fabric clock, and conflating them is what this line used to do.
    assert f"blk_latency: HwParam[int] = {int(clean.tb.dut.blk_latency)}" in text
    assert clean.tb.loop_blk_latency == int(clean.tb.dut.blk_latency) + 1


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
    for page in ("guide/interface/behavioral.md", "guide/build/bfm.md"):
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
    """``examples/rf_loopback/python.md`` states the RTL completion cycle and the scenario size.

    The source of truth is ``test_xsi_bfm.py``'s ``GATES`` table — the same tuple the XSI run
    asserts against — so a re-recorded gate fails here until the page is updated, and a page edited
    to a number nobody measured fails immediately.
    """
    from examples.rf_loopback.rf_dut_build import XSI_NBURST, XSI_NWORDS_BLK
    from tests.examples.test_xsi_bfm import GATES

    want = {top: cycles for top, _tb, cycles, _m in GATES}
    assert "rf_pass_through" in want, "the rf_pass_through gate has been removed from GATES"
    cycles = want["rf_pass_through"]

    text = _page("examples/rf_loopback/python.md")
    assert f"**{cycles}**" in text, (
        f"python.md no longer quotes the recorded cycle gate of {cycles}")
    assert f"{XSI_NBURST} bursts × {XSI_NWORDS_BLK} words = {XSI_NBURST * XSI_NWORDS_BLK} words" \
        in text, "python.md no longer states the scenario the gate measures"


# ---------------------------------------------------------------------------
# guide/rf/converter.md — the loss figures the page reports from a real RTL run
# ---------------------------------------------------------------------------

def test_converter_page_quotes_the_recorded_rtl_loss():
    """``guide/rf/converter.md`` reports what the first real RTL run found, so those numbers are
    claims about the design and must match the gate that measured them.

    Read out of ``test_rf_loopback_xsi.py``'s constants — the same values the XSI gate asserts —
    rather than retyped, so a re-recorded shortfall fails here until the page is updated.
    """
    from tests.examples.test_rf_loopback_xsi import WANT_ADC_DROPPED, WANT_ADC_WORDS

    text = _page("guide/rf/converter.md")
    accepted = WANT_ADC_WORDS - WANT_ADC_DROPPED
    assert f"**{WANT_ADC_WORDS}** words" in text, "converter.md no longer states what the ADC produces"
    assert f"accepts **{accepted}**" in text, "converter.md no longer states what the fabric accepts"
    assert f"**{WANT_ADC_DROPPED} are dropped**" in text, (
        f"converter.md no longer quotes the measured drop count of {WANT_ADC_DROPPED}")


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
    # The RF pages' claims live in one table and one counter dict; prose would make both vacuous.
    assert "| absolute grid |" in _page("guide/rf/sampling.md")
    assert "'underrun':" in _page("examples/rf_loopback/python.md")


# ---------------------------------------------------------------------------
# Symbols — a page naming an API that does not exist
# ---------------------------------------------------------------------------

_SRC_ROOTS = ("waveflow", "examples", "tests")

#: Excluded from the identifier scan, each for a reason:
#:
#: * ``tests/docs`` is documentation *about* documentation — and it defeated this check the first
#:   time it ran, because the docstring below names the very phantom symbol the check exists to
#:   catch, which put that name into the set and made the check pass.
#: * ``_archive`` and ``mcp/corpus`` are snapshots; a name surviving only there is not an API.
_SCAN_SKIP = ("_archive", "mcp/corpus", "tests/docs")


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
