"""Unit tests for :mod:`waveflow.build.axi_xbar` — configuration checks and Verilog rendering.

No Vivado needed; the generated IP itself is exercised by ``test_axi_xbar_xsi.py`` (``-m xsi``).
"""
from __future__ import annotations

import pytest

from waveflow.build.axi_xbar import (
    AxiXbarConfig,
    AxiXbarRange,
    axi_port_decls,
    axi_signals,
    render_xbar_instance,
)


def _cfg(**kw):
    base = dict(name="xb", n_si=2, mi=[AxiXbarRange(0, 16), AxiXbarRange(0x10000, 16)])
    base.update(kw)
    return AxiXbarConfig(**base)


def test_window_must_be_aligned_and_at_least_4k():
    with pytest.raises(ValueError, match="at least 4 KB"):
        AxiXbarRange(0, 11)
    with pytest.raises(ValueError, match="not aligned"):
        AxiXbarRange(0x800, 12)


def test_overlapping_windows_refused():
    with pytest.raises(ValueError, match="overlap"):
        _cfg(mi=[AxiXbarRange(0, 16), AxiXbarRange(0x8000, 12)])


def test_id_width_must_name_every_si():
    with pytest.raises(ValueError, match="cannot distinguish"):
        _cfg(n_si=3, id_width=1)


def test_digest_tracks_configuration():
    assert _cfg().digest() == _cfg().digest()
    assert _cfg().digest() != _cfg(data_width=32).digest()
    assert _cfg().digest() != _cfg(mi=[AxiXbarRange(0, 16), AxiXbarRange(0x20000, 16)]).digest()


def test_tcl_sets_every_window():
    tcl = _cfg().tcl("ip")
    assert "CONFIG.NUM_SI {2}" in tcl and "CONFIG.NUM_MI {2}" in tcl
    assert "CONFIG.M01_A00_BASE_ADDR {0x0000000000010000}" in tcl
    assert "CONFIG.M01_A00_ADDR_WIDTH {16}" in tcl


def test_instance_concatenates_highest_slot_first():
    v = render_xbar_instance(_cfg(), "u", ["a", "b"], ["x", "y"])
    assert ".s_axi_awaddr({b_AWADDR, a_AWADDR})" in v
    assert ".m_axi_awregion({y_AWREGION, x_AWREGION})" in v
    assert ".aresetn(ap_rst_n)" in v


def test_port_direction_follows_facing():
    sigs = axi_signals(64, 32, 1)
    slave = axi_port_decls("p", sigs, facing="slave")
    master = axi_port_decls("p", sigs, facing="master")
    assert any(d.startswith("input ") and d.endswith("p_AWADDR") for d in slave)
    assert any(d.startswith("output") and d.endswith("p_AWADDR") for d in master)
    assert any(d.startswith("output") and d.endswith("p_BVALID") for d in slave)
    assert not any("REGION" in d for d in slave)


def test_one_by_one_crossbar_refused():
    """create_ip turns NUM_SI=1/NUM_MI=1 into an inconsistent 2-MI IP (measured); refuse it up front."""
    with pytest.raises(ValueError, match="degenerate"):
        AxiXbarConfig(name="x", n_si=1, mi=[AxiXbarRange(0, 12)])


# -- connectivity mode, identity, measurement helpers -------------------------------------------

def test_mode_is_validated_and_reaches_the_tcl():
    with pytest.raises(ValueError, match="mode"):
        _cfg(mode="crossbar")
    assert "CONFIG.CONNECTIVITY_MODE {SASD}" in _cfg(mode="SASD").tcl("ip")
    assert "CONFIG.CONNECTIVITY_MODE {SAMD}" in _cfg().tcl("ip")


def test_default_mode_keeps_existing_cache_keys():
    # Generated before `mode` existed (tests/build/_xsi_work/ip/xbar_witness_2x2_7936979da662): a
    # SAMD crossbar must keep its digest, or every cached IP and XSI build would regenerate.
    x = AxiXbarConfig(name="xbar_witness_2x2", n_si=2,
                      mi=[AxiXbarRange(0, 16), AxiXbarRange(0x10000, 16)])
    assert x.digest() == "7936979da662"
    assert _cfg(mode="SASD").digest() != _cfg().digest()


def test_identity_is_the_hardware_not_the_label():
    a, b = _cfg(name="one"), _cfg(name="two", part="xczu7ev-ffvc1156-2-e")
    assert a.identity() == b.identity()          # same circuit: one key, one synthesis
    assert a.identity().key.startswith("axi_crossbar-")
    assert a.identity().key != _cfg(mode="SASD").identity().key
    assert a.identity().key != _cfg(data_width=32).identity().key
    assert a.identity().params["n_mi"] == 2


def _square(n, **kw):
    return AxiXbarConfig(name="sq", n_si=n, mi=[AxiXbarRange(k << 16, 16) for k in range(n)],
                         id_width=max(1, (n - 1).bit_length()), **kw)


def test_probe_set_follows_the_shape():
    from waveflow.build.axi_xbar import xbar_latency_probes

    assert [p.name for p in xbar_latency_probes(_cfg(n_si=1, id_width=1))] == ["w1", "r1", "w16", "r16"]
    two_one = AxiXbarConfig(name="x", n_si=2, mi=[AxiXbarRange(0, 16), AxiXbarRange(1 << 16, 16)][:1])
    assert [p.name for p in xbar_latency_probes(two_one)][-2:] == ["same_w16", "same_r16"]
    assert "par_w16" not in [p.name for p in xbar_latency_probes(two_one)]
    par = {p.name: p for p in xbar_latency_probes(_square(3))}["par_w16"]
    assert par.ops == ((0, "W", 0, 16), (1, "W", 1, 16), (2, "W", 2, 16))


def test_latency_tb_reads_back_every_write():
    from waveflow.build.axi_xbar import render_xbar_latency_tb, xbar_latency_probes

    cfg = _square(4, data_width=32, mode="SASD")
    tb = render_xbar_latency_tb(cfg, xbar_latency_probes(cfg), "d.dll")
    assert "FlatMemory mem3(16384, 4);" in tb and 'AxiMmMaster h3(sim.dut(), "s3_axi", 4, 3);' in tb
    # same_w16 from SI 2 lands in MI 0's same_* block; same_r16 reads that slot back.
    assert "h2.write(0xe00, ramp(0x4200, 16), 2800);" in tb
    assert "h2.read(0xe00, 16, 3200);" in tb
    assert 'check("same_r16 si2 readback", h2.op(op18).rdata == ramp(0x4200, 16));' in tb


def test_latency_span_is_first_start_to_last_end():
    from waveflow.build.axi_xbar import parse_latency_output, xbar_latency_probes

    probes = xbar_latency_probes(_cfg())
    lines = ["OP w1 0 W 400 406", "OP r1 0 R 800 805", "OP w16 0 W 1200 1220", "OP r16 0 R 1600 1619",
             "OP par_w16 0 W 2000 2020", "OP par_w16 1 W 2000 2024",
             "OP par_r16 0 R 2400 2419", "OP par_r16 1 R 2400 2419",
             "OP same_w16 0 W 2800 2820", "OP same_w16 1 W 2800 2841",
             "OP same_r16 0 R 3200 3219", "OP same_r16 1 R 3200 3239", "PASSED test"]
    spans = parse_latency_output("\n".join(lines), probes)
    assert spans["w1"] == 7 and spans["par_w16"] == 25 and spans["same_w16"] == 42
    with pytest.raises(RuntimeError, match="failed"):
        parse_latency_output("FAIL x\nFAILED test (1)", probes)


_UTIL = """\
1. CLB Logic
------------

+----------------------------+------+-------+------------+-----------+-------+
|          Site Type         | Used | Fixed | Prohibited | Available | Util% |
+----------------------------+------+-------+------------+-----------+-------+
| CLB LUTs*                  |  612 |     0 |          0 |    230400 |  0.27 |
|   LUT as Logic             |  580 |     0 |          0 |    230400 |  0.25 |
|   LUT as Memory            |   32 |     0 |          0 |    101760 |  0.03 |
|     LUT as Distributed RAM |    0 |     0 |            |           |       |
|     LUT as Shift Register  |   32 |     0 |            |           |       |
| CLB Registers              |  901 |     0 |          0 |    460800 |  0.20 |
+----------------------------+------+-------+------------+-----------+-------+

2. BLOCKRAM
-----------

+-------------------+------+-------+------------+-----------+-------+
|     Site Type     | Used | Fixed | Prohibited | Available | Util% |
+-------------------+------+-------+------------+-----------+-------+
| Block RAM Tile    |  1.5 |     0 |          0 |       312 |  0.48 |
|   RAMB36/FIFO*    |    1 |     0 |          0 |       312 |  0.32 |
| URAM              |    0 |     0 |          0 |        96 |  0.00 |
+-------------------+------+-------+------------+-----------+-------+
| DSPs              |    0 |     0 |          0 |      1728 |  0.00 |
"""


def test_utilization_report_is_read_by_row_name():
    from waveflow.utils.vivado_report import parse_markers, parse_utilization

    u = parse_utilization(_UTIL)
    assert u == {"lut": 612, "lutram": 0, "srl": 32, "ff": 901, "bram": 3, "uram": 0, "dsp": 0}
    with pytest.raises(ValueError, match="no LUT row"):
        parse_utilization("nothing here")
    assert parse_markers("x\nWF_WNS_impl 0.412\nWF_VERSION v2025.1\n") == {
        "WNS_impl": "0.412", "VERSION": "v2025.1"}


def test_synth_records_carry_the_closure_verdict():
    from waveflow.build.axi_xbar import XbarSynth, xbar_synth_records

    cfg = _cfg()
    res = XbarSynth(digest=cfg.digest(), part=cfg.part, period_ns=4.0,
                    synth={"lut": 600, "ff": 900}, impl={"lut": 612, "ff": 901},
                    wns_ns=-0.05, whs_ns=0.02, logic_levels=7, tool="vivado v2025.1")
    syn, imp = xbar_synth_records(cfg, res)
    assert (syn.source, imp.source) == ("vivado_synth", "vivado_impl")
    assert imp.payload["lut"] == 612 and imp.payload["met"] is False
    assert imp.provenance.signature == cfg.identity().signature
    assert imp.provenance.period_ns == 4.0


def test_sasd_has_no_mi_side_ids():
    # Measured on the generated wrapper: SASD lacks exactly m_axi_{aw,b,ar,r}id.
    from waveflow.build.axi_xbar import mi_signals, render_xbar_bfm_top

    names = {s[0] for s in mi_signals(_cfg(mode="SASD"))}
    assert not names & {"AWID", "BID", "ARID", "RID"} and "AWREGION" in names
    assert "AWID" in {s[0] for s in mi_signals(_cfg())}
    v = render_xbar_instance(_cfg(mode="SASD"), "u", ["a", "b"], ["x", "y"])
    assert ".m_axi_awid(" not in v and ".s_axi_awid({b_AWID, a_AWID})" in v
    top = render_xbar_bfm_top(_cfg(mode="SASD"), "t")
    assert "_bid_q" not in top and "mx0_axi_BRESP = 2'b00" in top
    assert "mx0_axi_bid_q <= mx0_axi_AWID" in render_xbar_bfm_top(_cfg(), "t")
