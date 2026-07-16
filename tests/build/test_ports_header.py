"""Tests for `render_ports_h` — the testbench port binding derived from the top's own TopSpec.

The point of this header is a *negative* property: a TB cannot name a port the kernel does not have,
because both come from one spec. These tests pin the derivation rules that make that true, and the
one that makes it useful (the pin-low set must be the complement of what the BFM drives — pinning a
port the TB is supposed to drive would deadlock the run).
"""
from __future__ import annotations

from waveflow.build.composite_gen import (
    TopSpec,
    _axis_port,
    _maxi_port,
    render_ports_h,
)


def _spec(top="k", ports=None) -> TopSpec:
    return TopSpec(top_name=top, ports=tuple(ports or ()), tasks=(), cmd_headers=())


def test_axis_keeps_its_name_maxi_is_named_after_its_bundle():
    """The asymmetry a hand-written TB gets wrong once and then carries forever."""
    spec = _spec(ports=[
        _axis_port("s_cmd", 64, kind="axis_in"),
        _maxi_port("m_in", 64, const=True, bundle="gmem0"),
        _maxi_port("m_out", 64, const=False, bundle="gmem1"),
    ])
    h = render_ports_h(spec)
    assert 'const s_cmd    = "s_cmd";' in h
    # named after the BUNDLE, not the port: m_in lives on gmem0.
    assert 'const m_in     = "m_axi_gmem0";' in h
    assert 'const m_out    = "m_axi_gmem1";' in h
    assert 'const DESIGN_DLL = "xsim.dir/k/xsimk.dll";' in h


def test_binding_tracks_the_spec():
    """The drift claim: rename the bundle in Python and the TB's binding follows, with no edit.

    This is the whole reason the header exists. Before, a renamed bundle left the TB compiling
    happily against a port that no longer existed — and the failure surfaced as a hang.
    """
    before = render_ports_h(_spec(ports=[_maxi_port("m_in", 64, const=True, bundle="gmem0")]))
    after = render_ports_h(_spec(ports=[_maxi_port("m_in", 64, const=True, bundle="gmem7")]))
    assert 'const m_in     = "m_axi_gmem0";' in before
    assert 'const m_in     = "m_axi_gmem7";' in after
    assert "m_axi_gmem0" not in after and "m_axi_gmem7" not in before


def test_pin_low_set_is_the_complement_of_what_the_bfm_drives():
    """ZERO_PORTS must never contain a port the TB's own model drives — that would deadlock it.

    For a READ bundle the BFM owns ARREADY/R*; for a WRITE bundle it owns AWREADY/WREADY/B*. Each
    kind pins only the other side.
    """
    read_h = render_ports_h(_spec(ports=[_maxi_port("m_in", 64, const=True, bundle="gmem0")]))
    for driven in ("m_axi_gmem0_ARREADY", "m_axi_gmem0_RVALID", "m_axi_gmem0_RDATA",
                   "m_axi_gmem0_RLAST"):
        assert f'"{driven}"' not in read_h, f"{driven} is driven by AxiMmReadSlave; pinning it hangs"
    for pinned in ("m_axi_gmem0_AWREADY", "m_axi_gmem0_WREADY", "m_axi_gmem0_BVALID"):
        assert f'"{pinned}"' in read_h

    write_h = render_ports_h(_spec(ports=[_maxi_port("m_out", 64, const=False, bundle="gmem1")]))
    for driven in ("m_axi_gmem1_AWREADY", "m_axi_gmem1_WREADY", "m_axi_gmem1_BVALID"):
        assert f'"{driven}"' not in write_h, f"{driven} is driven by AxiMmWriteSlave; pinning it hangs"
    for pinned in ("m_axi_gmem1_ARREADY", "m_axi_gmem1_RVALID", "m_axi_gmem1_RDATA"):
        assert f'"{pinned}"' in write_h


def test_control_slave_pinned_only_when_an_maxi_exists():
    """The s_axi_control slave exists because of `offset=slave` on an m_axi port. Pinning it
    quiescent is what makes every offset register read 0 — i.e. element coords == byte addr / BPW."""
    with_maxi = render_ports_h(_spec(ports=[_maxi_port("m_in", 64, const=True, bundle="gmem0")]))
    assert '"s_axi_control_AWVALID"' in with_maxi

    stream_only = render_ports_h(_spec(ports=[_axis_port("s_cmd", 64, kind="axis_in")]))
    assert "s_axi_control" not in stream_only, "a stream-only kernel has no control slave to pin"


def test_generated_header_is_ascii():
    """Generated C++ must be ASCII: written on a cp1252 host and fed to mingw g++."""
    spec = _spec(ports=[
        _axis_port("s_cmd", 64, kind="axis_in"),
        _maxi_port("m_in", 64, const=True, bundle="gmem0"),
    ])
    assert render_ports_h(spec).isascii()


def test_mem_copy_binding_matches_its_real_boundary():
    """End-to-end against the real composite: the binding is derived from the same graph walk that
    emits the top's pragmas, so the two cannot disagree."""
    from waveflow.build.composite_gen import composite_top_spec, render_top
    from waveflow.build.elaborate import elaborate
    from examples.interleaver.mem_copy import MemCopy

    comp = elaborate(MemCopy, {"mem_dwidth": 64}, name="mem_copy")
    spec = composite_top_spec(comp, width=64)
    h, top = render_ports_h(spec), render_top(spec)

    # every m_axi bundle the top declares a pragma for is bound in the header, by bundle name
    assert "bundle=gmem0" in top and '"m_axi_gmem0"' in h
    assert "bundle=gmem1" in top and '"m_axi_gmem1"' in h
    # every axis port the top declares is bound by its own name
    for axis in ("s_cmd", "s_done"):
        assert f"#pragma HLS INTERFACE axis port={axis}" in top and f'"{axis}"' in h
