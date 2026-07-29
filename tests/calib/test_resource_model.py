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

    assert m.predict_own(comp)["lut"] == 833
    conf = m.confidence_own(comp)
    assert conf.level is ConfidenceLevel.EXACT
    assert "measured directly" in conf.summary


def test_lookup_refuses_to_interpolate():
    """A key it has not seen is a gap, not an estimate — and it says so."""
    m = LookupResourceModel(table={identify(Blk, {"width": 16}).key: {"lut": 833}})
    conf = m.confidence_own(_comp(32))
    assert conf.level is ConfidenceLevel.UNCALIBRATED
    assert "cannot interpolate" in conf.summary
    assert m.predict_own(_comp(32)) == zero_counters()


def test_lookup_reads_a_module_store(tmp_path):
    store = ModuleStore(tmp_path / "platform")
    ident = identify(Blk, {"width": 16})
    store.append(resource_record(ident, {"LUT": 1850, "FF": 1464, "DSP": 0, "BRAM_18K": 0},
                                 source="hls_estimate"), identity=ident)

    m = LookupResourceModel(store=store)
    assert m.predict_own(_comp(16))["lut"] == 1850
    assert m.confidence_own(_comp(16)).level is ConfidenceLevel.EXACT


def test_lookup_prefers_the_stronger_source(tmp_path):
    store = ModuleStore(tmp_path / "platform")
    ident = identify(Blk, {"width": 16})
    for src, lut in (("hls_estimate", 100), ("vivado_impl", 180)):
        store.append(resource_record(ident, {"LUT": lut}, source=src), identity=ident)

    m = LookupResourceModel(store=store)
    assert m.predict_own(_comp(16))["lut"] == 180
    assert m.confidence_own(_comp(16)).facts["measured_source"] == "vivado_impl"


def test_lookup_has_nothing_to_fit():
    assert LookupResourceModel().has_free_params is False


# ---------------------------------------------------------------------------
# Prior
# ---------------------------------------------------------------------------

def test_prior_computes_from_features_and_claims_exact():
    m = PriorResourceModel(formulas={"dsp": lambda f: f["width"] // 4})
    assert m.predict_own(_comp(16)) == {"dsp": 4}
    conf = m.confidence_own(_comp(16))
    assert conf.level is ConfidenceLevel.EXACT
    assert "no fitted parameters" in conf.summary


def test_prior_reads_resolved_params_by_default():
    """Features come off the elaborated instance — nothing is threaded down from a parent."""
    m = PriorResourceModel(formulas={})
    assert m.features(_comp(24))["width"] == 24


def test_prior_accepts_a_transform():
    """A transform may read *structure*, not just parameters — both are param-determined."""
    m = PriorResourceModel(formulas={"lut": lambda f: 10 * f["ports"]},
                           transform=lambda c: {"ports": len(getattr(c, "endpoints", {}) or {})})
    assert m.predict_own(_comp(16)) == {"lut": 0}      # this toy declares no endpoints


def test_prior_predicts_only_the_counters_it_claims():
    """A prior for DSP says nothing about LUT rather than implying zero."""
    m = PriorResourceModel(formulas={"dsp": lambda f: 8})
    assert m.predict_own(_comp(16)) == {"dsp": 8}


def test_prior_has_nothing_to_fit():
    assert PriorResourceModel(formulas={}).has_free_params is False


# ---------------------------------------------------------------------------
# The fir_block prior, wired as a model
# ---------------------------------------------------------------------------

def test_fir_compute_prior_predicts_through_the_model_interface():
    from examples.fir_block.fir_block import FirCompute
    from examples.fir_block.fir_block_resource import fir_compute_prior

    comp = elaborate(FirCompute, {"mem_dwidth": 32, "ntap": 32, "samp_w": 16,
                                  "samp_i": 2, "unroll_lane": False}, name="fir_compute")
    out = fir_compute_prior().predict_own(comp)
    assert out == {"dsp": 32, "bram": 0}       # one DSP per tap, partitioned taps -> no BRAM
