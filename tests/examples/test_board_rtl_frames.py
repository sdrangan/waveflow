"""Tool-free checks of S2 XSim launching; subprocess outputs are test doubles."""
import os
import subprocess

import pytest

from examples.stream_inband.board import rtl_frames
from waveflow.toolchain import toolchain


def fake_install(tmp_path, monkeypatch, suffix=""):
    binary_dir = tmp_path / "AMD tools" / "bin"
    binary_dir.mkdir(parents=True)
    vivado = binary_dir / ("vivado" + suffix)
    xsim = binary_dir / ("xsim" + suffix)
    vivado.write_text("tool discovery fixture\n")
    xsim.write_text("subprocess fixture; never executed\n")
    monkeypatch.setattr(rtl_frames, "find_vivado_path", lambda: str(vivado))
    monkeypatch.setattr(toolchain, "_xsi_runner_vars", lambda: {})
    return xsim


@pytest.mark.parametrize("system,suffix", [("Linux", ""), ("Windows", ".bat")])
def test_replay_uses_platform_launcher_environment_and_timeout(tmp_path, monkeypatch, system, suffix):
    xsim = fake_install(tmp_path, monkeypatch, suffix)
    monkeypatch.setattr(toolchain.platform, "system", lambda: system)
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    monkeypatch.setenv("WAVEFLOW_REPLAY_MARKER", "retained")
    sim_dir = tmp_path / "simulation with spaces"
    sim_dir.mkdir()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        (sim_dir / "frames.vcd").write_text("fresh VCD output fixture\n")
        return subprocess.CompletedProcess(command, 0, "XSim replay fixture complete\n")

    monkeypatch.setattr(rtl_frames.subprocess, "run", run)
    assert rtl_frames.dump_cosim_vcd(sim_dir) == sim_dir / "frames.vcd"
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert kwargs["cwd"] == sim_dir and kwargs["timeout"] == 180
    assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.STDOUT
    env = os.environ if kwargs["env"] is None else kwargs["env"]
    assert env["WAVEFLOW_REPLAY_MARKER"] == "retained"
    if system == "Windows":
        assert kwargs["shell"] is True
        assert command.startswith('cmd.exe /c "call ')
        assert f'"{xsim}"' in command and '"-tclbatch" "frames.tcl"' in command
        assert "MSYSTEM" not in env
    else:
        assert kwargs["shell"] is False
        assert command[:4] == [str(xsim), "poly", "-tclbatch", "frames.tcl"]
        assert env["MSYSTEM"] == "MINGW64"
    assert "run all" in (sim_dir / "frames.tcl").read_text()
    assert "complete" in (sim_dir / "frames.log").read_text()


@pytest.mark.parametrize("failure", ["exit_status", "error_message", "missing_output"])
def test_replay_rejects_failures_and_stale_vcd(tmp_path, monkeypatch, failure):
    fake_install(tmp_path, monkeypatch)
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")
    sim_dir = tmp_path / "simulation"
    sim_dir.mkdir()
    vcd = sim_dir / "frames.vcd"
    vcd.write_text("stale result that must not establish success\n")

    def run(command, **kwargs):
        assert not vcd.exists()
        if failure != "missing_output":
            vcd.write_text("new output fixture\n")
        output = "ERROR: fixture failure\n" if failure == "error_message" else "fixture replay\n"
        return subprocess.CompletedProcess(command, int(failure == "exit_status"), output)

    monkeypatch.setattr(rtl_frames.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="frames.log"):
        rtl_frames.dump_cosim_vcd(sim_dir)
    assert "fixture" in (sim_dir / "frames.log").read_text()


@pytest.mark.parametrize("missing", ["vivado", "xsim"])
def test_replay_reports_missing_tools_before_launch(tmp_path, monkeypatch, missing):
    xsim = fake_install(tmp_path, monkeypatch)
    if missing == "vivado":
        monkeypatch.setattr(rtl_frames, "find_vivado_path", lambda: None)
    else:
        xsim.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("Missing tools must be rejected before subprocess launch")

    monkeypatch.setattr(rtl_frames.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="not found"):
        rtl_frames.dump_cosim_vcd(tmp_path)
