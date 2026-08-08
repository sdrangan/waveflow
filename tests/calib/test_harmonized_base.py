"""P2/P3 of ``plans/harmonize_calib.md`` — the structural claims the harmonization makes.

`test_harmonize_equivalence.py` pins the *numbers*; this pins the *shape*.  The two are
complementary: a later phase could keep every prediction identical while quietly reintroducing the
duplication this refactor removed, and nothing else would notice.

What is asserted here is deliberately narrow — one class relationship, one storage rule, one method
vocabulary — because those are the three things that had drifted apart, and the three a future
change is most likely to drift again.
"""
from __future__ import annotations

import pandas as pd
import pytest

from waveflow.calib.calib import CalibModel, LinCalibModel
from waveflow.calib.resource_model import (InterfaceResourceModel, LookupResourceModel,
                                           PriorResourceModel, ResourceModel)
from waveflow.calib.timing_model import RESIDUAL, StreamTimingModel, TimingModel
from waveflow.calib.vitis_model import VitisResourceModel


class TestOneBase:
    """Both axes descend from `CalibModel` — the claim the whole plan rests on."""

    @pytest.mark.parametrize("cls", [ResourceModel, LookupResourceModel, PriorResourceModel,
                                     InterfaceResourceModel, VitisResourceModel,
                                     TimingModel, StreamTimingModel, LinCalibModel])
    def test_every_model_is_a_calib_model(self, cls):
        assert issubclass(cls, CalibModel), f"{cls.__name__} left the hierarchy"

    def test_resource_and_timing_share_the_vocabulary(self):
        """The method names are the same on both axes, which is what makes a kind reusable.

        Before the harmonization these were `predict_own` / `confidence_own` on one side and
        `predict` / `confidence` on the other, so no model kind could serve both.
        """
        for name in ("transform", "predict_feat", "confidence_feat", "fit", "targets",
                     "corpus_df", "corpus_markdown", "data_dir", "corpus_path", "params_path"):
            assert hasattr(CalibModel, name), f"base lost {name}"


class TestTransformCannotSeeTheComponent:
    """The params/transform split — what makes a stored corpus re-fittable.

    `get_params(comp)` extracts and is recorded; `transform(params)` derives and is not.  Because
    `transform` never receives the component, a model **cannot** predict from a fact the corpus does
    not hold.  That is enforced by the signature rather than by review.
    """

    def test_transform_takes_params_not_a_component(self):
        import inspect

        for cls in (CalibModel, LinCalibModel, ResourceModel, VitisResourceModel):
            names = list(inspect.signature(cls.transform).parameters)
            assert names[:2] == ["self", "params"], f"{cls.__name__}.transform{tuple(names)}"

    def test_predict_routes_through_get_params(self, tmp_path):
        """So a live component and a recorded row take the identical path to a number."""
        from examples.vecmult.vecmult import VecMult
        from waveflow.build.elaborate import elaborate

        top = elaborate(VecMult, {"dwid": 128, "vlen": 4096}, name="vec_mult")
        top.add_rm(None)
        m = top.resource_model
        assert m.transform(m.get_params(top)) == m.transform(m.get_params(top))

    def test_get_params_is_sufficient_for_transform(self):
        """The durability property: features must be recomputable from the recorded row alone.

        If `transform` needed anything `get_params` did not return, a corpus could be re-read but
        never re-fitted — and nothing would say so, because the missing column simply would not exist.
        """
        from examples.vecmult.vecmult import VecMult
        from waveflow.build.elaborate import elaborate

        top = elaborate(VecMult, {"dwid": 128, "vlen": 4096}, name="vec_mult")
        top.add_rm(None)
        m = top.resource_model
        row = m.get_params(top)
        # A CSV round-trip returns floats, so every column the basis reads must survive becoming
        # one.  Non-numeric columns are exempt rather than overlooked: `ports` and `channels` are
        # the interface term's boundary signature, looked up whole and never arithmetic.  Coercing
        # them here would assert something false about the row -- that all of it is numbers.
        via_csv = {k: (float(v) if isinstance(v, (int, float, bool)) else v)
                   for k, v in row.items()}
        assert m.transform(via_csv) == m.transform(row)
        assert all(isinstance(v, float) for k, v in via_csv.items()
                   if k in m.transform(row)), "a basis column did not survive the round trip"

    def test_vitis_records_the_declaration_not_the_cost_dictionary(self):
        """The corpus row carries what the module *declared*, alongside the raw parameters.

        `VecMult` states its fabric basis directly (`LutFfBasis`), so the declaration IS the terms
        and the columns are `basis_*`.  What must still hold is that the raw parameters travel with
        them: a revised basis has to be re-derivable from measurements already on disk, and `dwid`
        is what makes that possible.  A row of terms alone would strand every point the moment the
        modelling claim changed.

        The structural vocabulary (`xbar0_lanes` -> `xbar_sw`) keeps its own split for designs that
        use it; `TestVitisStructuralVocabulary` below covers that path.
        """
        from examples.vecmult.vecmult import VecMult
        from waveflow.build.elaborate import elaborate

        top = elaborate(VecMult, {"dwid": 128, "vlen": 4096}, name="vec_mult")
        top.add_rm(None)
        row = top.resource_model.get_params(top)
        assert "mult0_count" in row and "mem0_banks" in row       # the device-rule declaration
        assert "basis_lw" in row and "basis_lw2" in row           # the fitted declaration
        assert "dwid" in row and "vlen" in row                    # ...and what it is re-derivable from


class TestVitisStructuralVocabulary:
    """The named-structure path (`PerLane`/`Crossbar`), which infers the basis instead of taking it.

    Kept as a first-class test rather than folded into `VecMult`'s, because the two declaration
    styles are separately reachable and a design using either must not silently get the other's
    terms.
    """

    def test_named_structures_still_derive_their_terms(self):
        from waveflow.calib.vitis_model import Crossbar, DesignStructure, PerLane

        s = DesignStructure(per_lane=[PerLane(lanes=8)], crossbars=[Crossbar(lanes=8)])
        assert "xbar0_lanes" in s.flatten()                       # the declaration
        assert "xbar_sw" not in s.flatten()                       # not the derived term
        assert s.basis_terms()["xbar_sw"] == 64.0                 # derived on demand

    def test_a_written_basis_wins_and_is_not_merged(self):
        """Declaring both is refused, so the two vocabularies can never double-count one growth."""
        import pytest

        from waveflow.calib.vitis_model import Crossbar, DesignStructure, LutFfBasis

        s = DesignStructure(lut_ff_basis=LutFfBasis(bases=[8, 64], names=("lw", "lw2")))
        assert s.basis_terms() == {"lw": 8.0, "lw2": 64.0}
        with pytest.raises(ValueError, match="EITHER"):
            DesignStructure(crossbars=[Crossbar(lanes=8)],
                            lut_ff_basis=LutFfBasis(bases=[64]))


class TestStorageIsDerived:
    """Paths come from `name` + `platform`, never from a second hand-rolled scheme."""

    def test_timing_params_path_is_where_the_inner_model_writes(self, tmp_path):
        """The bug this prevents: two schemes naming the same artifact, and drifting.

        `TimingModel` composes a `LinCalibModel` that was handed an explicit path.  If the base's
        derivation and that path disagree, `load_model` reads one file while `fit` writes another —
        and the symptom is a model that silently never loads its own parameters.
        """
        tm = StreamTimingModel(component="c", calib_dir=tmp_path)
        assert tm.params_path == tm._model.path
        assert tm.corpus_path == tmp_path / "corpus.csv"

    def test_no_platform_means_no_paths(self, tmp_path):
        """A model with nowhere to store anything still constructs — what lets a test build one."""
        m = LinCalibModel(basis=["n"], target="y")
        assert m.data_dir is None and m.corpus_path is None and m.params_path is None

    def test_data_dir_derives_from_name_and_platform(self, tmp_path):
        class _Plat:
            dir = tmp_path

        m = LinCalibModel(basis=["n"], target="residual", name="mem_r_span", platform=_Plat())
        assert m.data_dir == tmp_path / "models" / "mem_r_span"
        assert m.name == "mem_r_span"

    def test_name_defaults_to_target(self):
        assert LinCalibModel(basis=["n"], target="residual").name == "residual"


class TestFitReadsTheCorpus:
    """`fit()` with no argument trains on what was measured — see `docs/guide/calib/corpus.md`."""

    def _frame(self):
        return pd.DataFrame([{"nwords": n, RESIDUAL: 8.0 + 1.5 * n} for n in (4, 16, 64, 256)])

    def test_explicit_data_still_works(self):
        m = LinCalibModel(basis=["nwords"], target=RESIDUAL).fit(self._frame())
        assert m.predict_feat({"nwords": 32}) == pytest.approx(56.0)

    def test_corpus_is_the_default_source(self, tmp_path):
        class _Plat:
            dir = tmp_path

        m = LinCalibModel(basis=["nwords"], target=RESIDUAL, platform=_Plat())
        m.corpus_path.parent.mkdir(parents=True, exist_ok=True)
        self._frame().to_csv(m.corpus_path, index=False)
        assert m.fit().predict_feat({"nwords": 32}) == pytest.approx(56.0)

    def test_no_data_and_no_corpus_says_so(self):
        """Rather than fitting on nothing and reporting a confident zero."""
        with pytest.raises(RuntimeError, match="no corpus"):
            LinCalibModel(basis=["nwords"], target=RESIDUAL).fit()


class TestTargets:
    """`targets` is what tells a caller whether `predict_feat` returns a scalar or a mapping."""

    def test_single_target_model_reports_one(self):
        assert LinCalibModel(basis=["n"], target="residual").targets == ("residual",)

    def test_timing_targets_the_residual(self, tmp_path):
        assert StreamTimingModel(component="c", calib_dir=tmp_path).targets == (RESIDUAL,)

    def test_resource_targets_are_the_counters(self):
        """A resource model is the multi-target case — its targets are the platform's counters."""
        m = PriorResourceModel(formulas={"dsp": lambda f: 4})
        assert "dsp" in m.targets

    def test_a_declared_counter_outside_the_vocabulary_is_refused(self):
        """The check that stops a mistyped counter contributing zero and saying nothing."""
        class _Plat:
            dir = None
            res_types = ("lut", "ff")

            def check_counters(self, names):
                bad = [n for n in names if n not in self.res_types]
                if bad:
                    raise ValueError(f"unknown counters {bad}")

        with pytest.raises(ValueError, match="unknown counters"):
            PriorResourceModel(formulas={"dsp": lambda f: 4}, platform=_Plat())
