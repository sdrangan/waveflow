"""
tests/poly/test_timing_analysis.py — fixture-based tests for the poly
AXI4-Stream timing analysis API.

These tests use a committed VCD fixture and do not require Vivado or
any RTL simulation environment to run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# ---- path setup for examples/stream_inband sibling module ----
_POLY_DIR = Path(__file__).resolve().parents[2] / "examples" / "stream_inband"
if str(_POLY_DIR) not in sys.path:
    sys.path.insert(0, str(_POLY_DIR))

from timing_analysis import PolyTimingResult, analyze_poly_vcd

# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

FIXTURE_VCD = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "poly"
    / "timing"
    / "poly_timing_fixture.vcd"
)


@pytest.fixture(scope="module")
def result() -> PolyTimingResult:
    """Parse the committed VCD fixture once and share the result."""
    return analyze_poly_vcd(FIXTURE_VCD)


# ---------------------------------------------------------------------------
# VCD loading
# ---------------------------------------------------------------------------


class TestVcdLoading:
    def test_fixture_exists(self) -> None:
        assert FIXTURE_VCD.exists(), f"Fixture VCD not found: {FIXTURE_VCD}"

    def test_fixture_is_what_the_generator_renders(self) -> None:
        from tests.poly.poly_timing_fixture import render

        assert FIXTURE_VCD.read_text(encoding="utf-8") == render(), (
            "the poly timing fixture is stale: python -m tests.poly.poly_timing_fixture")

    def test_analyze_returns_result(self, result: PolyTimingResult) -> None:
        assert isinstance(result, PolyTimingResult)


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


class TestClock:
    def test_clk_name_found(self, result: PolyTimingResult) -> None:
        assert result.clk_name is not None
        assert "clk" in result.clk_name.lower() or "clock" in result.clk_name.lower()

    def test_clk_period_positive(self, result: PolyTimingResult) -> None:
        assert result.clk_period is not None
        assert result.clk_period > 0

    def test_clk_period_value(self, result: PolyTimingResult) -> None:
        # Fixture uses 10 ns clock period
        assert abs(result.clk_period - 10.0) < 1.0


# ---------------------------------------------------------------------------
# Signal discovery
# ---------------------------------------------------------------------------


class TestSignalDiscovery:
    def test_in_signals_keys(self, result: PolyTimingResult) -> None:
        for key in ("tdata", "tvalid", "tready", "tlast"):
            assert key in result.in_signals, f"Missing in_signals key: {key}"
            assert result.in_signals[key] is not None

    def test_out_signals_keys(self, result: PolyTimingResult) -> None:
        for key in ("tdata", "tvalid", "tready", "tlast"):
            assert key in result.out_signals, f"Missing out_signals key: {key}"
            assert result.out_signals[key] is not None


# ---------------------------------------------------------------------------
# Burst counts
# ---------------------------------------------------------------------------


class TestBurstCounts:
    def test_input_burst_count(self, result: PolyTimingResult) -> None:
        # DATA cmd_hdr burst + sample data burst (+ optional END cmd_hdr burst)
        assert len(result.bursts_in) >= 2

    def test_output_burst_count(self, result: PolyTimingResult) -> None:
        # resp_hdr burst + sample data burst
        assert len(result.bursts_out) >= 2

    def test_cmd_hdr_burst_nwords(self, result: PolyTimingResult) -> None:
        # PolyCmdHdr = cmd_type(1) + tx_id(1) + nsamp(1) packed = 1 word
        assert len(result.bursts_in[0]["data"]) >= 1

    def test_resp_hdr_burst_nwords(self, result: PolyTimingResult) -> None:
        # PolyRespHdr = tx_id(1) = 1 word
        assert len(result.bursts_out[0]["data"]) == 1


# ---------------------------------------------------------------------------
# Decoded command header
# ---------------------------------------------------------------------------


class TestCommandHeader:
    def test_cmd_hdr_not_none(self, result: PolyTimingResult) -> None:
        assert result.cmd_hdr is not None

    def test_tx_id(self, result: PolyTimingResult) -> None:
        assert result.cmd_hdr.val["tx_id"] == 42

    def test_nsamp(self, result: PolyTimingResult) -> None:
        assert result.cmd_hdr.val["nsamp"] == 3

    def test_cmd_type_is_data(self, result: PolyTimingResult) -> None:
        # The first input burst is the DATA command header.
        assert int(result.cmd_hdr.val["cmd_type"]) == 0


# ---------------------------------------------------------------------------
# Input sample array
# ---------------------------------------------------------------------------


class TestInputSamples:
    def test_x_not_none(self, result: PolyTimingResult) -> None:
        assert result.x is not None

    def test_x_shape(self, result: PolyTimingResult) -> None:
        nsamp = int(result.cmd_hdr.val["nsamp"])
        assert result.x.shape == (nsamp,)

    def test_x_first_value(self, result: PolyTimingResult) -> None:
        np.testing.assert_allclose(result.x[0], 0.0, atol=1e-6)

    def test_x_last_value(self, result: PolyTimingResult) -> None:
        np.testing.assert_allclose(result.x[-1], 1.0, atol=1e-5)


# ---------------------------------------------------------------------------
# Decoded response header
# ---------------------------------------------------------------------------


class TestResponseHeader:
    def test_resp_hdr_not_none(self, result: PolyTimingResult) -> None:
        assert result.resp_hdr is not None

    def test_resp_hdr_tx_id(self, result: PolyTimingResult) -> None:
        assert result.resp_hdr.val["tx_id"] == 42


# ---------------------------------------------------------------------------
# Output sample array
# ---------------------------------------------------------------------------


class TestOutputSamples:
    def test_y_not_none(self, result: PolyTimingResult) -> None:
        assert result.y is not None

    def test_y_shape(self, result: PolyTimingResult) -> None:
        nsamp = int(result.cmd_hdr.val["nsamp"])
        assert result.y.shape == (nsamp,)

    def test_y_values(self, result: PolyTimingResult) -> None:
        # y = 1 - 2x - 3x^2 + 4x^3 for x in [0, 0.5, 1]
        expected = np.array([1.0, -0.25, 0.0], dtype=np.float32)
        np.testing.assert_allclose(result.y, expected, rtol=1e-5, atol=1e-6)


# ---------------------------------------------------------------------------
# Halt status is now read from the AXI-Lite regmap, not from a streamed footer,
# so there is no per-transaction footer burst to decode here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Missing file error
# ---------------------------------------------------------------------------


class TestErrors:
    def test_missing_vcd_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            analyze_poly_vcd("/nonexistent/path/to/file.vcd")
