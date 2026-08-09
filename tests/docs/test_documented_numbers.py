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
# The guard on the guard
# ---------------------------------------------------------------------------

def test_the_pages_still_contain_tables_to_check():
    """A page rewritten into prose would make every check above vacuously pass.

    Cheap insurance: a check that silently stops checking is worse than no check, because the green
    tick is then evidence of nothing.
    """
    assert re.search(r"^\|\s*\*\*\d+\*\*\s*\|", _page("examples/vecmult/sweep.md"), flags=re.M)
    assert "rank correlation" in _page("examples/firblock/resource_fit.md")


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
