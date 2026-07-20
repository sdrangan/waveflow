"""
tests/utils/test_vcd.py – unit tests for waveflow.utils.vcd.SigInfo numeric_values
storage convention.

The convention under test (for numeric_type == 'uint'):
  * wid <= 32  → np.ndarray, dtype np.uint32, shape (n,)
  * wid <= 64  → np.ndarray, dtype np.uint64, shape (n,)
  * wid > 64   → np.ndarray, dtype np.uint64, shape (n, k), k = ceil(wid/64),
                  word 0 = least-significant 64 bits (LSW-first)
"""

from __future__ import annotations

import math
import os
import tempfile

import numpy as np
import pytest

from vcdvcd import VCDVCD

from waveflow.utils.vcd import (
    AximmBeatType,
    SigInfo,
    VcdParser,
    clock_sample_times,
    split_framed_word,
    vcd_trace,
    walk_handshake_bursts,
)


def test_vcd_trace_maps_levels():
    assert vcd_trace("port") == "port"
    assert vcd_trace("all") == "all"
    assert vcd_trace("none") == "*"
    assert vcd_trace("whatever") == "*"


# ---------------------------------------------------------------------------
# Minimal hand-written VCD fixture
# ---------------------------------------------------------------------------

# Signal definitions
#   a  – 8-bit  wire, identifier '!'
#   b  – 40-bit wire, identifier '"'
#   c  – 65-bit wire, identifier '#'
#   d  – 130-bit wire, identifier '$'
#
# Values chosen so that we can verify chunk packing for wide signals.

_VCD_TEXT = """\
$date today $end
$version test $end
$timescale 1ns $end
$scope module top $end
$var wire 8   ! a $end
$var wire 40  " b $end
$var wire 65  # c $end
$var wire 130 $ d $end
$upscope $end
$enddefinitions $end
#0
b00000001 !
b0000000000000000000000000000000000000001 "
b00000000000000000000000000000000000000000000000000000000000000001 #
b0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001 $
#10
b11111111 !
b1111111111111111111111111111111111111111 "
b10000000000000000000000000000000000000000000000000000000000000001 #
b1111111111111111111111111111111111111111111111111111111111111111110000000000000000000000000000000000000000000000000000000000000001 $
"""


@pytest.fixture
def vcd_path(tmp_path):
    """Write the VCD fixture to a temp file and return its path."""
    p = tmp_path / "test.vcd"
    p.write_text(_VCD_TEXT)
    return str(p)


@pytest.fixture
def parsed_signals(vcd_path):
    """
    Parse the VCD fixture and return a dict of {short_name: SigInfo} with
    numeric_values already computed.
    """
    vcd = VCDVCD(vcd_path)
    # Signal full names inside a 'top' scope use 'top.' prefix
    signals = {}
    for full_name in vcd.signals:
        short = full_name.split('.')[-1]
        tv = vcd[full_name].tv
        # Determine width from VCD variable declaration
        wid_map = {'a': 8, 'b': 40, 'c': 65, 'd': 130}
        wid = wid_map.get(short)
        si = SigInfo(full_name, tv, numeric_type='uint', wid=wid)
        si.get_values()
        signals[short] = si
    return signals


# ---------------------------------------------------------------------------
# Tests: dtype and shape
# ---------------------------------------------------------------------------

class TestUintStorageConvention:
    def test_wid_le32_dtype(self, parsed_signals):
        """8-bit signal must produce np.uint32 array."""
        si = parsed_signals['a']
        assert si.numeric_values.dtype == np.uint32, (
            f"Expected np.uint32, got {si.numeric_values.dtype}"
        )
        assert si.numeric_values.ndim == 1

    def test_wid_le64_dtype(self, parsed_signals):
        """40-bit signal must produce np.uint64 array."""
        si = parsed_signals['b']
        assert si.numeric_values.dtype == np.uint64, (
            f"Expected np.uint64, got {si.numeric_values.dtype}"
        )
        assert si.numeric_values.ndim == 1

    def test_wid_gt64_dtype_and_shape(self, parsed_signals):
        """65-bit signal must produce (n, 2) np.uint64 array."""
        si = parsed_signals['c']
        assert si.numeric_values.dtype == np.uint64
        assert si.numeric_values.ndim == 2
        k = math.ceil(65 / 64)  # == 2
        assert si.numeric_values.shape == (2, k), (
            f"Expected shape (2, {k}), got {si.numeric_values.shape}"
        )

    def test_wid_130_dtype_and_shape(self, parsed_signals):
        """130-bit signal must produce (n, 3) np.uint64 array."""
        si = parsed_signals['d']
        assert si.numeric_values.dtype == np.uint64
        assert si.numeric_values.ndim == 2
        k = math.ceil(130 / 64)  # == 3
        assert si.numeric_values.shape == (2, k), (
            f"Expected shape (2, {k}), got {si.numeric_values.shape}"
        )


# ---------------------------------------------------------------------------
# Tests: correct values
# ---------------------------------------------------------------------------

class TestUintValues:
    def test_8bit_values(self, parsed_signals):
        si = parsed_signals['a']
        assert si.numeric_values[0] == 1
        assert si.numeric_values[1] == 0xFF

    def test_40bit_values(self, parsed_signals):
        si = parsed_signals['b']
        assert si.numeric_values[0] == 1
        assert si.numeric_values[1] == (1 << 40) - 1

    def test_65bit_lsw_first(self, parsed_signals):
        """
        65-bit sample #0 = 1  → word[0]=1, word[1]=0
        65-bit sample #1 = (1 << 64) + 1 → word[0]=1, word[1]=1
        """
        si = parsed_signals['c']
        # sample 0
        assert si.numeric_values[0, 0] == np.uint64(1)
        assert si.numeric_values[0, 1] == np.uint64(0)
        # sample 1: value = 2^64 + 1
        assert si.numeric_values[1, 0] == np.uint64(1)
        assert si.numeric_values[1, 1] == np.uint64(1)

    def test_130bit_lsw_first(self, parsed_signals):
        """
        130-bit sample #0 = 1  → word[0]=1, word[1]=0, word[2]=0
        130-bit sample #1 = (2^129 - 2^64 + 1)
          binary = 1 followed by 65 ones, followed by 64 zeros, followed by 1
          Wait, let's recompute: the VCD value for sample #1 is:
            1111...1 (65 ones) 0000...0 (64 zeros) 0000...0001 (actually 1)

          Let's read the exact VCD string:
            b1111111111111111111111111111111111111111111111111111111111111111110000000000000000000000000000000000000000000000000000000000000001

          That is:
            65 ones followed by 64 zeros = 129 bits total, but wid=130, so
            zero-padded on the left to 130 bits:
            0_1111...1(65)_0000...0(64) = 0 followed by 65 ones followed by 64 zeros

          Actually let me count carefully:
            The binary string has 129 characters.
            With zero padding to 130: 0 + 129 chars.

          Numerical value:
            bits [129:65] = 65 ones   → contributes 2^129 - 2^65
            bits [64:1]   = 0 (64 zeros)
            bit  [0]      = 1

          So value = (2^65 - 1) * 2^65 + 1
                   = 2^130 - 2^65 + 1

          In 64-bit words (LSW first):
            word[0] = value & mask64 = 1
            word[1] = (value >> 64) & mask64 = (2^66 - 2) >> 0... 

          Let me just compute directly.
        """
        si = parsed_signals['d']

        # sample 0: value = 1
        assert si.numeric_values[0, 0] == np.uint64(1)
        assert si.numeric_values[0, 1] == np.uint64(0)
        assert si.numeric_values[0, 2] == np.uint64(0)

        # sample 1: compute expected words from the Python int representation
        # The VCD binary string (129 chars + 1 padding = 130-bit value):
        bin_str = "1111111111111111111111111111111111111111111111111111111111111111110000000000000000000000000000000000000000000000000000000000000001"
        value = int(bin_str, 2)
        mask = (1 << 64) - 1
        expected_w0 = np.uint64(value & mask)
        expected_w1 = np.uint64((value >> 64) & mask)
        expected_w2 = np.uint64((value >> 128) & mask)

        assert si.numeric_values[1, 0] == expected_w0
        assert si.numeric_values[1, 1] == expected_w1
        assert si.numeric_values[1, 2] == expected_w2


# ---------------------------------------------------------------------------
# AXI4-MM burst extraction tests
# ---------------------------------------------------------------------------
#
# VCD structure (timescale 1ps, clock period 10 ns):
#   SigInfo divides raw timestamps by time_scale (1e3), so timestamps in ps
#   are converted to ns; e.g. #5000 (5000 ps) → 5.0 ns.
#
#   Clock rises at t = 5000, 15000, 25000, 35000, 45000 ps
#   → cycle indices 0–4 correspond to t_ns = 5, 15, 25, 35, 45 ns.
#
# Each signal change is on its own line (VCDVCD requirement).
# Single-character identifiers A–M (Lite) or A–Q (Full) are used.
#
# AXI4-Lite VCD  (identifiers A=clk … M=RREADY):
#   Write burst 0: AW+W handshake at cycle 0 (addr=0x10, data=0xAA)
#   Write burst 1: AW+W handshake at cycle 1 (addr=0x20, data=0xBB)
#   Read  burst 0: AR+R handshake at cycle 3 (addr=0x30, data=0xCC)
#
# AXI4-Full VCD  (identifiers A=clk … Q=RLAST):
#   Write burst:  AW at cycle 0 (addr=0x40, AWLEN=1),
#                 W beat 0 at cycle 0 (WDATA=0x01, WLAST=0),
#                 W beat 1 at cycle 1 (WDATA=0x02, WLAST=1)
#   Read  burst:  AR at cycle 2 (addr=0x50, ARLEN=1),
#                 R beat 0 at cycle 2 (RDATA=0x03, RLAST=0),
#                 R beat 1 at cycle 3 (RDATA=0x04, RLAST=1)

# ---- AXI4-Lite fixture ----
#
# Signal / identifier mapping:
#   A=clk  B=AWADDR  C=AWVALID  D=AWREADY  E=WDATA  F=WVALID  G=WREADY
#   H=ARADDR  I=ARVALID  J=ARREADY  K=RDATA  L=RVALID  M=RREADY
#
# Timeline (ps):
#   t=0:     clk=0, AWADDR=0x10, AWVALID=1, AWREADY=1, WDATA=0xAA, WVALID=1, WREADY=1
#            ARADDR=0x30, ARVALID=0, ARREADY=0, RDATA=0xCC, RVALID=0, RREADY=1
#   t=5000:  clk=1  [cycle 0: AW+W burst 0]
#   t=8000:  AWADDR=0x20, WDATA=0xBB   (AWVALID/WVALID still 1)
#   t=10000: clk=0
#   t=15000: clk=1  [cycle 1: AW+W burst 1]
#   t=18000: AWVALID=0, AWREADY=0, WVALID=0
#   t=20000: clk=0
#   t=25000: clk=1  [cycle 2: no write handshake]
#   t=28000: ARVALID=1, ARREADY=1, RVALID=1
#   t=30000: clk=0
#   t=35000: clk=1  [cycle 3: AR+R burst 0]
#   t=38000: ARVALID=0, ARREADY=0, RVALID=0
#   t=40000: clk=0
#   t=45000: clk=1  [cycle 4: nothing]
#   t=50000: clk=0

_AXILITE_VCD = """\
$timescale 1ps $end
$scope module top $end
$var wire 1  A clk     $end
$var wire 32 B AWADDR  $end
$var wire 1  C AWVALID $end
$var wire 1  D AWREADY $end
$var wire 32 E WDATA   $end
$var wire 1  F WVALID  $end
$var wire 1  G WREADY  $end
$var wire 32 H ARADDR  $end
$var wire 1  I ARVALID $end
$var wire 1  J ARREADY $end
$var wire 32 K RDATA   $end
$var wire 1  L RVALID  $end
$var wire 1  M RREADY  $end
$upscope $end
$enddefinitions $end
#0
0A
b00000000000000000000000000010000 B
1C
1D
b00000000000000000000000010101010 E
1F
1G
b00000000000000000000000000110000 H
0I
0J
b00000000000000000000000011001100 K
0L
1M
#5000
1A
#8000
b00000000000000000000000000100000 B
b00000000000000000000000010111011 E
#10000
0A
#15000
1A
#18000
0C
0D
0F
#20000
0A
#25000
1A
#28000
1I
1J
1L
#30000
0A
#35000
1A
#38000
0I
0J
0L
#40000
0A
#45000
1A
#50000
0A
"""


@pytest.fixture
def axilite_vcd_path(tmp_path):
    p = tmp_path / "axilite.vcd"
    p.write_text(_AXILITE_VCD)
    return str(p)


@pytest.fixture
def axilite_parser(axilite_vcd_path):
    vcd = VCDVCD(axilite_vcd_path)
    vp = VcdParser(vcd)
    # Add clock
    vp.add_signal("top.clk", short_name="clk")
    vp.sig_info["top.clk"].is_clock = True
    # Add AXI4-MM signals
    aximm_sigs = {
        "AWADDR":  "top.AWADDR",
        "AWVALID": "top.AWVALID",
        "AWREADY": "top.AWREADY",
        "WDATA":   "top.WDATA",
        "WVALID":  "top.WVALID",
        "WREADY":  "top.WREADY",
        "ARADDR":  "top.ARADDR",
        "ARVALID": "top.ARVALID",
        "ARREADY": "top.ARREADY",
        "RDATA":   "top.RDATA",
        "RVALID":  "top.RVALID",
        "RREADY":  "top.RREADY",
    }
    for sig in aximm_sigs.values():
        vp.add_signal(sig)
    return vp, aximm_sigs


# ---- AXI4-Full fixture ----
#
# Signal / identifier mapping:
#   A=clk  B=AWADDR  C=AWVALID  D=AWREADY  E=AWLEN
#   F=WDATA  G=WVALID  H=WREADY  I=WLAST
#   J=ARADDR  K=ARVALID  L=ARREADY  M=ARLEN
#   N=RDATA  O=RVALID  P=RREADY  Q=RLAST
#
# Timeline (ps):
#   t=0:     clk=0, AWADDR=0x40, AWVALID=1, AWREADY=1, AWLEN=1
#            WDATA=0x01, WVALID=1, WREADY=1, WLAST=0
#            ARADDR=0x50, ARVALID=0, ARREADY=0, ARLEN=1
#            RDATA=0x03, RVALID=0, RREADY=1, RLAST=0
#   t=5000:  clk=1  [cycle 0: AW handshake, W beat 0 (WDATA=0x01, WLAST=0)]
#   t=8000:  AWVALID=0, AWREADY=0, WDATA=0x02, WLAST=1
#   t=10000: clk=0
#   t=15000: clk=1  [cycle 1: W beat 1 (WDATA=0x02, WLAST=1) → write burst done]
#   t=18000: WVALID=0, WLAST=0, ARVALID=1, ARREADY=1, RVALID=1
#   t=20000: clk=0
#   t=25000: clk=1  [cycle 2: AR handshake, R beat 0 (RDATA=0x03, RLAST=0)]
#   t=28000: ARVALID=0, ARREADY=0, RDATA=0x04, RLAST=1
#   t=30000: clk=0
#   t=35000: clk=1  [cycle 3: R beat 1 (RDATA=0x04, RLAST=1) → read burst done]
#   t=38000: RVALID=0, RLAST=0
#   t=40000: clk=0
#   t=45000: clk=1  [cycle 4: nothing]
#   t=50000: clk=0

_AXIFULL_VCD = """\
$timescale 1ps $end
$scope module top $end
$var wire 1  A clk     $end
$var wire 32 B AWADDR  $end
$var wire 1  C AWVALID $end
$var wire 1  D AWREADY $end
$var wire 8  E AWLEN   $end
$var wire 32 F WDATA   $end
$var wire 1  G WVALID  $end
$var wire 1  H WREADY  $end
$var wire 1  I WLAST   $end
$var wire 32 J ARADDR  $end
$var wire 1  K ARVALID $end
$var wire 1  L ARREADY $end
$var wire 8  M ARLEN   $end
$var wire 32 N RDATA   $end
$var wire 1  O RVALID  $end
$var wire 1  P RREADY  $end
$var wire 1  Q RLAST   $end
$upscope $end
$enddefinitions $end
#0
0A
b00000000000000000000000001000000 B
1C
1D
b00000001 E
b00000000000000000000000000000001 F
1G
1H
0I
b00000000000000000000000001010000 J
0K
0L
b00000001 M
b00000000000000000000000000000011 N
0O
1P
0Q
#5000
1A
#8000
0C
0D
b00000000000000000000000000000010 F
1I
#10000
0A
#15000
1A
#18000
0G
0I
1K
1L
1O
#20000
0A
#25000
1A
#28000
0K
0L
b00000000000000000000000000000100 N
1Q
#30000
0A
#35000
1A
#38000
0O
0Q
#40000
0A
#45000
1A
#50000
0A
"""


@pytest.fixture
def axifull_vcd_path(tmp_path):
    p = tmp_path / "axifull.vcd"
    p.write_text(_AXIFULL_VCD)
    return str(p)


@pytest.fixture
def axifull_parser(axifull_vcd_path):
    vcd = VCDVCD(axifull_vcd_path)
    vp = VcdParser(vcd)
    vp.add_signal("top.clk", short_name="clk")
    vp.sig_info["top.clk"].is_clock = True
    aximm_sigs = {
        "AWADDR":  "top.AWADDR",
        "AWVALID": "top.AWVALID",
        "AWREADY": "top.AWREADY",
        "AWLEN":   "top.AWLEN",
        "WDATA":   "top.WDATA",
        "WVALID":  "top.WVALID",
        "WREADY":  "top.WREADY",
        "WLAST":   "top.WLAST",
        "ARADDR":  "top.ARADDR",
        "ARVALID": "top.ARVALID",
        "ARREADY": "top.ARREADY",
        "ARLEN":   "top.ARLEN",
        "RDATA":   "top.RDATA",
        "RVALID":  "top.RVALID",
        "RREADY":  "top.RREADY",
        "RLAST":   "top.RLAST",
    }
    for sig in aximm_sigs.values():
        vp.add_signal(sig)
    return vp, aximm_sigs


class TestExtractAximmBurstsLite:
    """Tests for extract_aximm_bursts with AXI4-Lite signals (no AWLEN/WLAST/ARLEN/RLAST)."""

    def test_write_burst_count(self, axilite_parser):
        vp, aximm_sigs = axilite_parser
        wb, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert len(wb) == 2, f"Expected 2 write bursts, got {len(wb)}"

    def test_write_burst_addresses(self, axilite_parser):
        vp, aximm_sigs = axilite_parser
        wb, _, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert int(wb[0]['addr']) == 0x10
        assert int(wb[1]['addr']) == 0x20

    def test_write_burst_data(self, axilite_parser):
        vp, aximm_sigs = axilite_parser
        wb, _, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert len(wb[0]['data']) == 1
        assert int(wb[0]['data'][0]) == 0xAA
        assert len(wb[1]['data']) == 1
        assert int(wb[1]['data'][0]) == 0xBB

    def test_write_awlen_is_none_for_lite(self, axilite_parser):
        vp, aximm_sigs = axilite_parser
        wb, _, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert wb[0]['awlen'] is None
        assert wb[1]['awlen'] is None

    def test_read_burst_count(self, axilite_parser):
        vp, aximm_sigs = axilite_parser
        _, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert len(rb) == 1, f"Expected 1 read burst, got {len(rb)}"

    def test_read_burst_address_and_data(self, axilite_parser):
        vp, aximm_sigs = axilite_parser
        _, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert int(rb[0]['addr']) == 0x30
        assert len(rb[0]['data']) == 1
        assert int(rb[0]['data'][0]) == 0xCC

    def test_clk_period(self, axilite_parser):
        vp, aximm_sigs = axilite_parser
        _, _, clk_period = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert clk_period == pytest.approx(10.0)

    def test_write_beat_type_transfer(self, axilite_parser):
        vp, aximm_sigs = axilite_parser
        wb, _, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        # All accepted beats should be type 0 (transfer)
        assert wb[0]['beat_type'] == [0]
        assert wb[1]['beat_type'] == [0]


class TestExtractAximmBurstsFull:
    """Tests for extract_aximm_bursts with AXI4-Full signals (AWLEN/WLAST/ARLEN/RLAST present)."""

    def test_write_burst_count(self, axifull_parser):
        vp, aximm_sigs = axifull_parser
        wb, _, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert len(wb) == 1, f"Expected 1 write burst, got {len(wb)}"

    def test_write_burst_awlen(self, axifull_parser):
        vp, aximm_sigs = axifull_parser
        wb, _, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert wb[0]['awlen'] == 1

    def test_write_burst_address(self, axifull_parser):
        vp, aximm_sigs = axifull_parser
        wb, _, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert int(wb[0]['addr']) == 0x40

    def test_write_burst_data_two_beats(self, axifull_parser):
        vp, aximm_sigs = axifull_parser
        wb, _, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert len(wb[0]['data']) == 2
        assert int(wb[0]['data'][0]) == 0x01
        assert int(wb[0]['data'][1]) == 0x02

    def test_read_burst_count(self, axifull_parser):
        vp, aximm_sigs = axifull_parser
        _, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert len(rb) == 1, f"Expected 1 read burst, got {len(rb)}"

    def test_read_burst_arlen(self, axifull_parser):
        vp, aximm_sigs = axifull_parser
        _, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert rb[0]['arlen'] == 1

    def test_read_burst_address(self, axifull_parser):
        vp, aximm_sigs = axifull_parser
        _, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert int(rb[0]['addr']) == 0x50

    def test_read_burst_data_two_beats(self, axifull_parser):
        vp, aximm_sigs = axifull_parser
        _, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)
        assert len(rb[0]['data']) == 2
        assert int(rb[0]['data'][0]) == 0x03
        assert int(rb[0]['data'][1]) == 0x04


_AXIMM_SUSTAINED_AR_VCD = """\
$timescale 1ps $end
$scope module top $end
$var wire 1  A clk     $end
$var wire 32 B ARADDR  $end
$var wire 1  C ARVALID $end
$var wire 1  D ARREADY $end
$var wire 32 E RDATA   $end
$var wire 1  F RVALID  $end
$var wire 1  G RREADY  $end
$var wire 1  H RLAST   $end
$upscope $end
$enddefinitions $end
#0
0A
b00000000000000000000000000110000 B
1C
1D
b00000000000000000000000010101010 E
0F
1G
0H
#5000
1A
#10000
0A
#15000
1A
#18000
1F
1H
#20000
0A
#25000
1A
#28000
0C
0D
0F
0H
#30000
0A
"""


@pytest.fixture
def sustained_ar_parser(tmp_path):
    p = tmp_path / "sustained_ar.vcd"
    p.write_text(_AXIMM_SUSTAINED_AR_VCD)

    vcd = VCDVCD(str(p))
    vp = VcdParser(vcd)
    vp.add_signal("top.clk", short_name="clk")
    vp.sig_info["top.clk"].is_clock = True
    aximm_sigs = {
        "ARADDR":  "top.ARADDR",
        "ARVALID": "top.ARVALID",
        "ARREADY": "top.ARREADY",
        "RDATA":   "top.RDATA",
        "RVALID":  "top.RVALID",
        "RREADY":  "top.RREADY",
        "RLAST":   "top.RLAST",
    }
    for sig in aximm_sigs.values():
        vp.add_signal(sig)
    return vp, aximm_sigs


_AXIMM_BACK_TO_BACK_AR_VCD = """\
$timescale 1ps $end
$scope module top $end
$var wire 1  A clk     $end
$var wire 32 B ARADDR  $end
$var wire 1  C ARVALID $end
$var wire 1  D ARREADY $end
$var wire 8  E ARLEN   $end
$var wire 32 F RDATA   $end
$var wire 1  G RVALID  $end
$var wire 1  H RREADY  $end
$var wire 1  I RLAST   $end
$upscope $end
$enddefinitions $end
#0
0A
b00000000000000000000000000010000 B
1C
1D
b00000000 E
b00000000000000000000000010100001 F
0G
1H
0I
#5000
1A
#8000
b00000000000000000000000000100000 B
b00000000000000000000000010100010 F
#10000
0A
#15000
1A
#18000
1G
1I
#20000
0A
#25000
1A
#28000
b00000000000000000000000010110010 F
#30000
0A
#35000
1A
#38000
0C
0D
0G
0I
#40000
0A
"""


@pytest.fixture
def back_to_back_ar_parser(tmp_path):
    p = tmp_path / "back_to_back_ar.vcd"
    p.write_text(_AXIMM_BACK_TO_BACK_AR_VCD)

    vcd = VCDVCD(str(p))
    vp = VcdParser(vcd)
    vp.add_signal("top.clk", short_name="clk")
    vp.sig_info["top.clk"].is_clock = True
    aximm_sigs = {
        "ARADDR":  "top.ARADDR",
        "ARVALID": "top.ARVALID",
        "ARREADY": "top.ARREADY",
        "ARLEN":   "top.ARLEN",
        "RDATA":   "top.RDATA",
        "RVALID":  "top.RVALID",
        "RREADY":  "top.RREADY",
        "RLAST":   "top.RLAST",
    }
    for sig in aximm_sigs.values():
        vp.add_signal(sig)
    return vp, aximm_sigs


class TestExtractAximmBurstsAddressHandshakeDedup:
    def test_sustained_read_address_handshake_creates_one_burst(self, sustained_ar_parser):
        vp, aximm_sigs = sustained_ar_parser

        _, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)

        assert len(rb) == 1
        assert int(rb[0]["addr"]) == 0x30
        assert len(rb[0]["data"]) == 1
        assert int(rb[0]["data"][0]) == 0xAA

    def test_back_to_back_read_addresses_still_create_two_bursts(self, back_to_back_ar_parser):
        vp, aximm_sigs = back_to_back_ar_parser

        _, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)

        assert len(rb) == 2
        assert int(rb[0]["addr"]) == 0x10
        assert int(rb[1]["addr"]) == 0x20
        assert int(rb[0]["data"][0]) == 0xA2
        assert int(rb[1]["data"][0]) == 0xB2

    def test_back_to_back_reads_record_queued_data_phase_start(self, back_to_back_ar_parser):
        vp, aximm_sigs = back_to_back_ar_parser

        _, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)

        assert rb[0]["data_start_idx"] is not None
        assert rb[0]["data_end_idx"] is not None
        assert rb[0]["queue_wait_cycles"] == 0
        assert rb[1]["data_start_idx"] is not None
        assert rb[1]["data_end_idx"] is not None
        assert rb[1]["data_start_idx"] > rb[1]["start_idx"]
        assert rb[1]["queue_wait_cycles"] == rb[1]["data_start_idx"] - rb[1]["start_idx"]

    def test_back_to_back_reads_use_enum_backed_beat_types(self, back_to_back_ar_parser):
        vp, aximm_sigs = back_to_back_ar_parser

        _, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)

        assert all(isinstance(bt, AximmBeatType) for bt in rb[0]["beat_type"])
        assert rb[0]["beat_type"][0] is AximmBeatType.IDLE
        assert AximmBeatType.TRANSFER in rb[0]["beat_type"]


# ---------------------------------------------------------------------------
# Sampling phase
# ---------------------------------------------------------------------------
#
# Every fixture above changes its signals at t=8000/18000/28000 -- strictly BETWEEN clock edges.
# That is not what real RTL does: a flop driven by the edge at T changes at T, and the VCD records
# the new value at T.  So none of the fixtures above can tell edge-sampling from just-before-edge
# sampling, and the phase fix would be silently revertible.
#
# This fixture reproduces the real condition.  ARVALID/ARREADY are raised between edges (t=8000)
# and dropped exactly ON the edge at t=15000.  A flop clocked at 15000 captures the values that
# were held during the preceding cycle -- VALID=1, READY=1 -- so the address IS accepted at cycle
# 1.  Sampling at the edge instead reads the post-edge values (0, 0) and loses the burst entirely.
# This is the mechanism behind the mem_copy trace reading AW=16 instead of AW=128.
#
#   t=0:     clk=0, ARADDR=0x70, ARVALID=0, ARREADY=0, RVALID=0, RREADY=1, RLAST=0
#   t=5000:  clk=1  [cycle 0: nothing]
#   t=8000:  ARVALID=1, ARREADY=1
#   t=10000: clk=0
#   t=15000: clk=1  [cycle 1: address accepted]  AND ARVALID=0, ARREADY=0 at the same timestamp
#   t=18000: RVALID=1, RLAST=1
#   t=20000: clk=0
#   t=25000: clk=1  [cycle 2: single R beat, RLAST -> burst closes]
#   t=28000: RVALID=0, RLAST=0
#   t=30000: clk=0

_AXIMM_EDGE_ALIGNED_VCD = """\
$timescale 1ps $end
$scope module top $end
$var wire 1  A clk     $end
$var wire 32 B ARADDR  $end
$var wire 1  C ARVALID $end
$var wire 1  D ARREADY $end
$var wire 32 E RDATA   $end
$var wire 1  F RVALID  $end
$var wire 1  G RREADY  $end
$var wire 1  H RLAST   $end
$upscope $end
$enddefinitions $end
#0
0A
b00000000000000000000000001110000 B
0C
0D
b00000000000000000000000011011101 E
0F
1G
0H
#5000
1A
#8000
1C
1D
#10000
0A
#15000
1A
0C
0D
#18000
1F
1H
#20000
0A
#25000
1A
#28000
0F
0H
#30000
0A
"""


@pytest.fixture
def edge_aligned_parser(tmp_path):
    p = tmp_path / "edge_aligned.vcd"
    p.write_text(_AXIMM_EDGE_ALIGNED_VCD)

    vcd = VCDVCD(str(p))
    vp = VcdParser(vcd)
    vp.add_signal("top.clk", short_name="clk")
    vp.sig_info["top.clk"].is_clock = True
    aximm_sigs = {
        "ARADDR":  "top.ARADDR",
        "ARVALID": "top.ARVALID",
        "ARREADY": "top.ARREADY",
        "RDATA":   "top.RDATA",
        "RVALID":  "top.RVALID",
        "RREADY":  "top.RREADY",
        "RLAST":   "top.RLAST",
    }
    for sig in aximm_sigs.values():
        vp.add_signal(sig)
    return vp, aximm_sigs


class TestClockSamplingPhase:
    """The handshake must be read from the values the flops captured, not the post-edge ones."""

    def test_sample_times_land_before_the_edge(self):
        clk_times = np.array([5.0, 15.0, 25.0, 35.0])
        st = clock_sample_times(clk_times)
        assert np.allclose(st, [2.5, 12.5, 22.5, 32.5])
        assert len(st) == len(clk_times), "cycle indices must stay aligned with the edges"

    def test_sample_times_degenerate_input(self):
        """Too few edges to infer a period: pass through rather than divide by nothing."""
        assert np.array_equal(clock_sample_times(np.array([5.0])), np.array([5.0]))
        assert len(clock_sample_times(np.array([]))) == 0

    def test_edge_aligned_deassert_still_counts_the_handshake(self, edge_aligned_parser):
        """VALID/READY dropping exactly ON the edge must not erase the accepted address.

        Sampling at the rising edge reads the post-edge (0, 0) and yields zero bursts.
        """
        vp, aximm_sigs = edge_aligned_parser
        _, rb, _ = vp.extract_aximm_bursts("top.clk", aximm_sigs)

        assert len(rb) == 1, f"expected the address accepted at cycle 1, got {len(rb)} bursts"
        assert int(rb[0]["addr"]) == 0x70
        assert rb[0]["start_idx"] == 1

    def test_edge_aligned_burst_labelled_by_true_edge_time(self, edge_aligned_parser):
        """Beats are READ before the edge but must still be REPORTED at the edge time."""
        vp, aximm_sigs = edge_aligned_parser
        _, rb, clk_period = vp.extract_aximm_bursts("top.clk", aximm_sigs)

        assert clk_period == pytest.approx(10.0)
        assert rb[0]["tstart"] == pytest.approx(15.0), "must be the edge time, not 12.5"
        assert len(rb[0]["data"]) == 1
        assert int(rb[0]["data"][0]) == 0xDD


# ---------------------------------------------------------------------------
# framed_word unpacking
# ---------------------------------------------------------------------------

class TestSplitFramedWord:
    """`last` rides one bit above the payload -- there is no TLAST wire on an internal FIFO."""

    def test_narrow_channel_1d(self):
        # framed_word<8> -> a 9-bit net, stored 1-D.
        vals = np.array([0x001, 0x102, 0x0FF, 0x1FF], dtype=np.uint32)
        data, last = split_framed_word(vals, 8)
        assert list(data) == [1, 2, 0xFF, 0xFF]
        assert list(last) == [0, 1, 0, 1]

    def test_64bit_channel_is_2d(self):
        # framed_word<64> -> a 65-bit net, stored (n, 2) uint64 LSW-first.
        vals = np.array([[0xDEADBEEF, 0], [0xCAFE, 1]], dtype=np.uint64)
        data, last = split_framed_word(vals, 64)
        assert list(data) == [0xDEADBEEF, 0xCAFE]
        assert list(last) == [0, 1]

    def test_1d_too_narrow_fails_loud(self):
        vals = np.array([1, 2], dtype=np.uint64)
        with pytest.raises(ValueError, match="1-D"):
            split_framed_word(vals, 64)

    def test_payload_straddling_a_word_boundary_fails_loud(self):
        vals = np.array([[1, 0], [2, 0]], dtype=np.uint64)
        with pytest.raises(ValueError, match="straddles"):
            split_framed_word(vals, 40)


# ---------------------------------------------------------------------------
# The shared burst walker
# ---------------------------------------------------------------------------

class TestWalkHandshakeBursts:
    def test_unframed_channel_returns_one_burst(self):
        """No framing signal -> no boundaries to invent, so all beats form a single burst."""
        clk = np.arange(6, dtype=float)
        data = np.array([10, 11, 12, 13, 14, 15], dtype=np.uint64)
        valid = np.array([1, 1, 0, 0, 1, 0])
        ready = np.array([1, 1, 1, 0, 1, 1])
        bursts = walk_handshake_bursts(data, valid, ready, None, clk)
        assert len(bursts) == 1
        assert list(bursts[0]["data"]) == [10, 11, 14]
        assert bursts[0]["complete"] is False, "nothing closed it -- there is no `last`"

    def test_gap_does_not_split_an_unframed_burst(self):
        """Splitting on idle would break a packet at any stall; the gap is recorded instead."""
        clk = np.arange(4, dtype=float)
        bursts = walk_handshake_bursts(
            np.array([1, 2, 3, 4], dtype=np.uint64),
            np.array([1, 0, 1, 0]), np.array([1, 1, 1, 1]), None, clk)
        assert len(bursts) == 1
        assert bursts[0]["beat_type"] == [0, 1, 0], "the idle cycle is kept, not used to split"

    def test_trailing_idle_is_trimmed(self):
        """Cycles after the final transfer describe the end of the trace, not the packet."""
        clk = np.arange(5, dtype=float)
        bursts = walk_handshake_bursts(
            np.array([1, 2, 3, 4, 5], dtype=np.uint64),
            np.array([1, 0, 0, 0, 0]), np.ones(5), None, clk)
        assert bursts[0]["beat_type"] == [0]

    def test_incomplete_burst_is_returned_not_dropped(self):
        clk = np.arange(3, dtype=float)
        last = np.array([0, 0, 0])
        bursts = walk_handshake_bursts(
            np.array([1, 2, 3], dtype=np.uint64),
            np.ones(3), np.ones(3), last, clk)
        assert len(bursts) == 1
        assert bursts[0]["complete"] is False
        assert list(bursts[0]["data"]) == [1, 2, 3]

    def test_dtype_is_preserved(self):
        """A 64-bit payload must not come back truncated to uint32."""
        clk = np.arange(2, dtype=float)
        big = np.array([0xDEADBEEFCAFEBABE, 0x0123456789ABCDEF], dtype=np.uint64)
        bursts = walk_handshake_bursts(big, np.ones(2), np.ones(2), np.array([0, 1]), clk)
        assert bursts[0]["data"].dtype == np.uint64
        assert int(bursts[0]["data"][0]) == 0xDEADBEEFCAFEBABE


# ---------------------------------------------------------------------------
# Internal HLS FIFO channels
# ---------------------------------------------------------------------------
#
# A framed_word<8> channel -> a 9-bit din/dout net, `last` in bit 8.  Both ends are driven so the
# write side and the read side can be extracted from one trace.  din starts as an X VECTOR
# ('xxxxxxxxx'), which is what every internal net looks like before reset -- the scalar-only
# unknown test used to raise on exactly this.
#
#   cycle 0 : idle
#   cycle 1 : both sides transfer word 1 (last=0)
#   cycle 2 : write side STALLS (full_n=0); read side is IDLE (empty_n=0)
#   cycle 3 : both sides transfer word 2 (last=1) -> packet closes
#   cycle 4 : idle

_FIFO_VCD = """\
$timescale 1ps $end
$scope module top $end
$var wire 1 A clk     $end
$var wire 9 B din [8:0] $end
$var wire 1 C write   $end
$var wire 1 D full_n  $end
$var wire 9 E dout [8:0] $end
$var wire 1 F read    $end
$var wire 1 G empty_n $end
$upscope $end
$enddefinitions $end
#0
0A
bxxxxxxxxx B
0C
1D
bxxxxxxxxx E
0F
0G
#5000
1A
#8000
b000000001 B
1C
b000000001 E
1F
1G
#10000
0A
#15000
1A
#18000
0D
0G
#20000
0A
#25000
1A
#28000
1D
b100000010 B
1G
b100000010 E
#30000
0A
#35000
1A
#38000
0C
0F
0G
#40000
0A
#45000
1A
#50000
0A
"""


@pytest.fixture
def fifo_parser(tmp_path):
    p = tmp_path / "fifo.vcd"
    p.write_text(_FIFO_VCD)
    vcd = VCDVCD(str(p))
    vp = VcdParser(vcd)
    vp.add_signal("top.clk", short_name="clk")
    vp.sig_info["top.clk"].is_clock = True
    sigs = {"din": "top.din[8:0]", "write": "top.write", "full_n": "top.full_n",
            "dout": "top.dout[8:0]", "read": "top.read", "empty_n": "top.empty_n"}
    for s in sigs.values():
        vp.add_signal(s, numeric_type="uint")
    return vp, sigs


class TestExtractFifoBursts:
    def test_write_side_packet(self, fifo_parser):
        vp, sigs = fifo_parser
        bursts, period = vp.extract_fifo_bursts("top.clk", sigs, side="write", data_width=8)
        assert period == pytest.approx(10.0)
        assert len(bursts) == 1
        assert list(bursts[0]["data"]) == [1, 2]
        assert bursts[0]["complete"] is True, "bit 8 of the second word is `last`"

    def test_write_side_sees_the_stall(self, fifo_parser):
        """full_n low with write high is backpressure -- beat_type 2, not a dropped beat."""
        vp, sigs = fifo_parser
        bursts, _ = vp.extract_fifo_bursts("top.clk", sigs, side="write", data_width=8)
        assert bursts[0]["beat_type"] == [0, 2, 0]

    def test_read_side_sees_the_idle(self, fifo_parser):
        """empty_n low with read high is starvation -- beat_type 1."""
        vp, sigs = fifo_parser
        bursts, _ = vp.extract_fifo_bursts("top.clk", sigs, side="read", data_width=8)
        assert len(bursts) == 1
        assert list(bursts[0]["data"]) == [1, 2]
        assert bursts[0]["beat_type"] == [0, 1, 0]

    def test_unframed_fifo_returns_one_burst(self, fifo_parser):
        """Without data_width the framing bit is left in the payload and nothing delimits."""
        vp, sigs = fifo_parser
        bursts, _ = vp.extract_fifo_bursts("top.clk", sigs, side="write", data_width=None)
        assert len(bursts) == 1
        assert list(bursts[0]["data"]) == [0x001, 0x102], "raw 9-bit words, framing bit intact"

    def test_missing_signal_names_fail_loud(self, fifo_parser):
        vp, sigs = fifo_parser
        with pytest.raises(ValueError, match="missing"):
            vp.extract_fifo_bursts("top.clk", {"din": sigs["din"]}, side="write")

    def test_bad_side_fails_loud(self, fifo_parser):
        vp, sigs = fifo_parser
        with pytest.raises(ValueError, match="side must be"):
            vp.extract_fifo_bursts("top.clk", sigs, side="both")

    def test_unknown_vector_samples_are_counted_not_fatal(self, fifo_parser):
        """'xxxxxxxxx' before reset must convert to 0, not raise."""
        vp, sigs = fifo_parser
        vp.extract_fifo_bursts("top.clk", sigs, side="write", data_width=8)
        assert vp.sig_info[sigs["din"]].unknown_count == 1


# ---------------------------------------------------------------------------
# AXI4-Stream without TLAST
# ---------------------------------------------------------------------------
#
# mem_copy's `s_cmd` boundary port is a plain hls::stream<ap_uint<64> >: no TLAST wire exists, and
# framing only begins on the internal channel.  64-bit payload also pins the dtype regression --
# the old extractor cast burst data to uint32.

_AXIS_W0 = 0xDEADBEEFCAFEBABE
_AXIS_W1 = 0x0123456789ABCDEF

_AXIS_NO_TLAST_VCD = f"""\
$timescale 1ps $end
$scope module top $end
$var wire 1  A clk    $end
$var wire 64 B TDATA [63:0] $end
$var wire 1  C TVALID $end
$var wire 1  D TREADY $end
$upscope $end
$enddefinitions $end
#0
0A
b{'x' * 64} B
0C
1D
#5000
1A
#8000
b{_AXIS_W0:064b} B
1C
#10000
0A
#15000
1A
#18000
b{_AXIS_W1:064b} B
#20000
0A
#25000
1A
#28000
0C
#30000
0A
#35000
1A
#40000
0A
"""


@pytest.fixture
def axis_no_tlast_parser(tmp_path):
    p = tmp_path / "axis_no_tlast.vcd"
    p.write_text(_AXIS_NO_TLAST_VCD)
    vcd = VCDVCD(str(p))
    vp = VcdParser(vcd)
    vp.add_signal("top.clk", short_name="clk")
    vp.sig_info["top.clk"].is_clock = True
    sigs = {"tdata": "top.TDATA[63:0]", "tvalid": "top.TVALID", "tready": "top.TREADY"}
    for s in sigs.values():
        vp.add_signal(s, numeric_type="uint")
    return vp, sigs


class TestExtractAxisBurstsWithoutTlast:
    def test_absent_tlast_key_does_not_raise(self, axis_no_tlast_parser):
        """`add_axiss_signals` already treats tlast as optional; the extractor used to
        dereference sig_info[None] anyway."""
        vp, sigs = axis_no_tlast_parser
        assert "tlast" not in sigs
        bursts, _ = vp.extract_axis_bursts("top.clk", sigs)
        assert len(bursts) == 1
        assert bursts[0]["complete"] is False

    def test_explicit_none_tlast_also_works(self, axis_no_tlast_parser):
        vp, sigs = axis_no_tlast_parser
        bursts, _ = vp.extract_axis_bursts("top.clk", dict(sigs, tlast=None))
        assert len(bursts) == 1

    def test_64bit_data_is_not_truncated_to_uint32(self, axis_no_tlast_parser):
        vp, sigs = axis_no_tlast_parser
        bursts, _ = vp.extract_axis_bursts("top.clk", sigs)
        assert bursts[0]["data"].dtype == np.uint64
        assert [int(x) for x in bursts[0]["data"]] == [_AXIS_W0, _AXIS_W1]
