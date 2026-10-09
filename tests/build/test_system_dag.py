"""``waveflow/build/system_dag.py`` -- the fast half (``plans/system_dag.md``).

The RTL half is the example gates (``tests/examples/test_markov_xsi.py``, ``test_mm_fir_xsi.py``).
Here: csynth's freshness and its two modes, with a stand-in for Vitis; and scenario / pysim /
compare without Vivado -- the pysim traces byte-identical to the ones ``run_system_xsi`` wrote
before the DAG.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.system_dag import (
    CompareStep,
    CsynthStep,
    CsynthTopsStep,
    PysimStep,
    ScenarioStep,
    rtl_problem,
    rtl_rel,
)


# ---------------------------------------------------------------------------------------------------
# csynth (Stage 2), with run_vitis_hls replaced by a stand-in that writes a .v and says OK
# ---------------------------------------------------------------------------------------------------

@pytest.fixture
def fake_vitis(monkeypatch):
    calls: list[str] = []

    class _R:
        stdout, stderr = "WAVEFLOW_CSYNTH_OK\n", ""

    def run_vitis_hls(tcl, work_dir=None, **_):
        top = Path(tcl).stem
        d = Path(work_dir) / rtl_rel(top)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{top}.v").write_text(f"module {top}; endmodule\n", encoding="utf-8")
        calls.append(top)
        return _R()

    monkeypatch.setattr("waveflow.toolchain.toolchain.run_vitis_hls", run_vitis_hls)
    return calls


class _Codegen(BuildStep):
    """Writes ``gen/<top>.cpp`` for each top and one shared header -- the same bytes every run unless
    ``edits`` says otherwise."""

    produces = {"include": Path("include"), "gen": Path("gen")}
    tops = ("a", "b")
    edits: dict = {}

    def is_fresh(self, config, paths):
        return False

    def run(self, config, **_):
        root = Path(config.root_dir)
        (root / "include").mkdir(exist_ok=True)
        (root / "gen").mkdir(exist_ok=True)
        (root / "include" / "common.h").write_text(self.edits.get("common.h", "// common\n"))
        for t in self.tops:
            (root / "gen" / f"{t}.cpp").write_text(self.edits.get(f"{t}.cpp", f"// top {t}\n"))
            (root / "gen" / f"{t}.tcl").write_text("# tcl\n")
        return {"include": root / "include", "gen": root / "gen"}


def _csynth_dag():
    dag = BuildDag()
    cg = _Codegen(name="codegen")
    cg.edits = {}
    dag.add(cg)
    cs = CsynthTopsStep(name="csynth", tops=("a", "b"))
    dag.add(cs)
    return dag, cg, cs


def test_csynth_builds_a_fresh_tree_then_skips_it(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    cfg = BuildConfig(root_dir=tmp_path)
    res = dag.run(cfg)
    assert res["csynth"].success and fake_vitis == ["a", "b"]
    assert all(rtl_problem(tmp_path, t) is None for t in ("a", "b"))
    res = dag.run(cfg)                       # codegen rewrites identical bytes: nothing to synthesize
    assert not res["codegen"].skipped and res["csynth"].skipped
    assert fake_vitis == ["a", "b"]


def test_csynth_reruns_only_the_top_whose_source_changed(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    cfg = BuildConfig(root_dir=tmp_path)
    dag.run(cfg)
    cg.edits = {"b.cpp": "// top b, edited\n"}
    res = dag.run(cfg)
    assert res["csynth"].success and fake_vitis == ["a", "b", "b"]
    assert cs.last_run == {"csynth_a": "fresh", "csynth_b": "ran"}


def test_a_shared_header_change_reruns_every_top(tmp_path, fake_vitis):
    """The stamp hashes all of include/ (the plan's open question, 'narrower stamps')."""
    dag, cg, cs = _csynth_dag()
    cfg = BuildConfig(root_dir=tmp_path)
    dag.run(cfg)
    cg.edits = {"common.h": "// common, edited\n"}
    dag.run(cfg)
    assert fake_vitis == ["a", "b", "a", "b"]


def test_check_mode_fails_on_a_stale_top_and_never_synthesizes(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    dag.run(BuildConfig(root_dir=tmp_path))
    cg.edits = {"a.cpp": "// top a, edited\n"}
    res = dag.run(BuildConfig(root_dir=tmp_path, params={"synth": "check"}))
    assert not res["csynth"].success
    assert "a:" in res["csynth"].message and "gen/a.cpp has changed" in res["csynth"].message
    assert fake_vitis == ["a", "b"]


def test_check_mode_fails_on_a_missing_top(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    res = dag.run(BuildConfig(root_dir=tmp_path, params={"synth": "check"}))
    assert not res["csynth"].success and "no csynth RTL for a" in res["csynth"].message
    assert fake_vitis == []


def test_check_mode_passes_a_fresh_tree(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    dag.run(BuildConfig(root_dir=tmp_path))
    res = dag.run(BuildConfig(root_dir=tmp_path, params={"synth": "check"}))
    assert res["csynth"].success and fake_vitis == ["a", "b"]


def test_a_missing_stamp_falls_back_to_mtime_never_to_clean(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    cfg = BuildConfig(root_dir=tmp_path)
    dag.run(cfg)
    (tmp_path / "a_proj" / "rtl_sources.json").unlink()
    cg.edits = {"a.cpp": "// edited, and newer than the RTL\n"}
    dag.steps()[0].run(cfg)
    t = time.time() + 10
    os.utime(tmp_path / "gen" / "a.cpp", (t, t))
    assert "no source stamp" in rtl_problem(tmp_path, "a")
    assert cs.is_fresh(cfg, {}) is False


def test_status_reads_a_stamped_top_fresh_when_gen_is_newer(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    cfg = BuildConfig(root_dir=tmp_path)
    dag.run(cfg)
    dag.steps()[0].run(cfg)                  # gen/ rewritten, same bytes, newer
    by = {e["artifact"]: e for e in dag.results_status(cfg)}
    assert by["rtl_a"]["stale"] is False and by["rtl_b"]["stale"] is False


def test_bad_synth_mode_is_refused(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    res = dag.run(BuildConfig(root_dir=tmp_path, params={"synth": "maybe"}))
    assert not res["csynth"].success and "synth must be one of" in res["csynth"].message


def test_csynth_step_writes_the_stamp(tmp_path, fake_vitis):
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "t.cpp").write_text("// t\n")
    (tmp_path / "gen" / "t.tcl").write_text("# tcl\n")
    step = CsynthStep(top="t")
    assert step.name == "csynth_t"
    assert step.is_fresh(BuildConfig(root_dir=tmp_path), {}) is False
    step.run(BuildConfig(root_dir=tmp_path))
    assert (tmp_path / "t_proj" / "rtl_sources.json").is_file()
    assert step.is_fresh(BuildConfig(root_dir=tmp_path), {}) is True


def test_a_failed_csynth_reports_the_vitis_log(tmp_path, monkeypatch):
    import subprocess

    def run_vitis_hls(tcl, work_dir=None, **_):
        raise subprocess.CalledProcessError(1, "vitis-run", output="WAVEFLOW_ERROR: csynth\nboom\n")

    monkeypatch.setattr("waveflow.toolchain.toolchain.run_vitis_hls", run_vitis_hls)
    dag, cg, cs = _csynth_dag()
    res = dag.run(BuildConfig(root_dir=tmp_path))
    assert not res["csynth"].success
    assert "csynth of a failed" in res["csynth"].message and "boom" in res["csynth"].message



# ---------------------------------------------------------------------------------------------------
# scenario / pysim / compare (Stage 3): no Vivado
# ---------------------------------------------------------------------------------------------------

def _markov():
    from examples.markov.markov_build import system
    return system()


def _fir(topology):
    from examples.mm_fir.mm_fir_build import system
    return system(topology)


SYSTEMS = {"markov": _markov, "per_view": lambda: _fir("per_view"),
           "one_front": lambda: _fir("one_front")}


def _legacy_pysim(sysm, ws: Path):
    """What run_system_xsi did before the DAG, minus Vivado: the spec walk, the scenario, the harness
    render, then the pysim run from the same scenario file."""
    from waveflow.build.system_top import render_system_tb, render_system_top, system_tb_spec, \
        system_top_spec
    from waveflow.build.system_xsi import discover

    xbar, host, cut = discover(sysm)
    spec = system_top_spec(xbar, cut, top="t")
    scenario, traces = ws / "scenario", ws / "traces"
    host.scenario, host.trace_dir = scenario.as_posix(), traces.as_posix()
    host.write_scenario(scenario)
    render_system_tb(spec, system_tb_spec(spec, xbar, [host], probes=[]))
    render_system_top(spec, None)
    host.trace_dir = (ws / "pysim_traces").as_posix()
    sysm.run()
    return sysm.sim.env.now / host.clk.period


def _pysim_dag(sysm, work):
    dag = BuildDag()
    dag.add(ScenarioStep(sysm=sysm, work=work))
    dag.add(PysimStep(sysm=sysm, work=work))
    return dag


@pytest.mark.parametrize("which", sorted(SYSTEMS))
def test_through_pysim_matches_the_pre_dag_run_byte_for_byte(tmp_path, which):
    from waveflow.build.system_xsi import compare_traces

    legacy = _legacy_pysim(SYSTEMS[which](), tmp_path / "legacy")
    res = _pysim_dag(SYSTEMS[which](), Path("work")).run(BuildConfig(root_dir=tmp_path),
                                                       through="pysim")
    assert all(r.success for r in res.values()), {n: r.message for n, r in res.items()}
    new = tmp_path / "work"
    assert compare_traces(new / "pysim_traces", tmp_path / "legacy" / "pysim_traces") == []
    for f in ("words.bin", "bounds.bin", "meta.json"):
        assert (new / "scenario" / f).read_bytes() == (tmp_path / "legacy" / "scenario" / f).read_bytes()
    assert json.loads((new / "pysim.json").read_text())["cycles"] == legacy


class _FakeTraces(BuildStep):
    """Stands in for system_xsi: the pysim traces copied, one word appended when ``corrupt``."""
    consumes = ["pysim_traces"]
    produces = {"traces": Path("rtl_traces")}

    def is_fresh(self, config, paths):
        return False

    def run(self, config, pysim_traces, **_):
        import shutil
        src = Path(pysim_traces)
        dst = Path(config.root_dir) / "rtl_traces"
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
        if self.corrupt:
            f = next(dst.iterdir()) / "words.bin"
            f.write_bytes(f.read_bytes() + b"\0" * 8)
        return {"traces": dst}


@pytest.mark.parametrize("corrupt", [False, True])
def test_compare_passes_equal_traces_and_fails_on_a_difference(tmp_path, corrupt):
    sysm = _markov()
    dag = _pysim_dag(sysm, Path("work"))
    fake = _FakeTraces(name="fake")
    fake.corrupt = corrupt
    dag.add(fake)
    dag.add(CompareStep(sysm=sysm, work=Path("work")))
    res = dag.run(BuildConfig(root_dir=tmp_path))
    bad = json.loads((tmp_path / "work" / "compare.json").read_text())["trace_mismatches"]
    assert res["compare"].success is (not corrupt)
    assert (bad != []) is corrupt


# ---------------------------------------------------------------------------------------------------
# add_system_steps, system_xsi, the run_system_xsi wrapper (Stage 4): no Vivado
# ---------------------------------------------------------------------------------------------------

def test_snake_names_a_system_class():
    from waveflow.build.system_dag import snake
    assert snake("MarkovSystem") == "markov_system"
    assert snake("MmFirSystem") == "mm_fir_system"


def test_add_system_steps_derives_the_tops_and_the_defaults():
    from waveflow.build.system_dag import add_system_steps
    dag = BuildDag()
    xsi = add_system_steps(dag, _markov(), work_dir="xsi_work")
    assert dag.step_names()[-1] == "compare"
    assert set(dag.step_names()) == {"include", "gen", "csynth", "scenario", "pysim", "system_xsi",
                                     "compare"}
    assert xsi.top == "markov_system" and xsi.spec.xbar.name == "xbar_markov_system"
    assert xsi.work == Path("xsi_work") / "markov_system"
    from examples.markov.markov_build import top_names
    csynth = next(s for s in dag.steps() if s.name == "csynth")
    assert sorted(csynth.tops) == sorted(top_names())     # the cut brings the two writers


def test_two_systems_share_codegen_and_csynth():
    from waveflow.build.system_dag import add_system_steps
    dag = BuildDag()
    for topo in ("per_view", "one_front"):
        add_system_steps(dag, _fir(topo), work_dir="w", top="mm_fir_top", prefix=f"{topo}_",
                         workspace=f"mm_fir_{topo}")
    names = dag.step_names()
    assert [n for n in names if "csynth" in n] == ["csynth"]
    assert {"per_view_system_xsi", "one_front_system_xsi", "per_view_compare",
            "one_front_compare"} <= set(names)
    owners = dag.artifact_owners()
    assert owners["rtl_mm_fir"] == "csynth"
    assert owners["one_front_report"] == "one_front_system_xsi"


def test_through_pysim_on_the_system_dag_needs_no_toolchain(tmp_path):
    from waveflow.build.system_dag import add_system_steps
    dag = BuildDag()
    add_system_steps(dag, _markov(), work_dir="w", top="markov_top")
    res = dag.run(BuildConfig(root_dir=tmp_path), through="pysim")
    assert set(res) == {"scenario", "pysim"} and all(r.success for r in res.values())
    assert (tmp_path / "w" / "markov_top" / "pysim.json").is_file()


@pytest.mark.parametrize("which", sorted(SYSTEMS))
def test_the_top_and_harness_do_not_depend_on_pysim_having_run(tmp_path, which):
    """In the DAG, pysim may run the system object before system_xsi walks it (run_system_xsi did the
    opposite): the generated top and harness must be the same text either way."""
    from waveflow.build.system_dag import SystemXsiStep
    cfg = BuildConfig(root_dir=tmp_path)

    def render(sysm, run_first):
        xsi = SystemXsiStep(sysm=sysm, work=Path("w"), top="t")
        if run_first:
            dag = _pysim_dag(sysm, Path("w"))
            dag.run(cfg)
        top = xsi.inner_dag(tmp_path / "w" / "scenario").steps()
        out = {}
        for step in top:
            if step.name in ("system_top", "harness"):
                out.update(step.run(cfg, scenario=tmp_path / "w" / "scenario"))
        return out

    assert render(SYSTEMS[which](), False) == render(SYSTEMS[which](), True)


def test_report_round_trips(tmp_path):
    from waveflow.build.system_xsi import XsiRun, load_run, parse_output, write_report
    out = "DONE done=1 cycles=618 polls=0 nops=3\nOP W 0x40000000 n=2 s=5 e=9\nOP R 0x10 n=1 s=12 e=20\n"
    run = XsiRun(output=out, workspace=tmp_path, scenario=tmp_path / "scenario",
                 traces=tmp_path / "traces")
    parse_output(out, run)
    write_report(run, tmp_path / "report.json")
    (tmp_path / "pysim.json").write_text('{"cycles": 635.0}')
    (tmp_path / "compare.json").write_text('{"trace_mismatches": ["qin/words.bin"]}')
    back = load_run(tmp_path)
    assert (back.done, back.cycles, back.polls, back.nops) == (True, 618, 0, 3)
    assert back.ops == [(True, 0x40000000, 2, 5, 9), (False, 0x10, 1, 12, 20)]
    assert back.output == out and back.traces == tmp_path / "traces"
    assert back.pysim_cycles == 635.0 and back.pysim_traces == tmp_path / "pysim_traces"
    assert back.trace_mismatches == ["qin/words.bin"]


def test_run_system_xsi_checks_and_never_synthesizes(tmp_path, fake_vitis):
    """The wrapper runs csynth in check mode: no RTL under the root is a failure naming the top, and
    the toolchain is never called."""
    from waveflow.build.system_xsi import run_system_xsi
    for d in ("include", "gen"):
        (tmp_path / d).mkdir()
    with pytest.raises(RuntimeError, match="no csynth RTL for markov_gen"):
        run_system_xsi(_markov(), tmp_path / "w", top="markov_top", root=tmp_path)
    assert fake_vitis == []
