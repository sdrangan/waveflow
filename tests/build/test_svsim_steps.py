"""Tests for run_sv_sim / SvSimError / SvSimStep, and for BuildResult.traceback.

Vivado is never invoked: ``subprocess.run`` is monkeypatched so the command
strings the wrapper builds can be asserted directly, and failures simulated by
returning a non-zero code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from waveflow.build.build import BuildConfig, BuildDag, SourceStep
from waveflow.build.svsim_steps import SvSimStep
from waveflow.scripts import sv_sim as sv_sim_mod
from waveflow.scripts.sv_sim import SvSimError, main, run_sv_sim


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def recorded(monkeypatch):
    """Capture the commands run, and let a test dictate the return codes.

    ``sim_tool`` is pinned to the bare tool names so the asserted command strings do not
    depend on whether Vivado happens to be installed on the machine running the tests.
    """
    calls: list[dict[str, Any]] = []
    plan: dict[str, Any] = {"codes": None, "stdout": "", "stderr": ""}

    def fake_run(cmd, shell=False, cwd=None, capture_output=False, text=False):
        calls.append({"cmd": cmd, "cwd": cwd, "capture_output": capture_output})
        codes = plan["codes"]
        rc = 0 if codes is None else codes[min(len(calls) - 1, len(codes) - 1)]
        return _FakeProc(rc, plan["stdout"], plan["stderr"])

    monkeypatch.setattr(sv_sim_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(sv_sim_mod, "sim_tool", lambda name: name)
    return calls, plan


# ---------------------------------------------------------------------------
# run_sv_sim
# ---------------------------------------------------------------------------

class TestRunSvSim:
    def test_runs_the_three_tools_in_order(self, recorded, tmp_path):
        calls, _ = recorded
        (tmp_path / "adder.sv").write_text("")
        (tmp_path / "tb_adder.sv").write_text("")

        res = run_sv_sim([tmp_path / "adder.sv"], tmp_path / "tb_adder.sv",
                         sim_dir=tmp_path / "sim", echo=False)

        assert [c["cmd"].split()[0].strip('"') for c in calls] == ["xvlog", "xelab", "xsim"]
        assert res.top == "tb_adder"

    def test_top_defaults_to_testbench_stem(self, recorded, tmp_path):
        calls, _ = recorded
        run_sv_sim([], tmp_path / "tb_thing.sv", sim_dir=tmp_path / "sim", echo=False)
        assert "tb_thing -s tb_thing_sim" in calls[1]["cmd"]

    def test_explicit_top_overrides(self, recorded, tmp_path):
        calls, _ = recorded
        run_sv_sim([], tmp_path / "tb_thing.sv", top="other",
                   sim_dir=tmp_path / "sim", echo=False)
        assert "other -s other_sim" in calls[1]["cmd"]

    def test_sources_resolved_to_absolute_paths(self, recorded, tmp_path, monkeypatch):
        """sim_dir need not be a child of cwd — the old '../file' form assumed it was."""
        calls, _ = recorded
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.sv").write_text("")
        run_sv_sim(["a.sv"], "tb.sv", sim_dir=tmp_path / "deep" / "nested" / "sim", echo=False)
        assert str(tmp_path.resolve() / "a.sv") in calls[0]["cmd"]

    def test_runall_without_tcl(self, recorded, tmp_path):
        calls, _ = recorded
        run_sv_sim([], tmp_path / "tb.sv", sim_dir=tmp_path / "sim", echo=False)
        assert "--runall" in calls[2]["cmd"]

    def test_tcl_replaces_runall(self, recorded, tmp_path):
        calls, _ = recorded
        tcl = tmp_path / "run.tcl"
        tcl.write_text("")
        run_sv_sim([], tmp_path / "tb.sv", sim_dir=tmp_path / "sim", tcl=tcl, echo=False)
        assert "--runall" not in calls[2]["cmd"]
        assert str(tcl.resolve()) in calls[2]["cmd"]

    def test_plusargs_appended_to_xsim(self, recorded, tmp_path):
        calls, _ = recorded
        run_sv_sim([], tmp_path / "tb.sv", sim_dir=tmp_path / "sim",
                   plusargs={"vecdir": tmp_path / "vectors"}, echo=False)
        assert f'-testplusarg "vecdir={(tmp_path / "vectors").as_posix()}"' in calls[2]["cmd"]

    def test_path_plusargs_use_forward_slashes(self, recorded, tmp_path):
        """A Windows path reaches $value$plusargs with its backslashes read as escapes."""
        calls, _ = recorded
        run_sv_sim([], tmp_path / "tb.sv", sim_dir=tmp_path / "sim",
                   plusargs={"vecdir": Path("C:/Users/x/repos/hw/vectors")}, echo=False)
        arg = [tok for tok in calls[2]["cmd"].split() if "vecdir" in tok][0]
        assert "\\" not in arg

    def test_string_plusargs_pass_through_verbatim(self, recorded, tmp_path):
        calls, _ = recorded
        run_sv_sim([], tmp_path / "tb.sv", sim_dir=tmp_path / "sim",
                   plusargs={"mode": "fast"}, echo=False)
        assert '-testplusarg "mode=fast"' in calls[2]["cmd"]

    def test_no_plusargs_leaves_command_unchanged(self, recorded, tmp_path):
        calls, _ = recorded
        run_sv_sim([], tmp_path / "tb.sv", sim_dir=tmp_path / "sim", echo=False)
        assert "-testplusarg" not in calls[2]["cmd"]

    def test_sim_dir_cleared_by_default(self, recorded, tmp_path):
        sim = tmp_path / "sim"
        sim.mkdir()
        (sim / "stale.txt").write_text("old")
        run_sv_sim([], tmp_path / "tb.sv", sim_dir=sim, echo=False)
        assert not (sim / "stale.txt").exists()

    def test_keep_preserves_sim_dir(self, recorded, tmp_path):
        sim = tmp_path / "sim"
        sim.mkdir()
        (sim / "stale.txt").write_text("old")
        run_sv_sim([], tmp_path / "tb.sv", sim_dir=sim, keep=True, echo=False)
        assert (sim / "stale.txt").exists()

    def test_raises_instead_of_exiting(self, recorded, tmp_path):
        """The whole point of the split: a failure must not kill the interpreter."""
        _, plan = recorded
        plan["codes"] = [1]
        with pytest.raises(SvSimError) as exc:
            run_sv_sim([], tmp_path / "tb.sv", sim_dir=tmp_path / "sim", echo=False)
        assert exc.value.returncode == 1
        assert "xvlog" in exc.value.command

    def test_failure_stops_before_later_tools(self, recorded, tmp_path):
        calls, plan = recorded
        plan["codes"] = [1]
        with pytest.raises(SvSimError):
            run_sv_sim([], tmp_path / "tb.sv", sim_dir=tmp_path / "sim", echo=False)
        assert len(calls) == 1  # xelab and xsim never ran

    def test_captured_output_attached_to_error(self, recorded, tmp_path):
        _, plan = recorded
        plan["codes"] = [1]
        plan["stderr"] = "ERROR: [VRFC 10-3] syntax error near 'endmodule'"
        with pytest.raises(SvSimError) as exc:
            run_sv_sim([], tmp_path / "tb.sv", sim_dir=tmp_path / "sim",
                       capture_output=True, echo=False)
        assert "VRFC 10-3" in str(exc.value)
        assert "VRFC 10-3" in (exc.value.output or "")


class TestSimTool:
    """sim_tool derives xvlog/xelab/xsim from the Vivado install, PATH as fallback."""

    def test_uses_vivado_bin_dir_when_found(self, tmp_path, monkeypatch):
        bindir = tmp_path / "Vivado" / "2025.1" / "bin"
        bindir.mkdir(parents=True)
        exe = "xvlog.bat" if sv_sim_mod.platform.system() == "Windows" else "xvlog"
        (bindir / exe).write_text("")
        (bindir / "vivado").write_text("")
        monkeypatch.setattr(sv_sim_mod, "find_vivado_path", lambda: str(bindir / "vivado"))
        assert sv_sim_mod.sim_tool("xvlog") == str(bindir / exe)

    def test_falls_back_to_bare_name_without_vivado(self, monkeypatch):
        monkeypatch.setattr(sv_sim_mod, "find_vivado_path", lambda: None)
        assert sv_sim_mod.sim_tool("xelab") == "xelab"

    def test_falls_back_when_binary_missing_beside_vivado(self, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        monkeypatch.setattr(sv_sim_mod, "find_vivado_path", lambda: str(bindir / "vivado"))
        assert sv_sim_mod.sim_tool("xsim") == "xsim"


class TestCli:
    def test_main_returns_code_rather_than_raising(self, recorded, tmp_path):
        _, plan = recorded
        plan["codes"] = [3]
        rc = main(["--source", str(tmp_path / "a.sv"),
                   "--tb", str(tmp_path / "tb.sv"),
                   "--sim", str(tmp_path / "sim")])
        assert rc == 3

    def test_main_returns_zero_on_success(self, recorded, tmp_path):
        rc = main(["--source", str(tmp_path / "a.sv"),
                   "--tb", str(tmp_path / "tb.sv"),
                   "--sim", str(tmp_path / "sim")])
        assert rc == 0


# ---------------------------------------------------------------------------
# SvSimStep
# ---------------------------------------------------------------------------

class TestSvSimStep:
    def _step(self, tmp_path, **kw):
        return SvSimStep(
            name="sim",
            sources=["adder.sv"],
            tb="tb_adder.sv",
            sim_artifact="sim_dir",
            sim_dir="sim",
            **kw,
        )

    def test_produces_the_sim_directory(self, tmp_path):
        step = self._step(tmp_path)
        assert step.produces == {"sim_dir": Path("sim")}

    def test_expected_paths_resolved_against_root(self, tmp_path):
        step = self._step(tmp_path)
        cfg = BuildConfig(root_dir=tmp_path)
        assert step.expected_paths(cfg) == {"sim_dir": tmp_path / "sim"}

    def test_run_resolves_paths_against_root(self, recorded, tmp_path):
        calls, _ = recorded
        step = self._step(tmp_path)
        out = step.run(BuildConfig(root_dir=tmp_path))
        assert out == {"sim_dir": tmp_path / "sim"}
        assert str((tmp_path / "adder.sv").resolve()) in calls[0]["cmd"]
        assert str((tmp_path / "tb_adder.sv").resolve()) in calls[0]["cmd"]

    def test_path_plusargs_resolved_against_root(self, recorded, tmp_path):
        calls, _ = recorded
        step = self._step(tmp_path, plusargs={"vecdir": Path("vectors"), "mode": "fast"})
        step.run(BuildConfig(root_dir=tmp_path))
        assert f'-testplusarg "vecdir={(tmp_path / "vectors").as_posix()}"' in calls[2]["cmd"]
        assert '-testplusarg "mode=fast"' in calls[2]["cmd"]  # plain strings pass through

    def test_outputs_join_produces(self, tmp_path):
        step = self._step(tmp_path, outputs={"results": "vectors/out.txt"})
        assert step.produces == {"sim_dir": Path("sim"), "results": Path("vectors/out.txt")}

    def test_outputs_in_expected_paths(self, tmp_path):
        step = self._step(tmp_path, outputs={"results": "vectors/out.txt"})
        paths = step.expected_paths(BuildConfig(root_dir=tmp_path))
        assert paths["results"] == tmp_path / "vectors/out.txt"

    def test_outputs_returned_when_written(self, recorded, tmp_path):
        (tmp_path / "vectors").mkdir()
        (tmp_path / "vectors" / "out.txt").write_text("data")
        step = self._step(tmp_path, outputs={"results": "vectors/out.txt"})
        out = step.run(BuildConfig(root_dir=tmp_path))
        assert out["results"] == tmp_path / "vectors" / "out.txt"

    def test_missing_declared_output_raises(self, recorded, tmp_path):
        """xsim exits 0 even after $fatal, so a silent testbench failure must be caught here."""
        step = self._step(tmp_path, outputs={"results": "vectors/out.txt"})
        with pytest.raises(RuntimeError, match="did not write its declared output"):
            step.run(BuildConfig(root_dir=tmp_path))

    def test_failure_propagates(self, recorded, tmp_path):
        _, plan = recorded
        plan["codes"] = [1]
        step = self._step(tmp_path)
        with pytest.raises(SvSimError):
            step.run(BuildConfig(root_dir=tmp_path))

    def test_dag_records_failure_with_traceback(self, recorded, tmp_path):
        """End-to-end: a failing sim marks the step failed and keeps the traceback."""
        _, plan = recorded
        plan["codes"] = [1]
        (tmp_path / "adder.sv").write_text("")
        (tmp_path / "tb_adder.sv").write_text("")

        dag = BuildDag()
        dag.add(SourceStep(artifact="src", path="adder.sv"))
        dag.add(SourceStep(artifact="tb", path="tb_adder.sv"))
        dag.add(self._step(tmp_path, consumes=["src", "tb"]))

        results = dag.run(BuildConfig(root_dir=tmp_path))
        assert results["sim"].success is False
        assert results["sim"].traceback is not None
        assert "SvSimError" in results["sim"].traceback
