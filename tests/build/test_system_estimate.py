"""A system's estimate, row by row, and the out-of-context leaf library (``plans/system_resources.md``
Stages 5 and 6) -- toolchain-free: the records are filed by hand, the synthesis is faked.

* every one of markov's fifteen instances appears, under its owner, plus the integration row; with no
  data each is ``UNCALIBRATED`` and says what measuring it would cost -- never a silent zero;
* a kernel is priced from its ``hls_estimate`` records, a leaf in-context first and out-of-context
  second; ``use_in_context=False`` gives the purely composed prediction;
* the measured total sits beside the prediction with the gap;
* ``leaf_resources``' batch measures each missing configuration once and files it; a re-run measures
  nothing.
"""
from __future__ import annotations

import json

import pytest

from waveflow.build.system_estimate import estimate_system, main
from waveflow.calib.confidence import ConfidenceLevel
from waveflow.calib.platform import Platform
from waveflow.calib.record_store import resource_record


def _markov():
    from examples.markov.markov_build import HERE, build_dag
    dag = build_dag()
    rtl = next(s for s in dag.steps() if s.name == "system_rtl")
    return rtl.sysm, rtl.spec, HERE


def _platform(tmp_path):
    return Platform.resolve(tmp_path / "plats", "t", part="xc7z020clg484-1", clk_freq=100e6)


def test_every_instance_is_a_row_part_and_none_is_a_silent_zero(tmp_path):
    sysm, spec, _here = _markov()
    est = estimate_system(sysm, _platform(tmp_path), spec=spec)
    parts = [p for r in est.rows for p in r.parts if p.kind != "system"]
    assert sorted(p.inst for p in parts) == sorted(i.inst for i in spec.instances())
    assert len(parts) == 15
    assert est.rows[-1].owner == "system integration"
    assert {r.owner for r in est.rows} >= {"gen (MarkovGen)", "chain (MarkovChain)",
                                          "u (MmCreditStreamIF)", "xbar (AXIMMCrossBarIF)",
                                          "gen_mm (MemSlaveAdaptor)", "mem (MemoryMod)"}
    assert all(p.level == ConfidenceLevel.UNCALIBRATED for r in est.rows for p in r.parts)
    w = est.weakest()
    assert len(w) == 16 and all(x["cost_seconds"] > 0 and x["measure_with"] for x in w)
    blob = est.to_json()
    assert blob["level"] == "UNCALIBRATED" and json.dumps(blob)


def _file_kernels(spec, store):
    """hls_estimate records for every kernel module and kernel top (the csynth attribution)."""
    from waveflow.calib.module_key import walk_modules
    from waveflow.calib.record_store import integration_record

    for k in spec.kernels:
        for _p, comp, ident in walk_modules(k.hw):
            if not getattr(comp, "sub_comps", None):
                store.append(resource_record(ident, {"lut": 100, "ff": 50}, source="hls_estimate"),
                             identity=ident)
        top = next(i for _p, c, i in walk_modules(k.hw) if c is k.hw)
        store.append(integration_record(top, {"lut": 10, "ff": 5}, source="hls_estimate"),
                     identity=top)


def test_kernels_from_records_leaves_in_context_first(tmp_path):
    from waveflow.build.system_dag import rtl_rel
    from waveflow.build.system_synth import INSTANCE_TARGET, system_identity
    from waveflow.build.system_top import render_system_top
    from waveflow.calib.record_store import Record

    sysm, spec, here = _markov()
    kdirs = {m: here / rtl_rel(m) for m in spec.modules}
    if not all(d.is_dir() for d in kdirs.values()):
        pytest.skip("no csynth RTL for markov -- in-context records are keyed by the system identity")
    sys_key = system_identity(spec, render_system_top(spec), kdirs, part="", period_ns=0.0,
                              impl=False)[0].key
    plat = _platform(tmp_path)
    store = plat.module_store()
    _file_kernels(spec, store)

    def in_context(i, counters, system=sys_key):
        r = resource_record(i.identity, counters, source="vivado_synth",
                            extra={"context": spec.top, "system": system, "inst": i.inst})
        store.append(Record(**{**r.__dict__, "target": INSTANCE_TARGET}), identity=i.identity)

    qu = next(i for i in spec.instances() if i.inst == "u_chain_qu")
    store.append(resource_record(qu.identity, {"lut": 300, "ff": 200}, source="vivado_synth",
                                 extra={"context": "ooc"}), identity=qu.identity)
    in_context(qu, {"lut": 280, "ff": 190})
    # A synthesis of another version of this system (same top name) describes another design.
    resp = next(i for i in spec.instances() if i.inst == "u_chain_qresp")
    in_context(resp, {"lut": 9999}, system="markov_top-00000000")
    # Two instances of one configuration share a key; in context each keeps its own number.
    for inst, lut in (("u_gen_mm_front", 79), ("u_chain_mm_front", 69)):
        in_context(next(x for x in spec.instances() if x.inst == inst), {"lut": lut})

    est = estimate_system(sysm, plat, spec=spec, root=here)
    parts = {p.inst: p for r in est.rows for p in r.parts}
    assert parts["u_chain_qresp"].level == ConfidenceLevel.UNCALIBRATED      # the stale run ignored
    assert (parts["u_gen_mm_front"].counters["lut"], parts["u_chain_mm_front"].counters["lut"]) == \
        (79, 69)
    assert parts["u_chain"].level == ConfidenceLevel.EXACT
    assert parts["u_chain"].counters["lut"] == 2 * 100 + 10          # chain_core + chain_w + own
    assert parts["u_u_fwd_wr"].counters["lut"] == 110 and parts["u_u_fwd_wr"].source == "hls_estimate"
    assert (parts["u_chain_qu"].counters["lut"], parts["u_chain_qu"].context) == (280, "in markov_top")
    composed = estimate_system(sysm, plat, spec=spec, root=here, use_in_context=False)
    q = next(p for r in composed.rows for p in r.parts if p.inst == "u_chain_qu")
    assert (q.counters["lut"], q.context) == (300, "ooc")
    # The adaptor row holds its front and its views.
    chain_mm = next(r for r in est.rows if r.owner == "chain_mm (MemSlaveAdaptor)")
    assert {p.inst for p in chain_mm.parts} == {"u_chain_mm_front", "u_chain_qu", "u_chain_qresp"}


def test_the_measured_total_sits_beside_the_prediction(tmp_path):
    from waveflow.build.system_dag import rtl_rel
    from waveflow.build.system_synth import SystemSynth, system_synth_records
    from waveflow.build.system_top import render_system_top

    sysm, spec, here = _markov()
    kdirs = {m: here / rtl_rel(m) for m in spec.modules}
    if not all(d.is_dir() for d in kdirs.values()):
        pytest.skip("no csynth RTL for markov -- the system identity covers it")
    plat = _platform(tmp_path)
    inv = spec.instances()
    res = SystemSynth(top=spec.top, digest="x", part="xc7z020clg484-1", period_ns=10.0, impl=False,
                      total={"lut": 12000, "ff": 9000},
                      instances={i.inst: {"lut": 700, "ff": 500} for i in inv}, seconds=300.0)
    store = plat.module_store()
    for rec, ident in system_synth_records(spec, res, render_system_top(spec), kdirs):
        store.append(rec, identity=ident)
    est = estimate_system(sysm, plat, spec=spec, root=here)
    assert est.measured == {"lut": 12000, "ff": 9000} and est.measured_source == "vivado_synth"
    # Every part in-context, plus the integration term: the prediction IS the measurement here.
    assert est.total["lut"] == 12000 and est.gap["lut"] == 0.0
    integ = est.rows[-1].parts[0]
    assert integ.level == ConfidenceLevel.EXACT and integ.counters["lut"] == 12000 - 15 * 700
    assert "measured total" in est.format_table()


def test_the_cli_prints_json(tmp_path, capsys):
    _platform(tmp_path)
    assert main(["examples.markov.markov_build:system", "--platform", "t", "--platforms-root",
                 str(tmp_path / "plats"), "--top", "markov_top", "--json"]) == 0
    blob = json.loads(capsys.readouterr().out)
    assert blob["top"] == "markov_top" and len(blob["rows"]) >= 8


# ---------------------------------------------------------------------------------------------
# The out-of-context library (Stage 6)
# ---------------------------------------------------------------------------------------------

def test_leaf_clock_ports_and_generics():
    from waveflow.build.mm_adaptor_gen import RTL_DIR
    from waveflow.build.rtl_ooc import clock_port, ooc_tcl

    from waveflow.build.mm_adaptor_gen import LEAF_DEPS

    clocks = {m: clock_port(RTL_DIR / f"{m}.v", m) for m in LEAF_DEPS}
    assert clocks == {"mm_sync_fifo": "clk", "axi_slave_front": "ap_clk", "mm_queue_in": "ap_clk",
                      "mm_queue_out": "ap_clk", "mm_regbank": "ap_clk", "mm_bram_port": "ap_clk",
                      "bram_t2p": "clk", "mm_credit_in": "ap_clk"}
    tcl = ooc_tcl("mm_queue_in", {"DW": 64, "DEPTH": 128}, ["a.v"], part="p")
    assert "-generic DW=64 -generic DEPTH=128" in tcl and "-mode out_of_context" in tcl


def test_measure_instances_measures_each_configuration_once(tmp_path, monkeypatch):
    from waveflow.build import rtl_ooc
    from waveflow.build.rtl_ooc import OocSynth, measure_instances

    _sysm, spec, _here = _markov()
    calls = []

    def fake(module, params, sources, *, part, period_ns, out_dir, identity=None, **_):
        calls.append(identity.key)
        return OocSynth(module=module, key=identity.key, params=params, part=part,
                        period_ns=period_ns, counters={"lut": 42, "ff": 7}, seconds=60.0)

    monkeypatch.setattr(rtl_ooc, "synth_rtl_ooc", fake)
    store = _platform(tmp_path).module_store()
    first = measure_instances(spec, store, part="p", period_ns=10.0, out_dir=tmp_path / "ooc")
    assert {e["status"] for e in first.values()} == {"missing"} and not calls
    # Measure the leaves (the crossbar's flow is the census's; leave it out of this fake).
    leaves = [i for i in spec.instances() if i.kind == "leaf"]
    monkeypatch.setattr(type(spec), "instances", lambda self: tuple(leaves))
    got = measure_instances(spec, store, part="p", period_ns=10.0, out_dir=tmp_path / "ooc",
                            measure=True)
    assert {e["status"] for e in got.values()} == {"measured"}
    assert len(calls) == len({i.identity.key for i in leaves}) < len(leaves)   # shared fronts
    rec = store.best(leaves[0].identity.key, "resource")
    assert rec.payload["lut"] == 42 and rec.payload["context"] == "ooc"
    again = measure_instances(spec, store, part="p", period_ns=10.0, out_dir=tmp_path / "ooc",
                              measure=True)
    assert {e["status"] for e in again.values()} == {"stored"}
    assert len(calls) == len({i.identity.key for i in leaves})                 # nothing re-run
