"""Vitis kernels in the system flow (``plans/system_resources.md`` Stage 2).

* a top with no kernel task -- a bus writer, a host-activated kernel -- is attributed: its one body
  row is the module, the unitemized adapters stay integration;
* a composite whose boundary carries a credit endpoint's halves walks (the wrapper is exempt);
* the common :class:`~waveflow.build.resource_steps.InspectSynthStep` keeps the II gate the regmap and
  stream_inband examples had in local copies;
* ``kernel_resources`` files ``hls_estimate`` records for markov's four tops, each with its owner.

The synthetic reports carry the numbers measured from markov's csynth (Vitis 2025.1); the end-to-end
test reads the real reports and skips when they are absent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from waveflow.build.build import BuildConfig
from waveflow.build.resource_steps import PipelineIIError, loop_table, synth_seconds_of
from waveflow.calib.module_key import walk_modules
from waveflow.calib.synth_report import attribute_resources


def _row(lut, ff, bram=0):
    return {"LUT": lut, "FF": ff, "DSP": 0, "BRAM_18K": bram, "URAM": 0}


class FakeParser:
    def __init__(self, total, modules):
        self._t, self._m = total, modules

    def get_total_resources(self):
        self.total_resources = dict(self._t)
        self.available_resources = {}

    def get_module_resources(self):
        self.module_info = {k: dict(v) for k, v in self._m.items()}


def _markov():
    from examples.markov.markov_build import system
    return system()


def test_a_bus_writer_is_its_body_row_and_the_adapter_is_integration():
    sysm = _markov()
    w = sysm.u_link.fwd_writer
    rows = {"mm_queue_writer_task_64_128_Pipeline_GATHER": _row(109, 34),
            "mm_queue_writer_task_64_128_Pipeline_BURST": _row(244, 133),
            "mm_queue_writer_task_64_128_s": _row(680, 278, bram=2),
            "mm_queue_writer_64_128": _row(1531, 943, bram=2)}
    r = attribute_resources(w, FakeParser(_row(1531, 943, bram=2), rows),
                            top_name="mm_queue_writer_64_128")
    (m,) = r.modules
    assert m.rtl_module == "mm_queue_writer_task_64_128_s" and m.cls_name == "MmStreamWriter"
    assert set(m.subblocks) == {"mm_queue_writer_task_64_128_Pipeline_GATHER",
                                "mm_queue_writer_task_64_128_Pipeline_BURST"}
    assert r.integration["lut"] == 1531 - 680 and r.integration["bram"] == 0
    assert not r.unclaimed


def test_a_host_activated_top_with_one_row_claims_it():
    """simp_fun: the report itemizes nothing but the top; the module is the top row, adapters inside."""
    from waveflow.build.elaborate import elaborate
    from examples.regmap.simp_fun import SimpFun

    top = elaborate(SimpFun, {}, name="simp_fun")
    r = attribute_resources(top, FakeParser(_row(480, 485), {"simp_fun": _row(480, 485)}),
                            top_name="simp_fun")
    (m,) = r.modules
    assert m.rtl_module == "simp_fun" and m.resources["lut"] == 480
    assert r.integration["lut"] == 0


def test_a_stream_top_synthesized_alone_attributes():
    """poly: a host-activated top elaborated alone has unbound stream ports -- they face outside the
    design, so they are its boundary, and the walk must not refuse them."""
    from waveflow.build.elaborate import elaborate
    from examples.stream_inband.poly import PolyAccel

    top = elaborate(PolyAccel, {"in_bw": 32, "out_bw": 32}, name="poly")
    rows = {"poly": _row(900, 700), "poly_Pipeline_BODY": _row(300, 200)}
    r = attribute_resources(top, FakeParser(_row(900, 700), rows), top_name="poly")
    (m,) = r.modules
    assert m.rtl_module == "poly" and set(m.subblocks) == {"poly_Pipeline_BODY"}


def test_a_composite_with_a_routed_credit_endpoint_walks():
    """chain_core's ``s_u`` is bound to an MmCreditStreamIF (no depth); its halves are the boundary."""
    sysm = _markov()
    paths = [p for p, _c, _i in walk_modules(sysm.chain)]
    assert paths == ["chain", "chain.chain_core", "chain.chain_w"]


def test_the_ii_gate_fails_a_loop_at_ii_two(monkeypatch, tmp_path):
    from waveflow.utils import csynthparse

    def fake(self):
        self.loop_df = pd.DataFrame({"Loop": ["L1", "L2"], "PipelineII": [1, 2]})

    monkeypatch.setattr(csynthparse.CsynthParser, "get_loop_pipeline_info", fake)
    monkeypatch.setattr(csynthparse.CsynthParser, "__init__", lambda self, sol_path=None: None)
    assert len(loop_table(tmp_path)) == 2                         # no gate: the table
    with pytest.raises(PipelineIIError, match="L2"):
        loop_table(tmp_path, require_ii1=True)


def test_regmap_and_poly_use_the_framework_step():
    from examples.regmap.simp_fun_build import build_simp_fun_dag
    from examples.stream_inband.poly_build import build_poly_dag
    from waveflow.build.resource_steps import InspectSynthStep

    steps = [s for d in (build_simp_fun_dag(), build_poly_dag()) for s in d.steps()
             if s.name.startswith("inspect_synth")]
    assert len(steps) == 3
    assert all(type(s) is InspectSynthStep and s.require_ii1 for s in steps)


def test_synth_seconds_reads_a_number_or_a_file(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"synth_seconds": 41.5}))
    assert synth_seconds_of(f) == 41.5
    assert synth_seconds_of(3) == 3.0 and synth_seconds_of(tmp_path / "missing.json") == 0.0


def test_markov_kernel_resources_files_four_tops_with_owners(tmp_path):
    from examples.markov.markov_build import HERE, build_dag
    from waveflow.build.system_dag import report_rel
    from waveflow.calib.record_store import ModuleStore

    dag = build_dag(work_dir=tmp_path / "work")
    step = next(s for s in dag.steps() if s.name == "kernel_resources")
    tops = step.rtl.tops
    if not all((HERE / report_rel(t) / "syn" / "report" / "csynth.xml").is_file() for t in tops):
        pytest.skip("no csynth reports for markov -- run markov_build --through csynth")
    cfg = BuildConfig(root_dir=HERE, platform="t", platforms_root=tmp_path / "plats",
                      part="xczu7ev-ffvc1156-2-e", clk_freq=250e6)
    inputs = {a: HERE / report_rel(a.removeprefix("report_")) for a in step.consumes
              if a.startswith("report_")}
    out = step.run(cfg, **inputs)["kernel_resources"]
    index = json.loads(Path(out).read_text(encoding="utf-8"))
    assert set(index) == {"u_gen", "u_chain", "u_u_fwd_wr", "u_u_crd_wr"}
    assert index["u_u_fwd_wr"]["owner"] == "u (MmCreditStreamIF)"
    assert index["u_chain"]["owner"] == "chain (MarkovChain)"
    store = ModuleStore(cfg.platform_info.dir)
    for inst, entry in index.items():
        assert entry["modules"], inst
        for key in entry["modules"]:
            recs = store.read(key, "resource")
            assert recs and all(r.source == "hls_estimate" for r in recs), (inst, key)
            assert recs[-1].payload["owner"] == entry["owner"]
    # A second build with no new csynth files nothing: records are once per synthesis.
    count = {k: len(store.read(k, "resource")) for k in store.keys()}
    step.run(cfg, **inputs)
    assert {k: len(store.read(k, "resource")) for k in store.keys()} == count
