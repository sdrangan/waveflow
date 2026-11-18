"""Tests for StreamUtilsStep and MemMgrStep."""
from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.build.build import BuildConfig
from waveflow.build.streamutils import MemMgrStep, MemStreamStep, StreamUtilsStep


# ---------------------------------------------------------------------------
# BuildConfig.vitis_version_tuple
# ---------------------------------------------------------------------------


def test_vitis_version_tuple_none_when_not_set():
    cfg = BuildConfig()
    assert cfg.vitis_version_tuple() is None


def test_vitis_version_tuple_parses_correctly():
    cfg = BuildConfig(vitis_version="2023.1")
    assert cfg.vitis_version_tuple() == (2023, 1)


def test_vitis_version_tuple_parses_2025_1():
    cfg = BuildConfig(vitis_version="2025.1")
    assert cfg.vitis_version_tuple() == (2025, 1)


def test_vitis_version_tuple_invalid_format_raises():
    cfg = BuildConfig(vitis_version="2025")
    with pytest.raises(ValueError, match="Invalid vitis_version"):
        cfg.vitis_version_tuple()


def test_vitis_version_tuple_non_numeric_raises():
    cfg = BuildConfig(vitis_version="foo.bar")
    with pytest.raises(ValueError, match="Invalid vitis_version"):
        cfg.vitis_version_tuple()


# ---------------------------------------------------------------------------
# BuildConfig.needs_legacy_streamutils_cpp
# ---------------------------------------------------------------------------


def test_needs_legacy_defaults_true_when_no_version():
    cfg = BuildConfig()
    assert cfg.needs_legacy_streamutils_cpp() is True


def test_needs_legacy_true_for_2023_1():
    cfg = BuildConfig(vitis_version="2023.1")
    assert cfg.needs_legacy_streamutils_cpp() is True


def test_needs_legacy_true_for_2024_2():
    cfg = BuildConfig(vitis_version="2024.2")
    assert cfg.needs_legacy_streamutils_cpp() is True


def test_needs_legacy_false_for_2025_1():
    cfg = BuildConfig(vitis_version="2025.1")
    assert cfg.needs_legacy_streamutils_cpp() is False


def test_needs_legacy_false_for_2025_2():
    cfg = BuildConfig(vitis_version="2025.2")
    assert cfg.needs_legacy_streamutils_cpp() is False


def test_needs_legacy_false_for_2026_1():
    cfg = BuildConfig(vitis_version="2026.1")
    assert cfg.needs_legacy_streamutils_cpp() is False


# ---------------------------------------------------------------------------
# StreamUtilsStep — default (no version specified)
# ---------------------------------------------------------------------------


def test_streamutils_step_default_copies_headers(tmp_path: Path):
    result = StreamUtilsStep().run(BuildConfig(root_dir=tmp_path))
    assert result.success
    assert result.artifacts["hls"].exists()
    assert result.artifacts["tb"].exists()


def test_streamutils_step_default_copies_cpp(tmp_path: Path):
    result = StreamUtilsStep().run(BuildConfig(root_dir=tmp_path))
    assert result.success
    assert "cpp" in result.artifacts
    assert result.artifacts["cpp"].exists()


# ---------------------------------------------------------------------------
# StreamUtilsStep — legacy version (2023.1)
# ---------------------------------------------------------------------------


def test_streamutils_step_2023_1_copies_headers(tmp_path: Path):
    cfg = BuildConfig(root_dir=tmp_path, vitis_version="2023.1")
    result = StreamUtilsStep().run(cfg)
    assert result.artifacts["hls"].exists()
    assert result.artifacts["tb"].exists()


def test_streamutils_step_2023_1_copies_cpp(tmp_path: Path):
    cfg = BuildConfig(root_dir=tmp_path, vitis_version="2023.1")
    result = StreamUtilsStep().run(cfg)
    assert "cpp" in result.artifacts
    assert result.artifacts["cpp"].exists()


# ---------------------------------------------------------------------------
# StreamUtilsStep — new version (2025.1)
# ---------------------------------------------------------------------------


def test_streamutils_step_2025_1_copies_headers(tmp_path: Path):
    cfg = BuildConfig(root_dir=tmp_path, vitis_version="2025.1")
    result = StreamUtilsStep().run(cfg)
    assert result.artifacts["hls"].exists()
    assert result.artifacts["tb"].exists()


def test_streamutils_step_2025_1_does_not_copy_cpp(tmp_path: Path):
    cfg = BuildConfig(root_dir=tmp_path, vitis_version="2025.1")
    result = StreamUtilsStep().run(cfg)
    assert "cpp" not in result.artifacts
    assert not (tmp_path / "streamutils.cpp").exists()


def test_streamutils_step_2025_2_does_not_copy_cpp(tmp_path: Path):
    cfg = BuildConfig(root_dir=tmp_path, vitis_version="2025.2")
    result = StreamUtilsStep().run(cfg)
    assert "cpp" not in result.artifacts
    assert not (tmp_path / "streamutils.cpp").exists()


# ---------------------------------------------------------------------------
# StreamUtilsStep — stale-file cleanup
# ---------------------------------------------------------------------------


def test_streamutils_step_removes_stale_cpp_for_2025_1(tmp_path: Path):
    stale = tmp_path / "streamutils.cpp"
    stale.write_text("stale content", encoding="utf-8")

    cfg = BuildConfig(root_dir=tmp_path, vitis_version="2025.1")
    result = StreamUtilsStep().run(cfg)

    assert "cpp" not in result.artifacts
    assert not stale.exists()


def test_streamutils_step_removes_stale_cpp_in_subdir(tmp_path: Path):
    util_dir = tmp_path / "common"
    util_dir.mkdir()
    stale = util_dir / "streamutils.cpp"
    stale.write_text("stale content", encoding="utf-8")

    cfg = BuildConfig(root_dir=tmp_path, vitis_version="2025.1")
    result = StreamUtilsStep(output_dir="common").run(cfg)

    assert "cpp" not in result.artifacts
    assert not stale.exists()


# ---------------------------------------------------------------------------
# StreamUtilsStep — output_dir
# ---------------------------------------------------------------------------


def test_streamutils_step_output_dir_property():
    step = StreamUtilsStep(output_dir="include/util")
    assert step.output_dir == Path("include/util")


def test_streamutils_step_writes_to_output_dir(tmp_path: Path):
    cfg = BuildConfig(root_dir=tmp_path)
    result = StreamUtilsStep(output_dir="util").run(cfg)
    assert (tmp_path / "util" / "streamutils_hls.h").exists()
    assert (tmp_path / "util" / "streamutils_tb.h").exists()


# ---------------------------------------------------------------------------
# StreamUtilsStep — invalid version format raises at run time
# ---------------------------------------------------------------------------


def test_streamutils_step_invalid_version_returns_failure(tmp_path: Path):
    cfg = BuildConfig(root_dir=tmp_path, vitis_version="2025")
    result = StreamUtilsStep().run(cfg)
    assert result.success is False
    assert "Invalid vitis_version" in result.message


# ---------------------------------------------------------------------------
# MemMgrStep
# ---------------------------------------------------------------------------


def test_memmgr_step_copies_headers(tmp_path: Path):
    result = MemMgrStep().run(BuildConfig(root_dir=tmp_path))
    assert result.success
    assert result.artifacts["memmgr"].exists()
    assert result.artifacts["memmgr_tb"].exists()


def test_memmgr_step_output_dir(tmp_path: Path):
    result = MemMgrStep(output_dir="util").run(BuildConfig(root_dir=tmp_path))
    assert (tmp_path / "util" / "memmgr.hpp").exists()
    assert (tmp_path / "util" / "memmgr_tb.hpp").exists()


def test_memstream_step_default_copies_every_body(tmp_path: Path):
    MemStreamStep(output_dir="include").run(BuildConfig(root_dir=tmp_path))
    assert (tmp_path / "include" / "mem_w_stream_done_task.h").exists()
    assert (tmp_path / "include" / "il_load_inband_task.h").exists()


def test_memstream_step_copies_only_the_named_headers(tmp_path: Path):
    step = MemStreamStep(output_dir="include", headers=["mem_r_stream_task", "mem_w_stream_done_task"])
    step.run(BuildConfig(root_dir=tmp_path))
    assert sorted(p.name for p in (tmp_path / "include").iterdir()) == [
        "mem_r_stream_task.h", "mem_w_stream_done_task.h"]


def test_memstream_step_rejects_an_unknown_header():
    with pytest.raises(KeyError, match="mem_bogus_task"):
        MemStreamStep(headers=["mem_bogus_task"])
