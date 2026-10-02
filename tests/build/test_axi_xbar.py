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
