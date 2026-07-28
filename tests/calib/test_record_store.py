"""The A2 gate: records round-trip, and a record that describes different hardware is refused.

The load-bearing test here is not the round-trip — it is
:func:`test_changed_module_rejects_its_own_stale_records`.  A stale cache entry that reports success is
the failure mode this tree has already been bitten by (a stale ``rtl_fir_block.f`` beside a cached
``xsimk.dll`` makes an XSI run go green while proving nothing).  A store shared across designs *and*
parameter points turns that from occasional into constant, so verification-on-read is a safety
property, not an optimization (``plans/resource_model.md``).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from waveflow.build.elaborate import elaborate
from waveflow.calib.module_key import identify, walk_modules
from waveflow.calib.record_store import (
    FIDELITY,
    KeyCollisionError,
    ModuleStore,
    Provenance,
    Record,
    StaleRecordError,
    merge_payloads,
    normalize_resources,
    resource_record,
    with_cost,
)
from waveflow.hw.hw_module import HwModule, HwParam


@dataclass(kw_only=True)
class Widget(HwModule):
    width: HwParam[int] = 16

    def __post_init__(self) -> None:
        super().__post_init__()
        self.bits = int(self.width) * 4


@pytest.fixture()
def store(tmp_path):
    return ModuleStore(tmp_path / "platform")


def _rec(ident, *, source="hls_estimate", lut=100, dsp=4, cost=12.0):
    return resource_record(ident, {"LUT": lut, "FF": 50, "DSP": dsp, "BRAM_18K": 2},
                           source=source, part="xc7z020clg484-1", period_ns=10.0,
                           tool="vitis_hls 2025.1", cost_seconds=cost)


# ---------------------------------------------------------------------------
# Round-trip and layout
# ---------------------------------------------------------------------------

def test_record_round_trips(store):
    ident = identify(Widget, {"width": 16})
    rec = _rec(ident)
    store.append(rec, identity=ident)

    got = store.read(ident.key, "resource", identity=ident)
    assert len(got) == 1
    assert got[0].payload["lut"] == 100
    assert got[0].payload["dsp"] == 4
    assert got[0].source == "hls_estimate"
    assert got[0].cost_seconds == 12.0
    assert got[0].provenance.part == "xc7z020clg484-1"


def test_records_append_rather_than_overwrite(store):
    ident = identify(Widget, {"width": 16})
    store.append(_rec(ident, lut=100), identity=ident)
    store.append(_rec(ident, lut=101), identity=ident)
    assert [r.payload["lut"] for r in store.read(ident.key, "resource")] == [100, 101]


def test_identity_is_written_beside_the_records(store):
    ident = identify(Widget, {"width": 24})
    store.append(_rec(ident), identity=ident)
    stored = store.get_identity(ident.key)
    assert stored == ident
    assert store.keys() == [ident.key]


def test_layout_sits_under_modules(store):
    ident = identify(Widget, {"width": 16})
    store.append(_rec(ident), identity=ident)
    rel = store.records_path(ident.key, "resource").relative_to(store.platform_dir)
    assert rel.as_posix() == f"modules/{ident.key}/resource/records.jsonl"


# ---------------------------------------------------------------------------
# The guard: a record that describes different hardware is refused
# ---------------------------------------------------------------------------

def test_changed_module_rejects_its_own_stale_records(store):
    """Records written for one configuration must not be served for another under the same key.

    Simulated by keeping the key and moving the signature — which is exactly what a module edited
    between runs would do if the short key happened to survive.
    """
    ident = identify(Widget, {"width": 16})
    store.append(_rec(ident), identity=ident)

    moved = replace(ident, signature="0" * 64)
    with pytest.raises(StaleRecordError, match="different hardware"):
        store.read(ident.key, "resource", identity=moved)


def test_verification_can_be_disabled_for_inspection(store):
    ident = identify(Widget, {"width": 16})
    store.append(_rec(ident), identity=ident)
    moved = replace(ident, signature="0" * 64)
    assert len(store.read(ident.key, "resource", identity=moved, verify=False)) == 1


def test_reading_without_an_identity_does_not_pretend_to_verify(store):
    """No identity means no claim — the read succeeds but has proved nothing, and that is explicit."""
    ident = identify(Widget, {"width": 16})
    store.append(_rec(ident), identity=ident)
    assert len(store.read(ident.key, "resource")) == 1


def test_key_collision_is_detected_not_merged(store):
    """Two different modules under one key must raise rather than pool their measurements."""
    ident = identify(Widget, {"width": 16})
    store.put_identity(ident)
    impostor = replace(ident, cls_name="Other", signature="f" * 64)
    with pytest.raises(KeyCollisionError, match="share a key prefix"):
        store.put_identity(impostor)


def test_appending_with_a_mismatched_identity_raises(store):
    ident = identify(Widget, {"width": 16})
    other = identify(Widget, {"width": 32})
    with pytest.raises(ValueError, match="!= record key"):
        store.append(_rec(ident), identity=other)


def test_missing_records_read_empty(store):
    assert store.read("nope-00000000", "resource") == []
    assert store.best("nope-00000000", "resource") is None
    assert store.keys() == []


# ---------------------------------------------------------------------------
# Fidelity
# ---------------------------------------------------------------------------

def test_best_prefers_the_stronger_source(store):
    ident = identify(Widget, {"width": 16})
    store.append(_rec(ident, source="hls_estimate", lut=100), identity=ident)
    store.append(_rec(ident, source="vivado_impl", lut=180), identity=ident)
    store.append(_rec(ident, source="vivado_synth", lut=170), identity=ident)
    best = store.best(ident.key, "resource", identity=ident)
    assert best.source == "vivado_impl" and best.payload["lut"] == 180


def test_best_breaks_ties_toward_the_newer_record(store):
    ident = identify(Widget, {"width": 16})
    store.append(_rec(ident, source="hls_estimate", lut=100), identity=ident)
    store.append(_rec(ident, source="hls_estimate", lut=110), identity=ident)
    assert store.best(ident.key, "resource", identity=ident).payload["lut"] == 110


def test_fidelity_ranks_within_each_axis():
    assert FIDELITY["vivado_impl"] > FIDELITY["vivado_synth"] > FIDELITY["hls_estimate"]
    assert FIDELITY["xsi"] > FIDELITY["cosim"] > FIDELITY["pysim"]


def test_unknown_resource_source_is_rejected():
    ident = identify(Widget, {"width": 16})
    with pytest.raises(ValueError, match="unknown resource source"):
        resource_record(ident, {"LUT": 1}, source="vibes")


def test_unknown_source_has_no_fidelity():
    assert Record(key="k", target="resource", source="vibes").fidelity == -1


# ---------------------------------------------------------------------------
# Normalization — the `~0` trap
# ---------------------------------------------------------------------------

def test_tilde_zero_normalizes_to_zero():
    """Vitis writes ``~0`` for negligible-but-nonzero; left as a string it breaks every sum."""
    out = normalize_resources({"LUT": 120, "DSP": "~0", "BRAM_18K": "0", "URAM": ""})
    assert out == {"lut": 120, "dsp": 0, "bram": 0, "uram": 0}


def test_counter_names_are_canonicalized():
    """``BRAM_18K`` is a Vitis spelling; a record must not depend on which tool produced it."""
    assert normalize_resources({"BRAM_18K": 4})["bram"] == 4
    assert "bram_18k" not in normalize_resources({"BRAM_18K": 4})


def test_aliases_that_collapse_are_summed_not_overwritten():
    """18K and 36K blocks both canonicalize to ``bram``; the last one must not win silently."""
    assert normalize_resources({"BRAM_18K": 4, "BRAM_36K": 3})["bram"] == 7


def test_device_context_columns_are_dropped():
    """``AVAIL_LUT`` summed across modules looks like a resource count and is nonsense."""
    out = normalize_resources({"LUT": 120, "AVAIL_LUT": 53200, "UTIL_LUT": "~0"})
    assert out == {"lut": 120}


def test_normalization_drops_non_numeric_annotations():
    assert normalize_resources({"LUT": 10, "Note": "estimated"}) == {"lut": 10}


def test_normalized_counters_survive_into_the_record():
    ident = identify(Widget, {"width": 16})
    rec = resource_record(ident, {"LUT": 10, "DSP": "~0"}, source="hls_estimate")
    assert rec.payload == {"lut": 10, "dsp": 0}


# ---------------------------------------------------------------------------
# Aggregation and accounting
# ---------------------------------------------------------------------------

def test_merge_payloads_sums_the_known_counters(store):
    a = Record(key="a", target="resource", source="hls_estimate",
               payload={"lut": 100, "dsp": 4, "note": "x"})
    b = Record(key="b", target="resource", source="hls_estimate", payload={"lut": 50, "ff": 20})
    assert merge_payloads([a, b]) == {"lut": 150, "dsp": 4, "ff": 20}


def test_total_cost_is_auditable_from_the_store(store):
    ident = identify(Widget, {"width": 16})
    store.append(_rec(ident, cost=12.0), identity=ident)
    store.append(_rec(ident, cost=30.0, source="vivado_synth"), identity=ident)
    assert store.total_cost_seconds("resource") == pytest.approx(42.0)


def test_coverage_inventories_what_has_been_measured(store):
    a = identify(Widget, {"width": 16})
    b = identify(Widget, {"width": 32})
    store.append(_rec(a), identity=a)
    store.append(_rec(a, source="vivado_synth"), identity=a)
    store.append(_rec(b), identity=b)
    cov = store.coverage("resource")
    assert cov[a.key] == {"hls_estimate": 1, "vivado_synth": 1}
    assert cov[b.key] == {"hls_estimate": 1}


def test_with_cost_sets_the_field():
    rec = Record(key="k", target="resource", source="hls_estimate")
    assert with_cost(rec, 9.5).cost_seconds == 9.5


def test_timing_records_share_the_envelope(store):
    """One shape for both axes — a timing payload differs only in its contents."""
    ident = identify(Widget, {"width": 16})
    rec = Record(key=ident.key, target="timing", source="cosim",
                 payload={"features": {"nwords": 128}, "cycles": 2835},
                 provenance=Provenance(signature=ident.signature), cost_seconds=95.0)
    store.append(rec, identity=ident)
    got = store.read(ident.key, "timing", identity=ident)[0]
    assert got.payload["cycles"] == 2835
    assert store.total_cost_seconds() == pytest.approx(95.0)


# ---------------------------------------------------------------------------
# The pilot, end to end
# ---------------------------------------------------------------------------

def test_fir_block_walk_files_one_record_per_module(store):
    """The shape Phase B will use: walk the composite, file a record per keyed module."""
    from examples.fir_block.fir_block import MEM_DW, FirBlock

    top = elaborate(FirBlock, {"mem_dwidth": MEM_DW, "ntap": 32, "samp_w": 16,
                               "samp_i": 2, "unroll_lane": False}, name="fir_block")
    walked = walk_modules(top)
    for _, _, ident in walked:
        store.append(_rec(ident, lut=64), identity=ident)

    assert len(store.keys()) == len(walked)
    for _, _, ident in walked:
        assert store.best(ident.key, "resource", identity=ident).payload["lut"] == 64
    assert merge_payloads(
        store.best(i.key, "resource", identity=i) for _, _, i in walked
    )["lut"] == 64 * len(walked)


def test_mem_streams_are_shared_across_a_width_sweep(store):
    """The cache-reuse claim, stored: sweeping ``samp_w`` must not re-key the memory modules."""
    from examples.fir_block.fir_block import MEM_DW, FirBlock

    seen: dict[str, set[str]] = {}
    for samp_w in (8, 16):
        top = elaborate(FirBlock, {"mem_dwidth": MEM_DW, "ntap": 32, "samp_w": samp_w,
                                   "samp_i": 2, "unroll_lane": False}, name="fir_block")
        for _, _, ident in walk_modules(top):
            seen.setdefault(ident.cls_name, set()).add(ident.key)

    assert len(seen["MemRStream"]) == 1        # one synthesis serves both widths
    assert len(seen["MemWStream"]) == 1
    assert len(seen["FirCompute"]) == 2        # the compute genuinely differs
