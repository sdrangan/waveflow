"""
tests/poly/test_timing_capture.py — integration tests for xsim_vcd callable API.

These tests verify the Python-callable ``run_xsim_vcd`` function.  Tests
that require an actual Vivado/xsim installation are automatically skipped
when that environment is unavailable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Import the callable function under test
# ---------------------------------------------------------------------------

from waveflow.scripts.xsim_vcd import (
    run_xsim_vcd,
    modify_tcl,
    create_vcd_batch,
    check_vcd_not_empty,
    launcher_names,
)


# ---------------------------------------------------------------------------
# Platform guard: the flow itself runs on both OSes, but the integration test
# still needs a real Vivado install plus a prior cosim.
# ---------------------------------------------------------------------------

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Unit tests for helper functions (platform-independent)
# ---------------------------------------------------------------------------


class TestModifyTcl:
    """Tests for the TCL modifier helper — no simulator required."""

    def test_inserts_vcd_commands(self, tmp_path: Path) -> None:
        tcl_src = tmp_path / "poly.tcl"
        tcl_dst = tmp_path / "poly_vcd.tcl"
        tcl_src.write_text(
            "log_wave -r /\nrun all\nquit\n"
        )
        modify_tcl(str(tcl_src), str(tcl_dst), trace_level="*")
        content = tcl_dst.read_text()
        assert "open_vcd" in content
        # `log_vcd` has no -r: xsim 2025.1 rejects it outright and then dumps nothing.  The
        # recursive request becomes `-level 0` over a wildcard from the root scope.
        assert "log_vcd -level 0 /*" in content
        assert "log_vcd -r" not in content

    def test_recursive_long_form_also_translated(self, tmp_path: Path) -> None:
        """Vitis emits `-r`, but `log_wave` spells the same option `-recursive` too."""
        tcl_src = tmp_path / "poly.tcl"
        tcl_dst = tmp_path / "poly_vcd.tcl"
        tcl_src.write_text(
            "log_wave -recursive /apatb_poly_top/*\nrun all\nquit\n"
        )
        modify_tcl(str(tcl_src), str(tcl_dst), trace_level="all")
        content = tcl_dst.read_text()
        assert "log_vcd -level 0 /apatb_poly_top/*" in content
        # The original log_wave line stays -- only the inserted log_vcd is translated.
        assert "log_vcd -recursive" not in content

    def test_replaces_quit_with_close_vcd(self, tmp_path: Path) -> None:
        tcl_src = tmp_path / "poly.tcl"
        tcl_dst = tmp_path / "poly_vcd.tcl"
        tcl_src.write_text(
            "log_wave -r /\nrun all\nquit\n"
        )
        modify_tcl(str(tcl_src), str(tcl_dst), trace_level="*")
        content = tcl_dst.read_text()
        assert "close_vcd" in content
        # Original quit should be replaced
        lines = [l.strip() for l in content.splitlines()]
        assert "quit" in lines  # should still end with quit
        assert "close_vcd" in lines

    def test_port_trace_level(self, tmp_path: Path) -> None:
        tcl_src = tmp_path / "poly.tcl"
        tcl_dst = tmp_path / "poly_vcd.tcl"
        tcl_src.write_text(
            "log_wave [get_objects -filter {type == in_port || type == out_port || type == inout_port || type == port} /apatb_poly_top/AESL_inst_poly/*]\n"
            "run all\n"
            "quit\n"
        )
        modify_tcl(str(tcl_src), str(tcl_dst), trace_level="port")
        content = tcl_dst.read_text()
        assert "log_vcd [get_objects -filter {type == in_port || type == out_port || type == inout_port || type == port} /apatb_poly_top/AESL_inst_poly/*]" in content


class TestCheckVcdNotEmpty:
    """xsim exits 0 after rejecting a log_vcd command, so a VCD that logged nothing looks like
    a successful run.  This check is what turns that into a failure at the point it happens."""

    # What xsim leaves behind when the log_vcd command was rejected: a header and nothing else.
    EMPTY_VCD = "$date\n  today\n$end\n$dumpvars\n$end\n"

    def test_accepts_a_vcd_with_signals(self, tmp_path: Path) -> None:
        vcd = tmp_path / "dump.vcd"
        vcd.write_text("$var wire 1 ! ap_clk $end\n$enddefinitions $end\n")
        check_vcd_not_empty(vcd)  # does not raise

    def test_rejects_a_vcd_with_no_signals(self, tmp_path: Path) -> None:
        vcd = tmp_path / "dump.vcd"
        vcd.write_text(self.EMPTY_VCD)
        with pytest.raises(RuntimeError, match="declares no signals"):
            check_vcd_not_empty(vcd, trace_level="all")

    def test_rejects_a_missing_vcd(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="No VCD was written"):
            check_vcd_not_empty(tmp_path / "absent.vcd")

    def test_quotes_the_rejected_command_from_the_xsim_log(self, tmp_path: Path) -> None:
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        (sim_dir / "xsim.log").write_text(
            "## log_vcd -r /\n"
            "ERROR: [Common 17-170] Unknown option '-r', please type 'log_vcd -help' for usage info.\n"
        )
        vcd = tmp_path / "dump.vcd"
        vcd.write_text(self.EMPTY_VCD)

        with pytest.raises(RuntimeError, match=r"Unknown option '-r'"):
            check_vcd_not_empty(vcd, sim_dir=sim_dir, trace_level="all")


class TestLauncherNames:
    """The launcher Vitis emits differs by OS; both forms stay covered from either host."""

    def test_windows(self) -> None:
        assert launcher_names("nt") == ("run_xsim.bat", "run_xsim_vcd.bat")

    def test_posix(self) -> None:
        assert launcher_names("posix") == ("run_xsim.sh", "run_xsim_vcd.sh")

    def test_defaults_to_this_host(self) -> None:
        assert launcher_names() == launcher_names(os.name)


class TestCreateVcdBatch:
    """Tests for the launcher creator — no simulator required."""

    def test_creates_windows_batch_with_xsim_line(self, tmp_path: Path) -> None:
        original = tmp_path / "run_xsim.bat"
        new_bat = tmp_path / "run_xsim_vcd.bat"
        original.write_text(
            "@echo off\nC:\\Xilinx\\Vivado\\bin\\xsim poly.tcl --nolog\n"
        )
        create_vcd_batch("poly", str(original), str(new_bat), os_name="nt")
        content = new_bat.read_text()
        assert "poly_vcd.tcl" in content
        assert "cd /d" in content

    def test_creates_posix_script_with_xsim_line(self, tmp_path: Path) -> None:
        original = tmp_path / "run_xsim.sh"
        new_sh = tmp_path / "run_xsim_vcd.sh"
        original.write_text(
            "\n/eda/xilinx/2026.1/Vivado/bin/xelab work.poly -s poly\n"
            "/eda/xilinx/2026.1/Vivado/bin/xsim poly -tclbatch poly.tcl\n"
        )
        create_vcd_batch("poly", str(original), str(new_sh), os_name="posix")
        content = new_sh.read_text()
        assert "poly_vcd.tcl" in content
        assert content.startswith("#!/usr/bin/env bash")
        assert 'cd "$(dirname "$0")"' in content
        # The xelab line is deliberately omitted -- the snapshot already exists.
        assert "xelab" not in content

    def test_raises_if_no_xsim_line(self, tmp_path: Path) -> None:
        original = tmp_path / "run_xsim.bat"
        new_bat = tmp_path / "run_xsim_vcd.bat"
        original.write_text("@echo off\n:: nothing useful here\n")
        with pytest.raises(RuntimeError, match="No xsim line found"):
            create_vcd_batch("poly", str(original), str(new_bat))


# ---------------------------------------------------------------------------
# run_xsim_vcd: no longer platform-gated
# ---------------------------------------------------------------------------


class TestRunXsimVcdIsCrossPlatform:
    def test_does_not_refuse_on_non_windows(self, tmp_path: Path) -> None:
        """The old Windows-only guard is gone.

        With no component on disk the call must fail on the *missing files*, not on the platform.
        """
        with pytest.raises((FileNotFoundError, RuntimeError)) as exc:
            run_xsim_vcd(top="poly", comp="does_not_exist", workdir=tmp_path)
        assert "only works on Windows" not in str(exc.value)


# ---------------------------------------------------------------------------
# Integration test: requires Vivado / xsim environment
# ---------------------------------------------------------------------------

_XSIM_AVAILABLE = (
    Path("waveflow_poly_proj").exists()
    or Path("examples/stream_inband/waveflow_poly_proj").exists()
)

requires_xsim = pytest.mark.skipif(
    not _XSIM_AVAILABLE,
    reason="Vivado/xsim environment not available",
)


class TestRunXsimVcdIntegration:
    @requires_xsim
    def test_generates_vcd_file(self, tmp_path: Path) -> None:
        out_path = run_xsim_vcd(
            top="poly",
            comp="waveflow_poly_proj",
            out="test_out.vcd",
            workdir=Path(__file__).resolve().parents[2] / "examples" / "stream_inband",
        )
        assert out_path.exists(), f"Expected VCD at {out_path}"
        assert out_path.stat().st_size > 0
