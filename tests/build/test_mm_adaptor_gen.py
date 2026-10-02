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
