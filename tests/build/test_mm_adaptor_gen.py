"""Unit tests for :mod:`waveflow.build.mm_adaptor_gen` (no Vivado; the RTL runs in test_mm_queue_xsi)."""
from __future__ import annotations

import pytest

from waveflow.build.mm_adaptor_gen import QueueView, leaf_sources, render_queue_slot


def test_queue_view_checks():
    with pytest.raises(ValueError, match="'in' or 'out'"):
        QueueView("q", "both", axis="k")
    with pytest.raises(ValueError, match="power of two"):
        QueueView("q", "in", axis="k", depth=100)
    with pytest.raises(ValueError, match="4 KB"):
        QueueView("q", "in", axis="k", law=11)


def test_leaf_sources_exist():
    for p in leaf_sources():
        assert p.is_file(), p


def test_slot_wires_front_to_leaf_and_stream():
    v = render_queue_slot(QueueView("qin", "in", axis="k_in", depth=64), "mi0_axi", 64, 32, 1)
    assert "axi_slave_front #(.DW(64), .AW(32), .IDW(1), .LAW(12)) u_qin_front" in v
    assert ".s_axi_AWADDR(mi0_axi_AWADDR)" in v
    assert "mm_queue_in #(.DW(64), .LAW(12), .DEPTH(64)) u_qin" in v
    assert ".m_axis_TDATA(k_in_TDATA)" in v
    out = render_queue_slot(QueueView("qo", "out", axis="k_out"), "mi1_axi", 64, 32, 1)
    assert "mm_queue_out" in out and ".s_axis_TREADY(k_out_TREADY)" in out


def test_regbank_view_layout_and_render():
    from waveflow.build.mm_adaptor_gen import RegBankView, render_view_slot
    v = RegBankView("regs", ncfg=4, nstat=2, cfg_axis="k_cfg", status_axis="k_stat")
    assert (v.commit_offset(), v.status_offset()) == (0x800, 0xC00)
    txt = render_view_slot(v, "mi0_axi", 64, 32, 1)
    assert "mm_regbank #(.DW(64), .LAW(12), .NCFG(4), .NSTAT(2)) u_regs" in txt
    assert ".m_cfg_TLAST(k_cfg_TLAST)" in txt and ".s_status_TREADY(k_stat_TREADY)" in txt
    with pytest.raises(ValueError, match="at least one"):
        RegBankView("r", ncfg=0, nstat=1, cfg_axis="a", status_axis="b")


def test_adaptor_decoder_routes_by_window():
    from waveflow.build.mm_adaptor_gen import RegBankView, adaptor_law, render_adaptor_slot
    views = [RegBankView("regs", ncfg=5, nstat=2, cfg_axis="k_cfg", status_axis="k_stat"),
             QueueView("qin", "in", axis="k_in", depth=64), QueueView("qout", "out", axis="k_out")]
    assert [adaptor_law(n) for n in (1, 2, 3, 4, 5)] == [12, 13, 14, 14, 15]
    v = render_adaptor_slot("ad", views, "mi0_axi", 64, 32, 1)
    assert "axi_slave_front #(.DW(64), .AW(32), .IDW(1), .LAW(14)) u_ad_front" in v
    assert "wire [1:0] ad_sel = ad_req_addr[13:12];" in v
    assert "assign qin_req_valid = ad_req_valid && (ad_sel == 1);" in v
    assert "assign qout_req_addr  = ad_req_addr[11:0];" in v
    assert "wire ad_hole = (ad_sel >= 3);" in v          # the 4th window answers SLVERR
    assert v.count("axi_slave_front") == 1                # ONE front


def test_adaptor_refuses_duplicate_names_and_odd_windows():
    from waveflow.build.mm_adaptor_gen import render_adaptor_slot
    with pytest.raises(ValueError, match="unique"):
        render_adaptor_slot("ad", [QueueView("q", "in", axis="a"), QueueView("q", "out", axis="b")],
                            "mi0_axi", 64, 32, 1)
    with pytest.raises(ValueError, match="4 KB"):
        render_adaptor_slot("ad", [QueueView("q", "in", axis="a", law=13)], "mi0_axi", 64, 32, 1)


def test_bram_view_renders_memory_and_reads_latency_from_the_verilog():
    from waveflow.build.mm_adaptor_gen import BramView, bram_read_latency, render_view_slot
    assert bram_read_latency() == 1                         # bram_t2p.v publishes READ_LATENCY = 1
    v = render_view_slot(BramView("win", kport="kb", baw=6), "mi0_axi", 64, 32, 1)
    assert "bram_t2p #(.DW(64), .AW(6)) u_win_mem" in v
    assert ".LAT(1)" in v and ".b_dout(kb_dout)" in v
    with pytest.raises(ValueError, match="4 KB"):
        BramView("w", kport="k", law=11)


# --- the front's RD_OUTST and the decoder's order FIFO (plans/bram_fullrate_port.md, Stage 1) ---------

def _needs(n: int):
    """A view whose leaf needs *n* reads outstanding: a stand-in for the rendering alone."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Needs(QueueView):
        rd_outst = n

    return _Needs


def test_rd_outst_is_the_most_any_view_needs_and_appears_only_above_one():
    from waveflow.build.mm_adaptor_gen import front_params, rd_outst_of
    two = _needs(2)("w", "out", axis="k")
    assert rd_outst_of([]) == 1
    assert rd_outst_of([QueueView("q", "in", axis="a")]) == 1
    assert rd_outst_of([QueueView("q", "in", axis="a"), two]) == 2
    assert front_params(64, 32, 1, 12) == {"DW": 64, "AW": 32, "IDW": 1, "LAW": 12}
    assert front_params(64, 32, 1, 12, 1) == front_params(64, 32, 1, 12)
    assert front_params(64, 32, 1, 12, 2)["RD_OUTST"] == 2
    with pytest.raises(ValueError, match="at least 1"):
        front_params(64, 32, 1, 12, 0)


def test_a_single_view_slot_sets_the_front_and_needs_no_order_fifo():
    from waveflow.build.mm_adaptor_gen import render_view_slot
    v = render_view_slot(_needs(2)("w", "out", axis="k"), "mi0_axi", 64, 32, 1)
    assert "axi_slave_front #(.DW(64), .AW(32), .IDW(1), .LAW(12), .RD_OUTST(2)) u_w_front" in v
    assert "_order" not in v


def test_the_order_fifo_replaces_the_latched_select_above_one_outstanding_read():
    from waveflow.build.mm_adaptor_gen import order_fifo_params, render_adaptor_slot
    views = [QueueView("qin", "in", axis="k_in"), QueueView("qout", "out", axis="k_out"),
             _needs(2)("win", "out", axis="k_w")]                  # three views: a hole at 3
    assert order_fifo_params(views[:2]) is None
    assert order_fifo_params(views) == {"W": 3, "DEPTH": 2}          # 2 select bits + the hole bit
    v = render_adaptor_slot("ad", views, "mi0_axi", 64, 32, 1)
    assert "axi_slave_front #(.DW(64), .AW(32), .IDW(1), .LAW(14), .RD_OUTST(2)) u_ad_front" in v
    assert "mm_sync_fifo #(.W(3), .DEPTH(2)) u_ad_order (" in v
    assert ".push(ad_req_valid && ad_req_ready && !ad_req_we), .din({ad_hole, ad_sel})," in v
    assert ".pop(ad_rsp_valid && ad_rsp_ready), .dout({ad_ohole, ad_rsel})," in v
    assert "wire ad_hole_rsp = !ad_oempty && ad_ohole;" in v
    assert "wire [1:0] ad_rsel;" in v and "always" not in v
    assert "assign win_rsp_ready = ad_rsp_ready && !ad_hole_rsp && (ad_rsel == 2);" in v
    # one view behind the front: the select is a constant bit, the FIFO one bit wider than it
    one = render_adaptor_slot("a1", views[2:], "mi0_axi", 64, 32, 1)
    assert "mm_sync_fifo #(.W(2), .DEPTH(2)) u_a1_order (" in one
    # deeper: a power of two that holds every outstanding read
    assert order_fifo_params([_needs(3)("t", "out", axis="k")]) == {"W": 2, "DEPTH": 4}
