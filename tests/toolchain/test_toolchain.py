from pathlib import Path

from waveflow.toolchain import toolchain
import shutil
import pytest
import subprocess


def test_find_vitis_path_prefers_env_direct_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Windows")

    exe = tmp_path / "vitis-run.bat"
    exe.write_text("@echo off\n", encoding="utf-8")

    monkeypatch.setenv("WAVEFLOW_VITIS_PATH", str(exe))
    out = toolchain.find_vitis_path()

    assert out == str(exe.resolve())


def test_find_vitis_path_selects_highest_version_under_root(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Windows")
    monkeypatch.delenv("WAVEFLOW_VITIS_PATH", raising=False)

    older = tmp_path / "2024.2" / "bin" / "vitis-run.bat"
    newer = tmp_path / "2025.1" / "bin" / "vitis-run.bat"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_text("@echo off\n", encoding="utf-8")
    newer.write_text("@echo off\n", encoding="utf-8")

    out = toolchain.find_vitis_path(top_dir=tmp_path)

    assert out == str(newer.resolve())


def test_find_vitis_path_windows_nested_vitis_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Windows")
    monkeypatch.delenv("WAVEFLOW_VITIS_PATH", raising=False)

    older = tmp_path / "2024.2" / "Vitis" / "bin" / "vitis-run.bat"
    newer = tmp_path / "2025.2" / "Vitis" / "bin" / "vitis-run.bat"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_text("@echo off\n", encoding="utf-8")
    newer.write_text("@echo off\n", encoding="utf-8")

    out = toolchain.find_vitis_path(top_dir=tmp_path)

    assert out == str(newer.resolve())


def test_find_vitis_path_linux_uses_env_root(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")

    v1 = tmp_path / "2024.1" / "bin" / "vitis-run"
    v2 = tmp_path / "2024.2" / "bin" / "vitis-run"
    v1.parent.mkdir(parents=True)
    v2.parent.mkdir(parents=True)
    v1.write_text("#!/bin/sh\n", encoding="utf-8")
    v2.write_text("#!/bin/sh\n", encoding="utf-8")
    v1.chmod(0o755)
    v2.chmod(0o755)

    monkeypatch.setenv("WAVEFLOW_VITIS_PATH", str(tmp_path))
    out = toolchain.find_vitis_path()

    assert out == str(v2.resolve())


def test_run_vitis_hls_result_passed(monkeypatch, tmp_path):
    expected = {
        "status": "passed",
        "stdout": "ok\n",
        "stderr": "",
        "message": None,
    }

    def fake_subprocess_result(cmd_list, work_dir=None, capture_output=True, output_path=None, env=None):
        assert cmd_list == [str(tmp_path / "vitis-run.bat"), "--mode", "hls", "--tcl", str(tmp_path / "run.tcl"), "--tclargs", "alpha", "beta"]
        assert work_dir == tmp_path
        assert capture_output is True
        assert output_path is None
        assert env is None
        return expected

    monkeypatch.setattr(toolchain, "find_vitis_path", lambda top_dir=None: str(tmp_path / "vitis-run.bat"))
    monkeypatch.setattr(toolchain, "subprocess_result", fake_subprocess_result)

    out = toolchain.run_vitis_hls_result(
        tmp_path / "run.tcl",
        work_dir=tmp_path,
        args=["alpha", "beta"],
    )

    assert out == expected


def test_run_vitis_hls_result_subprocess_error(monkeypatch, tmp_path):
    expected = {
        "status": "subprocess_error",
        "stdout": "step log\n",
        "stderr": "failed\n",
        "message": "Command returned non-zero exit status 1",
    }

    def fake_subprocess_result(cmd_list, work_dir=None, capture_output=True, output_path=None, env=None):
        assert env is None
        return expected

    monkeypatch.setattr(toolchain, "find_vitis_path", lambda top_dir=None: str(tmp_path / "vitis-run.bat"))
    monkeypatch.setattr(toolchain, "subprocess_result", fake_subprocess_result)

    out = toolchain.run_vitis_hls_result(tmp_path / "run.tcl", work_dir=tmp_path)

    assert out["status"] == "subprocess_error"
    assert out["stdout"] == "step log\n"
    assert out["stderr"] == "failed\n"
    assert "non-zero exit status 1" in out["message"]


def test_run_vitis_hls_result_runtime_error(monkeypatch, tmp_path):
    monkeypatch.setattr(toolchain, "find_vitis_path", lambda top_dir=None: None)

    out = toolchain.run_vitis_hls_result(tmp_path / "run.tcl", work_dir=tmp_path)

    assert out == {
        "status": "runtime_error",
        "stdout": None,
        "stderr": None,
        "message": (
            "Vitis installation not found. Please set WAVEFLOW_VITIS_PATH "
            "(run `test_amd_tools` to diagnose)."
        ),
    }


def test_subprocess_result_passed(monkeypatch, tmp_path):
    expected = subprocess.CompletedProcess(
        args=["echo", "hello"],
        returncode=0,
        stdout="ok\n",
        stderr="",
    )

    def fake_run(final_cmd, cwd=None, shell=None, check=None, text=None, capture_output=None, env=None):
        assert cwd == tmp_path
        assert check is True
        assert text is True
        assert capture_output is True
        assert env is None
        return expected

    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", fake_run)

    out = toolchain.subprocess_result(["echo", "hello"], work_dir=tmp_path)

    assert out == {
        "status": "passed",
        "stdout": "ok\n",
        "stderr": "",
        "message": None,
    }


def test_subprocess_result_writes_report_and_forces_capture(monkeypatch, tmp_path):
    expected = subprocess.CompletedProcess(
        args=["echo", "hello"],
        returncode=0,
        stdout="ok\n",
        stderr="warn\n",
    )

    def fake_run(final_cmd, cwd=None, shell=None, check=None, text=None, capture_output=None, env=None):
        assert cwd == tmp_path
        assert capture_output is True
        assert env is None
        return expected

    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", fake_run)

    report_path = tmp_path / "logs" / "cmd_result.txt"
    out = toolchain.subprocess_result(
        ["echo", "hello"],
        work_dir=tmp_path,
        capture_output=False,
        output_path=report_path,
    )

    assert out["status"] == "passed"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "status\npassed\n\n" in content
    assert "stderr\nwarn\n\n" in content
    assert "stdout\nok\n" in content


def test_run_vitis_hls_uses_shared_command_builder(monkeypatch, tmp_path):
    expected = subprocess.CompletedProcess(
        args=["echo", "hello"],
        returncode=0,
        stdout="ok\n",
        stderr="",
    )

    def fake_run(final_cmd, cwd=None, shell=None, check=None, text=None, capture_output=None, env=None):
        assert final_cmd == [str(tmp_path / "vitis-run"), "--mode", "hls", "--tcl", str(tmp_path / "run.tcl"), "--tclargs", "alpha", "beta"]
        assert cwd == tmp_path
        assert shell is False
        assert check is True
        assert text is True
        assert capture_output is False
        assert env is None
        return expected

    monkeypatch.setattr(toolchain, "find_vitis_path", lambda top_dir=None: str(tmp_path / "vitis-run"))
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", fake_run)

    out = toolchain.run_vitis_hls(
        tmp_path / "run.tcl",
        work_dir=tmp_path,
        args=["alpha", "beta"],
        capture_output=False,
    )

    assert out is expected


def test_run_vitis_hls_result_writes_report_file(monkeypatch, tmp_path):
    expected = {
        "status": "passed",
        "stdout": "ok\n",
        "stderr": "warn\n",
        "message": None,
    }

    def fake_subprocess_result(cmd_list, work_dir=None, capture_output=True, output_path=None, env=None):
        assert output_path == report_path
        assert env is None
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("status\npassed\n\nmessage\nNone\n\nstderr\nwarn\n\nstdout\nok\n", encoding="utf-8")
        return expected

    report_path = tmp_path / "logs" / "vitis_result.txt"
    monkeypatch.setattr(toolchain, "find_vitis_path", lambda top_dir=None: str(tmp_path / "vitis-run.bat"))
    monkeypatch.setattr(toolchain, "subprocess_result", fake_subprocess_result)

    out = toolchain.run_vitis_hls_result(
        tmp_path / "run.tcl",
        work_dir=tmp_path,
        output_path=report_path,
    )

    assert out["status"] == "passed"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "status\npassed\n\n" in content
    assert "message\nNone\n\n" in content
    assert "stderr\nwarn\n\n" in content
    assert "stdout\nok\n" in content


def test_run_vitis_hls_result_forces_capture_when_writing_report(monkeypatch, tmp_path):
    def fake_subprocess_result(cmd_list, work_dir=None, capture_output=True, output_path=None, env=None):
        assert capture_output is True
        assert env is None
        return {
            "status": "passed",
            "stdout": "ok\n",
            "stderr": "",
            "message": None,
        }

    monkeypatch.setattr(toolchain, "find_vitis_path", lambda top_dir=None: str(tmp_path / "vitis-run.bat"))
    monkeypatch.setattr(toolchain, "subprocess_result", fake_subprocess_result)

    out = toolchain.run_vitis_hls_result(
        tmp_path / "run.tcl",
        work_dir=tmp_path,
        capture_output=False,
        output_path=tmp_path / "vitis_result.txt",
    )

    assert out["status"] == "passed"


# Get the directory where this test file lives
TEST_DIR = Path(__file__).parent
RESOURCE_DIR = TEST_DIR / "resources"

@pytest.mark.vitis
def test_vitis_smoke_with_resources(tmp_path):
    vitis_path = toolchain.find_vitis_path()
    if not vitis_path:
        pytest.skip("Vitis installation not found; skipping smoke integration test.")

    # 1. Copy the "Golden" files to the temp workspace
    # This keeps the original resources 'clean'
    shutil.copy(RESOURCE_DIR / "smoke_test.cpp", tmp_path / "main.cpp")
    shutil.copy(RESOURCE_DIR / "smoke_test.tcl", tmp_path / "run.tcl")
    
    # 2. Run
    result = toolchain.run_vitis_hls(tmp_path / "run.tcl", work_dir=tmp_path)
    
    # 3. Assert
    assert result.returncode == 0