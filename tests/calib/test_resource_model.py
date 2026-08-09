"""The ResourceModel kinds: a lookup that refuses to interpolate, and a prior that has nothing to fit.

The composition rule is ``predict(comp) = comp's own model + Σ predict(child)`` — a composite's "own"
cost being the adapters, FIFOs and control block it adds beyond its children.  These tests cover the
leaf models; composition itself lands with E1.

The property worth defending here is the lookup's **refusal**.  Most modules in a design do not vary
with the knobs being explored (measured on ``fir_block``: three of four modules resolved to one or four
configurations across a 24-point sweep), so a table is the honest model — and a table that quietly
returned its nearest entry would be the exact mechanism by which an exploration walks into a region
nothing ever measured.
"""
from __future__ import annotations

from dataclasses import dataclass


from waveflow.build.elaborate import elaborate
from waveflow.calib.confidence import ConfidenceLevel
from waveflow.calib.module_key import identify
from waveflow.calib.record_store import ModuleStore, resource_record
from waveflow.calib.resource_model import (
    COUNTERS,
    LookupResourceModel,
    PriorResourceModel,
    ResourceModel,
    add_counters,
    zero_counters,
)
from waveflow.hw.hw_module import HwModule, HwParam


@dataclass(kw_only=True)
class Blk(HwModule):
    width: HwParam[int] = 16

    def __post_init__(self) -> None:
        super().__post_init__()
        self.bits = int(self.width) * 4


def _comp(width=16):
    return elaborate(Blk, {"width": width}, name="blk")


# ---------------------------------------------------------------------------
# Counter arithmetic
# ---------------------------------------------------------------------------

def test_counters_sum_and_missing_are_zero():
    assert add_counters({"lut": 10, "dsp": 2}, {"lut": 5, "ff": 3}) == {"lut": 15, "dsp": 2, "ff": 3}


def test_negative_own_cost_is_preserved():
    """HLS sharing logic across a boundary drives a composite's own term below zero.

    That is the signal additivity is leaking — clamping it would hide exactly what whole-design
    synthesis exists to catch.
    """
    assert add_counters({"lut": 100}, {"lut": -30})["lut"] == 70


def test_unknown_keys_are_ignored():
    assert add_counters({"lut": 1, "not_a_counter": 99}) == {"lut": 1}


def test_zero_counters_covers_every_counter():
    assert set(zero_counters()) == set(COUNTERS)


# ---------------------------------------------------------------------------
# The base
# ---------------------------------------------------------------------------

def test_base_has_no_free_params_and_fit_is_a_noop():
    """Most models here have nothing to fit — that is by construction, not an exclusion flag."""
    m = ResourceModel()
    assert m.has_free_params is False
    assert m.fit([]) is m


def test_base_names_itself():
    assert ResourceModel().name == "ResourceModel"


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def test_lookup_returns_the_stored_measurement_and_calls_it_exact():
    comp = _comp(16)
    key = identify(Blk, {"width": 16}).key
    m = LookupResourceModel(table={key: {"lut": 833, "ff": 472, "dsp": 0, "bram": 0}})

    assert m.predict(comp)["lut"] == 833
    conf = m.confidence(comp)
    assert conf.level is ConfidenceLevel.EXACT
    assert "measured directly" in conf.summary


def test_lookup_refuses_to_interpolate():
    """A key it has not seen is a gap, not an estimate — and it says so."""
    m = LookupResourceModel(table={identify(Blk, {"width": 16}).key: {"lut": 833}})
    conf = m.confidence(_comp(32))
    assert conf.level is ConfidenceLevel.UNCALIBRATED
    assert "cannot interpolate" in conf.summary
    assert m.predict(_comp(32)) == zero_counters()


def test_lookup_reads_a_module_store(tmp_path):
    store = ModuleStore(tmp_path / "platform")
    ident = identify(Blk, {"width": 16})
    store.append(resource_record(ident, {"LUT": 1850, "FF": 1464, "DSP": 0, "BRAM_18K": 0},
                                 source="hls_estimate"), identity=ident)

    m = LookupResourceModel(store=store)
    assert m.predict(_comp(16))["lut"] == 1850
    assert m.confidence(_comp(16)).level is ConfidenceLevel.EXACT


def test_lookup_prefers_the_stronger_source(tmp_path):
    store = ModuleStore(tmp_path / "platform")
    ident = identify(Blk, {"width": 16})
    for src, lut in (("hls_estimate", 100), ("vivado_impl", 180)):
        store.append(resource_record(ident, {"LUT": lut}, source=src), identity=ident)

    m = LookupResourceModel(store=store)
    assert m.predict(_comp(16))["lut"] == 180
    assert m.confidence(_comp(16)).facts["measured_source"] == "vivado_impl"


def test_lookup_is_fitted_like_any_other_model():
    """A lookup **is** a fit — a memorizing one.

    Its training data is ``(configuration, measurement)`` pairs and its fitted parameters are the
    table those pairs become, so it takes the same ``fit(samples)`` every other model does.  What it
    does not do is *interpolate*.  Calling that "nothing to fit" was the framing this replaces: it
    made the one model that most obviously needs data look like it needed none.
    """
    m = LookupResourceModel(res_types=("lut", "ff"))
    assert m.has_free_params is True
    assert m.table == {}

    m.fit([(_comp(16), {"LUT": 833, "FF": 472}),
           (_comp(32), {"LUT": 1650, "FF": 940})])

    assert len(m.table) == 2
    assert m.predict(_comp(16)) == {"lut": 833, "ff": 472}
    assert m.confidence(_comp(16)).level is ConfidenceLevel.EXACT


def test_lookup_fit_accepts_report_spelling():
    """Measurements come out of a synthesis report, so ``LUT``/``BRAM_18K`` must work as given."""
    m = LookupResourceModel(res_types=("lut", "bram")).fit([(_comp(16), {"LUT": 12, "BRAM_18K": 3})])
    assert m.predict(_comp(16)) == {"lut": 12, "bram": 3}


def test_lookup_still_refuses_to_interpolate_after_fitting():
    """Fitting adds rows; it does not grant the model permission to guess between them."""
    m = LookupResourceModel(res_types=("lut",)).fit(
        [(_comp(16), {"LUT": 100}), (_comp(64), {"LUT": 400})])
    assert m.confidence(_comp(32)).level is ConfidenceLevel.UNCALIBRATED
    # zero across the counters this model stores — not across the whole platform vocabulary.
    assert m.predict(_comp(32)) == {"lut": 0}


# ---------------------------------------------------------------------------
# Prior
# ---------------------------------------------------------------------------

def test_prior_computes_from_features_and_claims_exact():
    m = PriorResourceModel(formulas={"dsp": lambda f: f["width"] // 4})
    assert m.predict(_comp(16)) == {"dsp": 4}
    conf = m.confidence(_comp(16))
    assert conf.level is ConfidenceLevel.EXACT
    assert "no fitted parameters" in conf.summary


def test_prior_reads_resolved_params_by_default():
    """Params come off the elaborated instance — nothing is threaded down from a parent."""
    m = PriorResourceModel(formulas={})
    assert m.get_params(_comp(24))["width"] == 24


def test_prior_accepts_an_extraction():
    """`params_fn` may read *structure*, not just parameters — and what it reads is recorded."""
    m = PriorResourceModel(formulas={"lut": lambda f: 10 * f["ports"]},
                           params_fn=lambda c: {"ports": len(getattr(c, "endpoints", {}) or {})})
    assert m.predict(_comp(16)) == {"lut": 0}      # this toy declares no endpoints


def test_prior_accepts_a_derivation():
    """`transform_fn` derives from params — it never sees the component, by construction."""
    m = PriorResourceModel(formulas={"lut": lambda f: f["area"]},
                           transform_fn=lambda p: {"area": int(p["width"]) * 2})
    assert m.predict(_comp(16)) == {"lut": 32}


def test_prior_predicts_only_the_counters_it_claims():
    """A prior for DSP says nothing about LUT rather than implying zero."""
    m = PriorResourceModel(formulas={"dsp": lambda f: 8})
    assert m.predict(_comp(16)) == {"dsp": 8}


def test_prior_has_nothing_to_fit():
    assert PriorResourceModel(formulas={}).has_free_params is False


# ---------------------------------------------------------------------------
# The fir_block prior, wired as a model
# ---------------------------------------------------------------------------

def test_fir_compute_derives_dsp_and_bram_before_any_fit():
    """The declared structure alone prices the countable counters — no corpus involved.

    ``bram: 0`` is an assertion rather than an omission: the taps and delay line are partitioned
    into registers, so declaring no ``MemArray`` is what claims they cost no block RAM.
    """
    from examples.fir_block.fir_block import FirCompute
    from examples.fir_block.fir_block_resource import fir_compute_model

    comp = elaborate(FirCompute, {"mem_dwidth": 32, "ntap": 32, "samp_w": 16,
                                  "samp_i": 2, "unroll_lane": False}, name="fir_compute")
    out = fir_compute_model().predict(comp)    # unfitted: only the derived half answers
    assert out["dsp"] == 32                    # one DSP per tap at samp_w=16
    assert out["bram"] == 0                    # partitioned taps -> registers, not block RAM
    assert "lut" not in out                    # nothing fitted, so nothing claimed
