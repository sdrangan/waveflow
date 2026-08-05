"""P4 of ``plans/harmonize_calib.md`` — the claim the whole harmonization rests on.

If timing and resource models are really *one* kind of thing, then a model kind must not be able to
tell which axis its data came from.  This file is the check: the **same** `LookupCalibModel` class,
fitted once on timing rows and once on resource rows, must behave identically — same memorization,
same refusal to interpolate, same confidence transitions, same round-trip.

Everything else in the plan is a refactor that could be argued for on tidiness grounds.  This is the
part that would be *false* if the unification were wrong, which is why it gets its own file.

The second half covers `corpus_from_records`: the resource axis's raw-tier reducer, without which
"both axes share a corpus format" is an aspiration rather than a fact.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from waveflow.calib.calib import (ConcatCalibModel, LinCalibModel, LookupCalibModel,
                                  PriorCalibModel)
from waveflow.calib.confidence import ConfidenceLevel
from waveflow.calib.module_key import ModuleIdentity
from waveflow.calib.record_store import ModuleStore, corpus_from_records, resource_record

REPO = Path(__file__).resolve().parents[2]
FIR_PLATFORM = REPO / "examples" / "fir_block" / "calib" / "platforms" / "zynq7020_bfm_100mhz"


def _timing_rows() -> pd.DataFrame:
    """A residual corpus: two features, one target."""
    return pd.DataFrame([{"nwords": n, "num_trans": k, "residual": 8.0 + 1.5 * n + 3.0 * k}
                         for n in (4, 16, 64) for k in (1, 2)])


def _resource_rows() -> pd.DataFrame:
    """A utilization corpus: two features, three targets.  Same shape, different axis."""
    return pd.DataFrame([{"ntap": n, "samp_w": w, "lut": 100 * n, "ff": 50 * n, "dsp": n // 2}
                         for n in (8, 16, 32) for w in (12, 16)])


class TestOneKindServesBothAxes:
    """`LookupCalibModel` cannot tell which axis it was fitted on."""

    def test_both_memorize_every_measured_point(self):
        tm = LookupCalibModel(basis=["nwords", "num_trans"], target="residual").fit(_timing_rows())
        rm = LookupCalibModel(basis=["ntap", "samp_w"],
                              target_names=("lut", "ff", "dsp")).fit(_resource_rows())
        assert len(tm.table) == len(_timing_rows())
        assert len(rm.table) == len(_resource_rows())

    def test_both_report_exact_where_measured(self):
        tm = LookupCalibModel(basis=["nwords", "num_trans"], target="residual").fit(_timing_rows())
        rm = LookupCalibModel(basis=["ntap", "samp_w"],
                              target_names=("lut", "ff", "dsp")).fit(_resource_rows())
        assert tm.confidence_feat({"nwords": 16, "num_trans": 2}).level is ConfidenceLevel.EXACT
        assert rm.confidence_feat({"ntap": 16, "samp_w": 12}).level is ConfidenceLevel.EXACT

    def test_both_refuse_to_interpolate(self):
        """Between two measured points the truth can be anywhere — so neither axis guesses."""
        tm = LookupCalibModel(basis=["nwords", "num_trans"], target="residual").fit(_timing_rows())
        rm = LookupCalibModel(basis=["ntap", "samp_w"],
                              target_names=("lut", "ff", "dsp")).fit(_resource_rows())
        assert tm.confidence_feat({"nwords": 17, "num_trans": 2}).level is ConfidenceLevel.UNCALIBRATED
        assert rm.confidence_feat({"ntap": 17, "samp_w": 12}).level is ConfidenceLevel.UNCALIBRATED

    def test_a_gap_is_never_a_silent_zero(self):
        """It returns 0 so a composed estimate still sums — but always says UNCALIBRATED with it."""
        tm = LookupCalibModel(basis=["nwords"], target="residual").fit(
            _timing_rows().drop(columns=["num_trans"]))
        assert tm.predict_feat({"nwords": 999}) == 0.0
        assert tm.confidence_feat({"nwords": 999}).level is ConfidenceLevel.UNCALIBRATED

    def test_return_shape_follows_target_count(self):
        """Scalar for one target, mapping for several — the scikit-learn multi-output convention.

        The shape follows `len(targets)`, which is **declared at construction**, so a caller knows it
        statically and never has to inspect a return value. It does *not* follow the axis: a
        single-counter resource lookup returns a scalar too, which is why this is pinned rather than
        assumed. (Open decision 2 in `plans/harmonize_calib.md` — whether to make it uniformly a
        mapping — would change this test on purpose.)
        """
        tm = LookupCalibModel(basis=["nwords", "num_trans"], target="residual").fit(_timing_rows())
        rm = LookupCalibModel(basis=["ntap", "samp_w"],
                              target_names=("lut", "ff", "dsp")).fit(_resource_rows())
        one_counter = LookupCalibModel(basis=["ntap", "samp_w"],
                                       target_names=("lut",)).fit(_resource_rows())
        assert isinstance(tm.predict_feat({"nwords": 16, "num_trans": 2}), float)
        assert isinstance(rm.predict_feat({"ntap": 16, "samp_w": 12}), dict)
        assert isinstance(one_counter.predict_feat({"ntap": 16, "samp_w": 12}), float)

    @pytest.mark.parametrize("basis,target_kw,rows", [
        (["nwords", "num_trans"], {"target": "residual"}, _timing_rows()),
        (["ntap", "samp_w"], {"target_names": ("lut", "ff", "dsp")}, _resource_rows()),
    ])
    def test_params_round_trip_on_either_axis(self, basis, target_kw, rows):
        """A committed table must reload to the same predictions — the artifact contract."""
        a = LookupCalibModel(basis=basis, **target_kw).fit(rows)
        b = LookupCalibModel(basis=basis, **target_kw).load_params(a.to_params())
        probe = {k: rows.iloc[1][k] for k in basis}
        assert b.predict_feat(probe) == a.predict_feat(probe)

    def test_an_int_and_a_float_are_the_same_point(self):
        """A CSV round-trip yields 4.0 where a live component yields 4.

        Without normalization those become two table entries, and every prediction from the live side
        misses — reporting UNCALIBRATED for a point that *was* measured.
        """
        m = LookupCalibModel(basis=["nwords"], target="residual").fit(
            pd.DataFrame([{"nwords": 4, "residual": 10.0}]))
        assert m.predict_feat({"nwords": 4.0}) == 10.0
        assert m.confidence_feat({"nwords": 4.0}).level is ConfidenceLevel.EXACT

    def test_a_repeated_point_supersedes(self):
        """Re-measuring overwrites, so a corpus need not be pruned by hand."""
        m = LookupCalibModel(basis=["nwords"], target="residual").fit(
            pd.DataFrame([{"nwords": 4, "residual": 10.0}, {"nwords": 4, "residual": 12.0}]))
        assert len(m.table) == 1
        assert m.predict_feat({"nwords": 4}) == 12.0

    def test_a_missing_basis_column_is_loud(self):
        """Rather than keying on a partial point and quietly never matching."""
        m = LookupCalibModel(basis=["nwords", "num_trans"], target="residual").fit(_timing_rows())
        with pytest.raises(KeyError, match="num_trans"):
            m.predict_feat({"nwords": 16})


class TestPriorCalibModel:
    """A formula with zero free parameters — the strongest claim available here."""

    def _m(self):
        return PriorCalibModel(name="dsp_rule", formulas={"dsp": lambda f: 2 * int(f["n_mult"])})

    def test_predicts_from_the_formula(self):
        assert self._m().predict_feat({"n_mult": 4}) == 8

    def test_has_no_free_parameters(self):
        """Which is what makes its EXACT stronger than a regression's."""
        m = self._m()
        assert m.n_free_params() == 0 and m.has_free_params is False

    def test_reports_exact_without_a_corpus(self):
        """The claim is *this is the rule*, not *this was fitted*."""
        assert self._m().confidence_feat({"n_mult": 4}).level is ConfidenceLevel.EXACT

    def test_fit_checks_the_formula_rather_than_tuning_it(self):
        """`fit` moves nothing but records the residual, so a wrong rule is contradictable."""
        good = pd.DataFrame([{"n_mult": n, "dsp": 2 * n} for n in (2, 4, 8)])
        m = self._m().fit(good)
        assert m.predict_feat({"n_mult": 4}) == 8              # unchanged by fitting
        assert m._fit_summary.max_abs_residual == 0.0

        wrong = pd.DataFrame([{"n_mult": n, "dsp": 3 * n} for n in (2, 4, 8)])
        assert self._m().fit(wrong)._fit_summary.max_abs_residual > 0.0

    def test_predicts_only_what_it_claims(self):
        """A prior for DSP says nothing about LUT, rather than implying zero."""
        assert self._m().targets == ("dsp",)


class TestConcatCalibModel:
    """Each target from whichever model is honest for it."""

    def _rows(self):
        return pd.DataFrame([{"n_mult": n, "dsp": 2 * n, "lut": 100 + 30 * n}
                             for n in (2, 4, 8, 16)])

    def _m(self):
        return ConcatCalibModel(name="vec", models=(
            PriorCalibModel(name="dsp_rule", formulas={"dsp": lambda f: 2 * int(f["n_mult"])}),
            LinCalibModel(basis=["n_mult"], target="lut", name="lut_fit"),
        ))

    def test_targets_are_the_union(self):
        assert set(self._m().targets) == {"dsp", "lut"}

    def test_each_target_comes_from_its_owner(self):
        m = self._m().fit(self._rows())
        got = m.predict_feat({"n_mult": 6})
        assert got["dsp"] == 12                                 # the rule, exactly
        assert got["lut"] == pytest.approx(280.0)               # the regression

    def test_free_params_exclude_the_prior(self):
        m = self._m().fit(self._rows())
        assert m.n_free_params() == 2                           # the fit's slope + intercept only

    def test_confidence_is_the_weakest_link_and_names_it(self):
        """Not an average — an estimate is only as believable as its least believable part."""
        m = self._m().fit(self._rows())
        far = m.confidence_feat({"n_mult": 999})
        assert far.level is ConfidenceLevel.EXTRAPOLATED
        assert far.facts["weakest"] == ["lut"]                  # the prior is still exact out there
        assert far.facts["per_target"]["dsp"]["level"] == "EXACT"

    def test_earlier_model_wins_a_contested_target(self):
        """Precedence is positional, so it is visible at the construction site."""
        m = ConcatCalibModel(models=(
            PriorCalibModel(name="a", formulas={"dsp": lambda f: 1}),
            PriorCalibModel(name="b", formulas={"dsp": lambda f: 99}),
        ))
        assert m.predict_feat({}) == {"dsp": 1}

    def test_get_params_is_the_union_so_one_row_serves_every_sub_model(self):
        """Otherwise the concat would be unfittable from its own corpus."""
        from examples.vecmult.vecmult import VecMult
        from waveflow.build.elaborate import elaborate

        top = elaborate(VecMult, {"dwid": 128, "vlen": 4096}, name="vec_mult")
        a = PriorCalibModel(name="a", formulas={"dsp": lambda f: 1})
        m = ConcatCalibModel(models=(a, LinCalibModel(basis=["dwid"], target="lut", name="b")))
        assert set(m.get_params(top)) >= set(a.get_params(top))

    def test_params_round_trip(self):
        a = self._m().fit(self._rows())
        b = self._m().load_params(a.to_params())
        assert b.predict_feat({"n_mult": 6}) == pytest.approx(a.predict_feat({"n_mult": 6}))


class TestLookupResourceModelKeepsItsIdentity:
    """Decision 9: share the machinery, keep the module-key identity."""

    def test_it_is_a_lookup_calib_model(self):
        from waveflow.calib.resource_model import LookupResourceModel

        assert issubclass(LookupResourceModel, LookupCalibModel)

    def test_it_keys_on_the_module_key_not_the_parameter_tuple(self):
        """The finer partition, and the safe one.

        A bound FIFO depth is physical, reaches the structure signature, and is no `HwParam` — so two
        instances with identical parameters and different wiring collide under a parameter key. Under
        a module key they stay distinct, and the second measurement cannot silently overwrite the
        first.
        """
        from waveflow.calib.resource_model import LookupResourceModel

        assert LookupResourceModel().basis == ["module_key"]

    def test_a_committed_table_may_use_bare_string_keys(self):
        """`{module_key: counters}` is the natural spelling; it must not silently never match."""
        from waveflow.calib.resource_model import LookupResourceModel

        m = LookupResourceModel(table={"blk-1234": {"lut": 833}})
        assert m.predict_feat({"module_key": "blk-1234"})["lut"] == 833
        assert m.confidence_feat({"module_key": "blk-1234"}).level is ConfidenceLevel.EXACT
        assert m.confidence_feat({"module_key": "other"}).level is ConfidenceLevel.UNCALIBRATED


class TestCorpusFromRecords:
    """The resource axis's raw-tier reducer — what gives resources a corpus at all."""

    def _store(self, tmp_path, rows):
        store = ModuleStore(tmp_path)
        for i, (params, res) in enumerate(rows):
            ident = ModuleIdentity(key=f"blk-{i:04d}", cls_name="Blk", cls_module="m",
                                   params=params, signature=f"sig{i}")
            store.append(resource_record(ident, res, source="hls_estimate"), identity=ident)
        return store

    def test_one_row_per_module_with_params_and_counters(self, tmp_path):
        store = self._store(tmp_path, [({"ntap": 8}, {"LUT": 100, "FF": 50}),
                                       ({"ntap": 16}, {"LUT": 200, "FF": 90})])
        db = corpus_from_records(store, cls_name="Blk")
        assert len(db) == 2
        assert {"ntap", "lut", "ff", "measured_at"} <= set(db.df.columns)
        assert sorted(db.df["lut"]) == [100, 200]

    def test_it_is_fit_ready(self, tmp_path):
        """The point of the reducer: the frame it returns can be fitted with no translation."""
        store = self._store(tmp_path, [({"ntap": n}, {"LUT": 100 * n, "FF": 50 * n})
                                       for n in (8, 16, 32)])
        db = corpus_from_records(store, cls_name="Blk")
        m = LookupCalibModel(basis=["ntap"], target_names=("lut", "ff")).fit(db)
        assert m.predict_feat({"ntap": 16}) == {"lut": 1600, "ff": 800}

    def test_a_foreign_class_is_excluded(self, tmp_path):
        """Two classes have different parameter names; mixing them yields unfittable blanks."""
        store = self._store(tmp_path, [({"ntap": 8}, {"LUT": 100})])
        other = ModuleIdentity(key="oth-0001", cls_name="Other", cls_module="m",
                               params={"width": 4}, signature="sigX")
        store.append(resource_record(other, {"LUT": 7}, source="hls_estimate"), identity=other)
        assert len(corpus_from_records(store, cls_name="Blk")) == 1
        assert len(corpus_from_records(store)) == 2          # no filter -> everything

    def test_empty_store_yields_an_empty_corpus(self, tmp_path):
        """Not an exception — a caller decides fit-or-skip on `len`."""
        assert len(corpus_from_records(ModuleStore(tmp_path), cls_name="Blk")) == 0

    def test_provenance_travels_but_is_not_a_feature(self, tmp_path):
        """`module_key` / `measured_source` make a row traceable; a basis is chosen by name, so
        they can never leak into a fit."""
        store = self._store(tmp_path, [({"ntap": 8}, {"LUT": 100})])
        df = corpus_from_records(store, cls_name="Blk").df
        assert df.iloc[0]["measured_source"] == "hls_estimate"
        assert df.iloc[0]["module_key"].startswith("blk-")

    def test_new_records_carry_a_measurement_time(self, tmp_path):
        store = self._store(tmp_path, [({"ntap": 8}, {"LUT": 100})])
        assert corpus_from_records(store, cls_name="Blk").df.iloc[0]["measured_at"]


class TestAgainstTheCommittedStore:
    """The reducer against real committed data, not a fixture — 35 modules of `fir_block`."""

    @pytest.fixture(scope="class")
    def store(self):
        if not FIR_PLATFORM.is_dir():
            pytest.skip("committed fir_block platform not present")
        return ModuleStore(FIR_PLATFORM)

    def test_fir_compute_reduces_to_a_fittable_corpus(self, store):
        db = corpus_from_records(store, cls_name="FirCompute")
        assert len(db) >= 20
        for col in ("ntap", "samp_w", "unroll_lane", "lut", "ff", "dsp"):
            assert col in db.df.columns, col

    def test_a_lookup_over_it_reproduces_every_measured_point(self, store):
        """Zero free assumptions, so it must reproduce the corpus exactly or the reducer is wrong."""
        db = corpus_from_records(store, cls_name="FirCompute")
        basis = ["mem_dwidth", "ntap", "samp_i", "samp_w", "unroll_lane"]
        m = LookupCalibModel(basis=basis, target_names=("lut", "ff", "dsp", "bram")).fit(db)
        for row in db.df.to_dict("records"):
            got = m.predict_feat({k: row[k] for k in basis})
            assert got["lut"] == int(row["lut"])
            assert got["dsp"] == int(row["dsp"])
        assert m._fit_summary.max_abs_residual == 0.0

    def test_older_records_carry_a_blank_time_rather_than_a_fabricated_one(self, store):
        """These predate `measured_at`.  A plausible invented date would be worse than a gap."""
        db = corpus_from_records(store, cls_name="FirCompute")
        assert (db.df["measured_at"] == "").all()


# ---------------------------------------------------------------------------
# The integration term — a measurement, not a constant
# ---------------------------------------------------------------------------

VEC_PLATFORM = REPO / "examples" / "vecmult" / "calib" / "platforms" / "zynq7020_vecmult"


class TestIntegrationRecords:
    """P1-P4 of ``plans/integration_record.md`` — the third additive term is stored like the others.

    ``top = Σ(modules) + integration``.  Two of those were durable and the third was not, so both
    examples transcribed it into source.  On ``fir_block`` that term is 29% of the design and was the
    only number in the estimate with no provenance behind it.
    """

    @pytest.fixture(scope="class")
    def store(self):
        from waveflow.calib.record_store import ModuleStore

        if not VEC_PLATFORM.is_dir():
            pytest.skip("vecmult platform library not present")
        return ModuleStore(VEC_PLATFORM)

    def test_the_term_is_filed_at_all(self, store):
        from waveflow.calib.record_store import INTEGRATION_TARGET, corpus_from_records

        db = corpus_from_records(store, cls_name="VecMult", target=INTEGRATION_TARGET)
        assert len(db) >= 16, f"only {len(db)} integration record(s); expected one per synthesis"

    def test_it_is_never_summed_as_a_module(self, store):
        """A ``resource`` read never returns the composite's own cost, and vice versa.

        For a **single-task** design the top and its only module share a key, so both kinds of
        measurement are filed against it — legitimately.  What keeps them from being summed together
        is the separate ``target``, not a separate key, which is exactly why the term is a target
        rather than a flag inside a ``resource`` payload: a caller that forgets the distinction reads
        an empty list rather than a double-counted design.
        """
        from waveflow.calib.record_store import INTEGRATION_TARGET

        shared = 0
        for key in store.keys():
            resource = store.read(key, "resource", verify=False)
            integ = store.read(key, INTEGRATION_TARGET, verify=False)
            if resource and integ:
                shared += 1
            # whatever a key holds, the two reads must not return each other's rows
            assert all(r.target == "resource" for r in resource)
            assert all(r.target == INTEGRATION_TARGET for r in integ)
        assert shared, (
            "no key carries both — this test is meant to cover the single-task case where the top "
            "and its only module share a key, and would pass vacuously otherwise")

    def test_negative_is_preserved(self, store):
        """VecMult's term is negative — HLS reclaims two LUTs flattening its single task.

        Nothing clamps it.  A negative own-cost is the signal that additivity is leaking across a
        module boundary, and hiding it would hide what whole-design synthesis exists to catch.
        """
        from waveflow.calib.record_store import INTEGRATION_TARGET, corpus_from_records

        db = corpus_from_records(store, cls_name="VecMult", target=INTEGRATION_TARGET)
        assert set(db.df["lut"]) == {-2}, f"expected a constant -2, got {sorted(set(db.df['lut']))}"

    def test_the_invariance_is_derived_rather_than_asserted(self, store):
        """One distinct value across every measured point — checked, not claimed in a docstring.

        This is why one record is filed per *synthesis* rather than one per distinct value: a point
        that broke the invariance would appear here as a second value instead of silently
        contradicting a comment.
        """
        from waveflow.calib.record_store import INTEGRATION_TARGET, corpus_from_records

        db = corpus_from_records(store, cls_name="VecMult", target=INTEGRATION_TARGET)
        for counter in ("lut", "ff", "dsp", "bram"):
            if counter in db.df.columns:
                assert len(set(db.df[counter])) == 1, (
                    f"{counter} integration is not invariant across the grid: "
                    f"{sorted(set(db.df[counter]))}")

    def test_the_model_builds_its_table_from_the_store(self, store):
        """And deduplicates 16 records to one entry per boundary."""
        from waveflow.calib.resource_model import InterfaceResourceModel

        m = InterfaceResourceModel(name="shell", store=store, cls_name="VecMult").load_table()
        assert len(m.table) == 4, f"expected one entry per port width, got {len(m.table)}"
        assert all(t.get("lut") == -2 for t in m.table.values())

    def test_a_contradicted_boundary_raises(self, store):
        """Two different measurements for one boundary is a finding, not something to average."""
        from waveflow.calib.resource_model import InterfaceResourceModel

        m = InterfaceResourceModel(name="shell", store=store, cls_name="VecMult")
        rows = [{"ports": [["StreamIFMaster", 32], ["StreamIFSlave", 32]], "channels": [], "lut": -2},
                {"ports": [["StreamIFMaster", 32], ["StreamIFSlave", 32]], "channels": [], "lut": -9}]

        class _Fake:
            df = pd.DataFrame(rows)

            def __len__(self):
                return len(self.df)

        import waveflow.calib.record_store as rs
        orig = rs.corpus_from_records
        rs.corpus_from_records = lambda *a, **k: _Fake()
        try:
            with pytest.raises(ValueError, match="two different integration measurements"):
                m.table_from_store()
        finally:
            rs.corpus_from_records = orig
