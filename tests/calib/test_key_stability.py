"""P0-P3 of ``plans/key_stability.md`` — a module key addresses structure, and nothing else.

A record store is addressed by ``structure_signature``.  If something that is *not* structure reaches
that signature, every stored measurement for the affected modules becomes unreachable — and silently,
because a missing key is indistinguishable from a configuration nobody measured.

That is not hypothetical.  ``FirCompute`` held its timing model under a second attribute name that
``_CONTEXT_ATTRS`` did not cover, so a ``LinCalibModel`` refactor moved its key and orphaned 26
committed records with every gate green.  ``vecmult``, whose modules carry no timing model, was
unaffected — the control that made the diagnosis conclusive.

Three things are pinned here:

* **the keys themselves** (P0), so a deliberate move is auditable rather than silent;
* **the invariant** (P1) — mutating a calibration model must not move any key;
* **reachability** (P3) — every module a design walks must resolve in the store it ships with.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from waveflow.build.elaborate import elaborate, structure_signature
from waveflow.calib.module_key import _serialize, walk_modules
from waveflow.calib.record_store import ModuleStore

REPO = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).resolve().parent / "golden" / "module_keys.json"

FIR_PLATFORM = REPO / "examples" / "fir_block" / "calib" / "platforms" / "zynq7020_bfm_100mhz"
VEC_PLATFORM = REPO / "examples" / "vecmult" / "calib" / "platforms" / "zynq7020_vecmult"


# ---------------------------------------------------------------------------
# Walking each example over its corpus grid
# ---------------------------------------------------------------------------

def _fir_points() -> list:
    from examples.fir_block.fir_block_corpus import GRID

    return [{"mem_dwidth": 32, "ntap": n, "samp_w": w, "samp_i": 2, "unroll_lane": u}
            for (n, w, u) in sorted(GRID)]


def _vec_points() -> list:
    from examples.vecmult.vecmult_corpus import GRID

    return [{"dwid": d, "vlen": v} for (v, d) in sorted(GRID)]


def _walk(cls, params: dict, name: str) -> list:
    """``[(cls_name, key, params), ...]`` for one elaborated design."""
    top = elaborate(cls, params, name=name)
    return [(i.cls_name, i.key, dict(i.params)) for _p, _c, i in walk_modules(top)]


def _design_keys() -> dict:
    """``{example: {cls_name: {key: params}}}`` over each example's whole corpus grid."""
    from examples.fir_block.fir_block import FirBlock
    from examples.vecmult.vecmult import VecMult

    out: dict = {}
    for label, cls, points, name in (("fir_block", FirBlock, _fir_points(), "fir_block"),
                                     ("vecmult", VecMult, _vec_points(), "vec_mult")):
        per: dict = {}
        for p in points:
            for cls_name, key, params in _walk(cls, p, name):
                per.setdefault(cls_name, {})[key] = params
        out[label] = {c: dict(sorted(k.items())) for c, k in sorted(per.items())}
    return out


# ---------------------------------------------------------------------------
# P0 — the keys are pinned
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def golden() -> dict:
    if not GOLDEN.is_file():
        pytest.skip(f"no key snapshot at {GOLDEN}; run with --regenerate")
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_module_keys_are_unchanged(golden):
    """A key move is a **data migration**, so it must never happen by accident.

    Every stored measurement is addressed by these strings.  A refactor that shifts one has invalidated
    a library, and the only difference between a deliberate move and an accident is whether this file
    was regenerated on purpose.
    """
    assert _design_keys() == golden, (
        "module keys moved. If this is a deliberate signature change, regenerate the snapshot AND "
        "migrate the committed stores in the same commit — see plans/key_stability.md.")


# ---------------------------------------------------------------------------
# P1 — a calibration model is not structure
# ---------------------------------------------------------------------------

def test_no_calibration_model_reaches_a_signature():
    """The leak itself, named rather than inferred from a key that moved.

    Checked on the serialized signature rather than on the key, so a failure says *what* leaked
    instead of only that something did.
    """
    from examples.fir_block.fir_block import FirBlock
    from examples.vecmult.vecmult import VecMult

    leaks: dict = {}
    for cls, params, name in ((FirBlock, _fir_points()[0], "fir_block"),
                              (VecMult, _vec_points()[0], "vec_mult")):
        top = elaborate(cls, params, name=name)
        for _p, comp, ident in walk_modules(top):
            text = _serialize(structure_signature(comp))
            for marker in ("CalibModel", "TimingModel", "ResourceModel"):
                if marker in text:
                    leaks.setdefault(ident.cls_name, set()).add(marker)
    assert not leaks, (
        "a calibration model reached a structure signature: "
        f"{ {k: sorted(v) for k, v in leaks.items()} }. A model predicts something *about* the "
        "hardware; it is not part of it, so a key that moves when a coefficient moves is not "
        "addressing structure.")


def test_a_model_under_a_brand_new_name_does_not_leak():
    """The guarantee excluding-by-name could never give.

    The original leak was a model held under an attribute no exclusion list covered.  Renaming that
    attribute, or adding it to the list, fixes one instance; what matters is that the *next* one
    cannot happen.  So this attaches a model under a name nobody has ever excluded and asserts both
    that it stays out of the signature and that the key does not move.
    """
    from waveflow.calib.calib import LinCalibModel
    from waveflow.calib.module_key import signature_digest
    from examples.fir_block.fir_block import FirBlock

    top = elaborate(FirBlock, _fir_points()[0], name="fir_block")
    comp = next(c for _p, c, i in walk_modules(top) if i.cls_name == "FirCompute")
    before = signature_digest(comp)

    comp.some_attribute_no_list_knows_about = LinCalibModel(
        basis=["q"], target="z", seed={"q": 1.0})

    assert "CalibModel" not in _serialize(structure_signature(comp))
    assert signature_digest(comp) == before, (
        "attaching a model under a new attribute name moved the key — the exclusion is still "
        "happening by name somewhere.")


def test_mutating_a_model_does_not_move_a_key():
    """The invariant, asserted directly rather than via its symptom.

    This is what would have caught the original: refitting, reseeding or refactoring a model must
    leave every key alone.  Perturbing a live model instance stands in for the field-layout change
    that actually did the damage.
    """
    from examples.fir_block.fir_block import FirBlock

    top = elaborate(FirBlock, _fir_points()[0], name="fir_block")
    before = {i.cls_name: i.key for _p, _c, i in walk_modules(top)}

    touched = 0
    for _p, comp, _i in walk_modules(top):
        for attr in ("compute_timing", "_timing_model"):
            model = getattr(comp, attr, None)
            if model is None:
                continue
            touched += 1
            model.seed = {"n": 999.0, "intercept": 999.0}
            if hasattr(model, "_coef"):
                model._coef = None
            model.name = "perturbed"
    assert touched, "no model found to perturb — this test would pass vacuously"

    after = {i.cls_name: i.key for _p, _c, i in walk_modules(top)}
    assert after == before, (
        "perturbing a calibration model moved a module key. The signature is sensitive to something "
        "that is not structure.")


# ---------------------------------------------------------------------------
# P3 — every walked module resolves in the store it ships with
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("example,platform,composite", [
    ("fir_block", FIR_PLATFORM, "FirBlock"),
    ("vecmult", VEC_PLATFORM, None),
])
def test_every_leaf_key_resolves_in_the_committed_store(example, platform, composite):
    """A store whose keys a current elaboration cannot reach is a library that silently answers zero.

    Asserted over **leaves** only: a composite legitimately has no records of its own until the
    integration term is filed (``plans/integration_record.md``), and asserting over it would make this
    fail for a reason it is not about.
    """
    if not platform.is_dir():
        pytest.skip(f"{example} platform library not present")
    have = set(ModuleStore(platform).keys())
    keys = _design_keys()[example]

    missing = {cls: sorted(k for k in per if k not in have)
               for cls, per in keys.items() if cls != composite}
    missing = {c: ks for c, ks in missing.items() if ks}
    assert not missing, (
        f"{example}: module keys with no record in the committed store: "
        f"{ {c: f'{len(ks)} key(s)' for c, ks in missing.items()} }. Either the store is stale or a "
        f"signature moved; see plans/key_stability.md.")


if __name__ == "__main__":  # pragma: no cover - regeneration entry point
    import sys

    if "--regenerate" in sys.argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(_design_keys(), indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(f"wrote {GOLDEN.relative_to(REPO)}")
    else:
        print(json.dumps(_design_keys(), indent=2, sort_keys=True))
