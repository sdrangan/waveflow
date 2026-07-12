"""Tests for the endpoint direction-as-capability layer (roadmap Phase 1).

``@port_read`` / ``@port_write`` tag the concrete transaction methods, and
``endpoint.as_dir('R' | 'W' | 'RW')`` hands out a capability view exposing only
the matching-direction subset — a read view raises ``AttributeError`` on a write
call and vice versa.  These tests pin the classification and the proxy behaviour
without needing a bound interface (the check fires at attribute access).
"""
from __future__ import annotations

import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import IntField
from waveflow.hw.interface import (
    CapabilityView,
    InterfaceEndpoint,
    StreamIFMaster,
    StreamIFSlave,
    _classify_port_dir,
    port_read,
    port_write,
)
from waveflow.hw.memif import MMIFMaster
from waveflow.hw.schema_transfer_interface import (
    SchemaTransferIFMaster,
    SchemaTransferIFSlave,
)
from waveflow.simulation.simulation import Simulation


Word32 = IntField.specialize(bitwidth=32, signed=False)


@pytest.fixture
def sim():
    return Simulation()


# ---------------------------------------------------------------------------
# The tags themselves
# ---------------------------------------------------------------------------

def test_port_tags_set_marker():
    """``@port_read`` / ``@port_write`` set ``__port_dir__`` on the function."""
    assert MMIFMaster.read.__port_dir__ == 'R'
    assert MMIFMaster.read_array.__port_dir__ == 'R'
    assert MMIFMaster.poll_until.__port_dir__ == 'R'
    assert MMIFMaster.write.__port_dir__ == 'W'
    assert MMIFMaster.write_array.__port_dir__ == 'W'
    assert StreamIFSlave.get.__port_dir__ == 'R'
    assert StreamIFMaster.write.__port_dir__ == 'W'


def test_tag_coexists_with_synthesizable():
    """A tagged ``@synthesizable`` method keeps both markers (order-independent)."""
    assert MMIFMaster.read_array._is_synthesizable is True
    assert MMIFMaster.read_array.__port_dir__ == 'R'
    assert StreamIFSlave.get._is_synthesizable is True
    assert StreamIFSlave.get.__port_dir__ == 'R'


# ---------------------------------------------------------------------------
# _classify_port_dir — explicit tag wins, else the name heuristic
# ---------------------------------------------------------------------------

def test_classify_explicit_tag_wins():
    @port_read
    def f_write_named_but_read_tagged():
        pass

    # Name says write, tag says read -> tag wins.
    assert _classify_port_dir('f_write_named_but_read_tagged',
                              f_write_named_but_read_tagged) == 'R'


def test_classify_name_heuristic():
    def read_foo():
        pass

    def get_bar():
        pass

    def poll_baz():
        pass

    def write_foo():
        pass

    def put_bar():
        pass

    def helper():
        pass

    assert _classify_port_dir('read_foo', read_foo) == 'R'
    assert _classify_port_dir('get_bar', get_bar) == 'R'
    assert _classify_port_dir('poll_baz', poll_baz) == 'R'
    assert _classify_port_dir('write_foo', write_foo) == 'W'
    assert _classify_port_dir('put_bar', put_bar) == 'W'
    # Untyped callable -> None (allowed on any direction).
    assert _classify_port_dir('helper', helper) is None
    # Non-callable -> None.
    assert _classify_port_dir('bitwidth', 32) is None


# ---------------------------------------------------------------------------
# MMIFMaster capability views
# ---------------------------------------------------------------------------

def test_mmif_master_read_view(sim):
    m = MMIFMaster(sim=sim, bitwidth=32)
    r = m.as_dir('R')
    assert isinstance(r, CapabilityView)
    # Read-direction methods are reachable.
    assert r.read is not None
    assert r.read_array is not None
    assert r.poll_until is not None
    # Write-direction methods are blocked.
    with pytest.raises(AttributeError):
        _ = r.write
    with pytest.raises(AttributeError):
        _ = r.write_array


def test_mmif_master_write_view(sim):
    m = MMIFMaster(sim=sim, bitwidth=32)
    w = m.as_dir('W')
    assert isinstance(w, CapabilityView)
    # Write-direction methods are reachable.
    assert w.write is not None
    assert w.write_array is not None
    # Read-direction methods are blocked.
    with pytest.raises(AttributeError):
        _ = w.read
    with pytest.raises(AttributeError):
        _ = w.read_array
    with pytest.raises(AttributeError):
        _ = w.poll_until


def test_mmif_master_rw_view_is_endpoint(sim):
    m = MMIFMaster(sim=sim, bitwidth=32)
    rw = m.as_dir('RW')
    # 'RW' is the endpoint itself — full, unrestricted access.
    assert rw is m
    assert rw.read is not None
    assert rw.write is not None


def test_mmif_master_untagged_helper_passes(sim):
    """An untagged helper (no direction hint) is reachable on any view."""
    m = MMIFMaster(sim=sim, bitwidth=32)
    # `region` carries no read/write hint -> classified None -> allowed.
    assert m.as_dir('R').region is not None
    assert m.as_dir('W').region is not None


def test_as_dir_invalid_direction(sim):
    m = MMIFMaster(sim=sim, bitwidth=32)
    with pytest.raises(ValueError):
        m.as_dir('X')


# ---------------------------------------------------------------------------
# Region (element-coordinate memory view) capability
# ---------------------------------------------------------------------------

def test_region_read_write_views(sim):
    m = MMIFMaster(sim=sim, bitwidth=32)
    region = m.region(0x1000, Word32, word_bw=32)

    r = region.as_dir('R')
    assert r.read_slice is not None
    assert r.read_slice_pipelined is not None
    with pytest.raises(AttributeError):
        _ = r.write_slice
    with pytest.raises(AttributeError):
        _ = r.write_slice_pipelined

    w = region.as_dir('W')
    assert w.write_slice is not None
    assert w.write_slice_pipelined is not None
    with pytest.raises(AttributeError):
        _ = w.read_slice
    with pytest.raises(AttributeError):
        _ = w.read_slice_pipelined

    assert region.as_dir('RW') is region


# ---------------------------------------------------------------------------
# Stream endpoints
# ---------------------------------------------------------------------------

def test_stream_slave_read_view(sim):
    s = StreamIFSlave(sim=sim, bitwidth=32)
    r = s.as_dir('R')
    # A slave reads: get is reachable on the read view.
    assert r.get is not None
    assert r.get_pipelined is not None
    # ... and blocked on a write view.
    w = s.as_dir('W')
    with pytest.raises(AttributeError):
        _ = w.get


def test_stream_master_write_view(sim):
    m = StreamIFMaster(sim=sim, bitwidth=32)
    w = m.as_dir('W')
    assert w.write is not None
    assert w.write_pipelined is not None
    # A read view blocks the write.
    r = m.as_dir('R')
    with pytest.raises(AttributeError):
        _ = r.write


# ---------------------------------------------------------------------------
# Schema transfer endpoints
# ---------------------------------------------------------------------------

def test_schema_transfer_endpoints(sim):
    s = SchemaTransferIFSlave(sim=sim, schema_type=Word32, bitwidth=32)
    assert s.as_dir('R').get is not None
    with pytest.raises(AttributeError):
        _ = s.as_dir('W').get

    m = SchemaTransferIFMaster(sim=sim, bitwidth=32)
    assert m.as_dir('W').write is not None
    with pytest.raises(AttributeError):
        _ = m.as_dir('R').write
