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
from waveflow.build.trace_steps import (
    AddVcdTopStep,
    ExtractBurstsStep,
    RtlSimStep,
    TraceManifestStep,
)

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
    ])
    def test_committed_dumper_is_what_the_step_generates(self, comp_path, xsi_dir, top, tmp_path):
        """The checked-in .v must be regenerable, not a parallel hand-written copy that drifts.

        This is the file the XSI gate actually elaborates, so if the generator and the committed
        artifact disagree, one of them is untested."""
        import importlib

        mod_name, cls_name = comp_path.split(":")
        comp_class = getattr(importlib.import_module(mod_name), cls_name)
        step = AddVcdTopStep(name="d", comp_class=comp_class, source_artifact="src",
                             output_dir="out")
        generated = step.run(BuildConfig(root_dir=str(tmp_path)))["vcd_dumper"].read_text(
            encoding="utf-8")
        committed = (REPO / xsi_dir / f"vcd_dumper_{top}.v").read_text(encoding="utf-8")
        assert generated == committed


class TestRtlSimStepWiring:
    """Structural only -- actually running it needs Vitis RTL plus Vivado xsim."""

    @pytest.fixture
    def step(self):
        return RtlSimStep(name="rtlsim", top="mem_copy", tb="mem_copy_bfm_tb", xsi_dir="xsi")

    def test_declares_the_vcd_it_produces(self, step):
        assert step.produces["trace_vcd"] == Path("xsi/mem_copy_trace.vcd")

    def test_consumes_rtl_and_the_dumper(self, step):
        """It needs synthesized RTL *and* the second top, or run.bat cannot trace."""
        assert set(step.consumes) == {"report_dir", "vcd_dumper"}

    def test_asserts_nothing_about_cycle_counts(self, step):
        """Correctness stays with the -m xsi gate; this step only produces a waveform.  Routing a
        green gate through new code is how a gate quietly stops meaning what it meant."""
        src = Path(RtlSimStep.run.__code__.co_filename).read_text(encoding="utf-8")
        body = src.split("class RtlSimStep")[1].split("class ExtractBurstsStep")[0]
        assert "2908" not in body and "want_cycles" not in body


class TestExtractBurstsWiring:
    @pytest.fixture
    def step(self):
        return ExtractBurstsStep(name="extract_bursts", output_path="results/t.json")

    def test_consumes_manifest_and_vcd(self, step):
        assert set(step.consumes) == {"trace_manifest", "trace_vcd"}

    def test_produces_the_timing_table(self, step):
        assert step.produces["timing_events"] == Path("results/t.json")

    def test_records_the_burst_length_it_assumed(self, step):
        """`num_trans` is measured, not derived -- recording max_burst_len lets a consumer CHECK
        it against ceil(nwords/max_burst_len) instead of assuming."""
        assert step.max_burst_len == 16


@pytest.mark.xsi
class TestExtractedTableAgainstTheRealTrace:
    """The artifact is the calibration input, so these pin its invariants, not just its shape."""

    @pytest.fixture
    def events(self):
        p = REPO / "examples/mem_copy/results/mem_copy_timing.json"
        if not p.exists():
            pytest.skip(f"no extracted timing at {p} -- "
                        f"python examples/mem_copy/mem_copy_build.py --through extract_bursts")
        return json.loads(p.read_text(encoding="utf-8"))

    def test_one_row_per_firing_per_component(self, events):
        from collections import Counter
        per = Counter(f["component"] for f in events["firings"])
        assert set(per.values()) == {16}, f"16 jobs, so 16 firings each: {per}"

    def test_blocked_isolates_the_calibratable_rows(self, events):
        """`blocked == 0` is the "safe to calibrate on" filter, computed rather than assumed.

        Only the reader's FIRST firing is uncontended; every later one absorbs 30 cycles waiting on
        a writer that is still draining.  The writer is never blocked -- it is the bottleneck."""
        r = [f for f in events["firings"] if f["component"] == "mem_r_stream_framed_task"]
        w = [f for f in events["firings"] if f["component"] == "mem_w_stream_framed_done_task"]

        assert r[0]["blocked"] == 0 and r[0]["span"] == 153
        assert {f["blocked"] for f in r[1:]} == {30}
        assert {f["span"] for f in r[1:]} == {183}
        assert {f["blocked"] for f in w} == {0}
        assert {f["span"] for f in w} == {183}

    def test_the_bus_term_is_shared_and_the_fixed_term_is_per_component(self, events):
        """The whole point of the two-level split: subtract the bus occupancy
        (`nwords + 2*(bursts-1)`) and what remains is a CONSTANT per component -- the writer's
        control cost is 41, the reader's 11.  Same bus law, different component constants."""
        fixed = {}
        for f in events["firings"]:
            if f["blocked"] or not f["nwords"]:
                continue                      # contended rows and non-m_axi stages are not samples
            bus = f["nwords"] + 2 * (f["num_trans"] - 1)
            fixed.setdefault(f["component"], set()).add(f["span"] - bus)

        assert fixed["mem_w_stream_framed_done_task"] == {41}
        assert fixed["mem_r_stream_framed_task"] == {11}

    def test_num_trans_matches_the_recorded_burst_length(self, events):
        """128 words at max_burst_len=16 is 8 bursts -- measured off AW/AR, so this cross-checks
        the trace against the assumption rather than restating it."""
        import math
        mb = events["max_burst_len"]
        for f in events["firings"]:
            if f["nwords"]:
                assert f["num_trans"] == math.ceil(f["nwords"] / mb), f
