"""tests/build/test_trace_steps.py -- the manifest as a build artifact.

The derivation is covered in test_trace_manifest.py; this is about the step: does it write a
loadable, byte-stable JSON, and does it declare the DAG wiring that makes it regenerate when the
design changes.
"""
from __future__ import annotations

import json

import pytest

from pathlib import Path

from waveflow.build.build import BuildConfig
from waveflow.build.trace_steps import AddVcdTopStep, TraceManifestStep

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def step():
    from examples.mem_copy.mem_copy import MemCopy
    return TraceManifestStep(
        name="trace_manifest",
        comp_class=MemCopy,
        source_artifact="mem_copy_py",
        output_path="results/mem_copy_trace.json",
    )


class TestDagWiring:
    def test_consumes_the_design_source(self, step):
        """The dependency that makes the DAG regenerate when the graph changes."""
        assert step.consumes == ["mem_copy_py"]

    def test_produces_the_manifest_artifact(self, step):
        assert set(step.produces) == {"trace_manifest"}
        assert step.produces["trace_manifest"].name == "mem_copy_trace.json"

    def test_needs_no_rtl_input(self, step):
        """The whole premise: these names are known before anything is synthesized."""
        assert not any("rtl" in c or "report" in c or "solution" in c for c in step.consumes)


class TestOutput(object):
    def test_writes_a_loadable_manifest(self, step, tmp_path):
        out = step.run(BuildConfig(root_dir=str(tmp_path)))
        path = out["trace_manifest"]
        assert path.exists()

        man = json.loads(path.read_text(encoding="utf-8"))
        assert man["top"] == "mem_copy"
        assert {c["id"] for c in man["channels"]} == {"cmd", "copy_data"}
        assert len(man["tasks"]) == 3

    def test_is_byte_stable_across_runs(self, step, tmp_path):
        """An artifact that churns would make the DAG think the design changed every build."""
        first = step.run(BuildConfig(root_dir=str(tmp_path)))["trace_manifest"].read_bytes()
        second = step.run(BuildConfig(root_dir=str(tmp_path)))["trace_manifest"].read_bytes()
        assert first == second

    def test_creates_missing_parent_directories(self, step, tmp_path):
        step.output_path = "deep/nested/dir/trace.json"
        out = step.run(BuildConfig(root_dir=str(tmp_path)))
        assert out["trace_manifest"].exists()

    def test_written_json_matches_the_spec_exactly(self, step, tmp_path):
        """Serialization must not lose or reshape anything -- the step is a writer, not a filter.

        (Loading an on-disk manifest is covered by tests/utils/test_trace.py; this pins that what
        lands on disk IS the derived manifest.)"""
        from waveflow.build.composite_gen import composite_top_spec
        from waveflow.build.elaborate import elaborate

        path = step.run(BuildConfig(root_dir=str(tmp_path)))["trace_manifest"]
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        derived = composite_top_spec(elaborate(step.comp_class), width=64).trace_manifest()
        assert on_disk == derived


@pytest.fixture
def dumper_step():
    from examples.mem_copy.mem_copy import MemCopy
    return AddVcdTopStep(name="vcd_dumper", comp_class=MemCopy,
                         source_artifact="mem_copy_py", output_dir="xsi")


class TestAddVcdTop:
    def test_names_the_module_and_file_after_the_top(self, dumper_step, tmp_path):
        """One xsi/ directory can serve several tops, and a dumper naming a scope that is not part
        of *this* elaboration is a hard error -- so the name cannot be generic."""
        out = dumper_step.run(BuildConfig(root_dir=str(tmp_path)))["vcd_dumper"]
        assert out.name == "vcd_dumper_mem_copy.v"
        assert "module vcd_dumper_mem_copy;" in out.read_text(encoding="utf-8")

    def test_dumps_level_1_of_the_top(self, dumper_step, tmp_path):
        """Level 1 is this scope only.  Level 0 would dump the whole subtree for no extra reach --
        the inter-task channel wires are already lifted into the top scope."""
        text = dumper_step.run(BuildConfig(root_dir=str(tmp_path)))["vcd_dumper"].read_text()
        assert "$dumpvars(1, mem_copy);" in text
        assert '$dumpfile("mem_copy_trace.vcd");' in text

    def test_produces_declares_the_same_path_it_writes(self, dumper_step, tmp_path):
        declared = dumper_step.produces["vcd_dumper"]
        written = dumper_step.run(BuildConfig(root_dir=str(tmp_path)))["vcd_dumper"]
        assert written == Path(tmp_path) / declared

    def test_consumes_the_design_source_not_rtl(self, dumper_step):
        assert dumper_step.consumes == ["mem_copy_py"]

    @pytest.mark.parametrize("comp_path,xsi_dir,top", [
        ("examples.mem_copy.mem_copy:MemCopy",
         "examples/mem_copy/xsi", "mem_copy"),
        ("examples.interleaver.interleaver:InterleaverCanon",
         "examples/interleaver/xsi", "interleaver_canon"),
    ])
    def test_committed_dumper_is_what_the_step_generates(self, comp_path, xsi_dir, top, tmp_path):
        """The checked-in .v must be regenerable, not a parallel hand-written copy that drifts.

        These two files are the ones the XSI gates actually elaborate, so if the generator and the
        committed artifact disagree, one of them is untested."""
        import importlib

        mod_name, cls_name = comp_path.split(":")
        comp_class = getattr(importlib.import_module(mod_name), cls_name)
        step = AddVcdTopStep(name="d", comp_class=comp_class, source_artifact="src",
                             output_dir="out")
        generated = step.run(BuildConfig(root_dir=str(tmp_path)))["vcd_dumper"].read_text(
            encoding="utf-8")
        committed = (REPO / xsi_dir / f"vcd_dumper_{top}.v").read_text(encoding="utf-8")
        assert generated == committed
