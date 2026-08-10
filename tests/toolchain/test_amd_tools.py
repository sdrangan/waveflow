"""Tests for Vivado discovery, version probing, and the `test_amd_tools` report."""

import pytest

from waveflow.scripts import test_amd_tools as report_mod
from waveflow.toolchain import toolchain


@pytest.fixture(autouse=True)
def isolated_toolchain_env(monkeypatch):
    """Keep the tests off any real AMD install on the host running them."""
    monkeypatch.delenv("WAVEFLOW_VITIS_PATH", raising=False)
    monkeypatch.delenv("WAVEFLOW_VIVADO_PATH", raising=False)
    monkeypatch.setattr(toolchain, "_default_roots", lambda product: [])
    monkeypatch.setattr(toolchain.shutil, "which", lambda name: None)


def _make_exe(path, windows: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("@echo off\n" if windows else "#!/bin/sh\n", encoding="utf-8")
    if not windows:
        path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------------------
# find_vivado_path
# ---------------------------------------------------------------------------------------


def test_find_vivado_path_derives_from_unified_layout(tmp_path, monkeypatch):
    """<root>/<version>/Vitis/bin/vitis-run implies <root>/<version>/Vivado/bin/vivado."""
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")

    _make_exe(tmp_path / "2026.1" / "Vitis" / "bin" / "vitis-run", windows=False)
    vivado = _make_exe(tmp_path / "2026.1" / "Vivado" / "bin" / "vivado", windows=False)

    monkeypatch.setenv("WAVEFLOW_VITIS_PATH", str(tmp_path))

    assert toolchain.find_vivado_path() == str(vivado.resolve())


def test_find_vivado_path_derives_from_per_product_layout(tmp_path, monkeypatch):
    """<root>/Vitis/<version>/bin implies the matching <root>/Vivado/<version>/bin."""
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")

    _make_exe(tmp_path / "Vitis" / "2025.1" / "bin" / "vitis-run", windows=False)
    vivado = _make_exe(tmp_path / "Vivado" / "2025.1" / "bin" / "vivado", windows=False)

    monkeypatch.setenv("WAVEFLOW_VITIS_PATH", str(tmp_path / "Vitis"))

    assert toolchain.find_vivado_path() == str(vivado.resolve())


def test_find_vivado_path_picks_version_matching_vitis(tmp_path, monkeypatch):
    """A split install must not pair a new Vitis with an old Vivado from another version."""
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")

    _make_exe(tmp_path / "2024.2" / "Vitis" / "bin" / "vitis-run", windows=False)
    _make_exe(tmp_path / "2024.2" / "Vivado" / "bin" / "vivado", windows=False)
    _make_exe(tmp_path / "2026.1" / "Vitis" / "bin" / "vitis-run", windows=False)
    newer = _make_exe(tmp_path / "2026.1" / "Vivado" / "bin" / "vivado", windows=False)

    monkeypatch.setenv("WAVEFLOW_VITIS_PATH", str(tmp_path))

    assert toolchain.find_vivado_path() == str(newer.resolve())


def test_find_vivado_path_env_override_wins_over_vitis_sibling(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")

    _make_exe(tmp_path / "2026.1" / "Vitis" / "bin" / "vitis-run", windows=False)
    _make_exe(tmp_path / "2026.1" / "Vivado" / "bin" / "vivado", windows=False)
    elsewhere = _make_exe(tmp_path / "split" / "bin" / "vivado", windows=False)

    monkeypatch.setenv("WAVEFLOW_VITIS_PATH", str(tmp_path))
    monkeypatch.setenv("WAVEFLOW_VIVADO_PATH", str(elsewhere))

    assert toolchain.find_vivado_path() == str(elsewhere.resolve())


def test_find_vivado_path_falls_back_to_path(tmp_path, monkeypatch):
    """An environment module that only puts vivado on PATH is still enough to find it."""
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")

    on_path = _make_exe(tmp_path / "modulebin" / "vivado", windows=False)
    monkeypatch.setattr(toolchain.shutil, "which", lambda name: str(on_path))

    assert toolchain.find_vivado_path() == str(on_path.resolve())


def test_find_vivado_path_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")

    assert toolchain.find_vivado_path() is None


def test_find_vivado_path_windows_uses_bat(tmp_path, monkeypatch):
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Windows")

    _make_exe(tmp_path / "2026.1" / "Vitis" / "bin" / "vitis-run.bat", windows=True)
    vivado = _make_exe(tmp_path / "2026.1" / "Vivado" / "bin" / "vivado.bat", windows=True)

    monkeypatch.setenv("WAVEFLOW_VITIS_PATH", str(tmp_path))

    assert toolchain.find_vivado_path() == str(vivado.resolve())


# ---------------------------------------------------------------------------------------
# Version parsing
# ---------------------------------------------------------------------------------------


def test_parse_tool_version_reads_vitis_banner():
    banner = (
        "\n****** vitis-run v2026.1 (64-bit)\n"
        "  **** SW Build 6497934 on 2026-06-16-10:22:25\n"
    )
    assert toolchain.parse_tool_version(banner) == "2026.1"


def test_parse_tool_version_ignores_vivado_tool_version_limit():
    """`Tool Version Limit: 2026.06` must not be mistaken for the release."""
    banner = (
        "vivado v2025.1 (64-bit)\n"
        "Tool Version Limit: 2026.06\n"
        "SW Build 6511674 on Tue Jun 16 11:01:26 MDT 2026\n"
    )
    assert toolchain.parse_tool_version(banner) == "2025.1"


def test_parse_tool_version_returns_none_on_junk():
    assert toolchain.parse_tool_version("command not found") is None


def test_tool_version_falls_back_to_install_path(tmp_path, monkeypatch):
    """A tool that cannot be launched still reports the release from its path."""
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")
    exe = _make_exe(tmp_path / "2025.2" / "Vitis" / "bin" / "vitis-run", windows=False)

    def boom(*args, **kwargs):
        raise OSError("cannot execute")

    monkeypatch.setattr(toolchain.subprocess, "run", boom)

    assert toolchain.tool_version(exe, "--version") == "2025.2"


def test_version_tuple_round_trip():
    assert toolchain.version_tuple("2026.1") == (2026, 1)
    assert toolchain.version_tuple("not-a-version") is None
    assert toolchain.version_tuple(None) is None


# ---------------------------------------------------------------------------------------
# ToolInfo
# ---------------------------------------------------------------------------------------


def _info(version, path="/x/vitis-run"):
    return toolchain.ToolInfo(
        name="Vitis", path=path, version=version, env_var="WAVEFLOW_VITIS_PATH"
    )


def test_tool_info_meets_min_boundary():
    assert _info("2025.1").meets_min is True
    assert _info("2026.1").meets_min is True
    assert _info("2024.2").meets_min is False


def test_tool_info_unknown_version_is_not_a_verdict():
    assert _info(None).meets_min is None
    assert _info(None, path=None).found is False


# ---------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------


def _patch_probe(monkeypatch, vitis_version, vivado_version, found=True):
    infos = [
        toolchain.ToolInfo(
            name="Vitis",
            path="/x/2026.1/Vitis/bin/vitis-run" if found else None,
            version=vitis_version,
            env_var="WAVEFLOW_VITIS_PATH",
        ),
        toolchain.ToolInfo(
            name="Vivado",
            path="/x/2026.1/Vivado/bin/vivado" if found else None,
            version=vivado_version,
            env_var="WAVEFLOW_VIVADO_PATH",
        ),
    ]
    monkeypatch.setattr(report_mod, "probe_amd_tools", lambda check_versions=True: infos)


def test_report_succeeds_when_both_tools_are_current(monkeypatch, capsys):
    _patch_probe(monkeypatch, "2026.1", "2026.1")

    assert report_mod.main([]) == 0

    out = capsys.readouterr().out
    assert "All tools found: Vitis 2026.1, Vivado 2026.1." in out
    assert "WARNING" not in out


def test_report_warns_and_fails_when_tools_are_missing(monkeypatch, capsys):
    _patch_probe(monkeypatch, None, None, found=False)

    assert report_mod.main([]) == 1

    out = capsys.readouterr().out
    assert "NOT FOUND" in out
    assert "WARNING: Vitis and Vivado could not be found." in out
    assert "WAVEFLOW_VITIS_PATH" in out


def test_report_warns_and_fails_on_unsupported_version(monkeypatch, capsys):
    _patch_probe(monkeypatch, "2024.2", "2024.2")

    assert report_mod.main([]) == 1

    out = capsys.readouterr().out
    assert "TOO OLD" in out
    assert "older than the supported 2025.1" in out


def test_report_warns_when_version_is_unreadable(monkeypatch, capsys):
    _patch_probe(monkeypatch, None, None, found=True)

    assert report_mod.main([]) == 1

    out = capsys.readouterr().out
    assert "could not read a version" in out


def test_report_fast_flag_skips_launching_the_tools(monkeypatch):
    seen = {}

    def fake_probe(check_versions=True):
        seen["check_versions"] = check_versions
        return []

    monkeypatch.setattr(report_mod, "probe_amd_tools", fake_probe)

    report_mod.main(["--fast"])

    assert seen["check_versions"] is False
