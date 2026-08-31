"""A boundary AXI-Stream port that carries TLAST — the pin, the wrapper wire, and what it costs.

``plans/rf_shot_buf.md`` Stage B needs one fact the free-running composite flow could not express:
**a frame boundary the kernel can act on.**  Every composite boundary stream lowered to
``hls::stream<ap_uint<W> >``, which has no TLAST pin at all, so a host sending fewer payload words
than its header declared produced a *hang* rather than a verdict — and a body that cannot see the end
of a frame cannot be written, which would have made the pysim and C++ twins different designs.

:class:`~waveflow.hw.interface.FramedStreamIFSlave` is the opt-in.  What this module pins is that it
IS an opt-in: the ports that do not ask are byte-identical to what they were, which is the only thing
that lets nine RTL-gated designs keep their recorded cycle counts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import pytest

from waveflow.build.composite_gen import composite_top_spec, render_top
from waveflow.build.elaborate import elaborate, structure_signature
from waveflow.build.wrapper_gen import _axis_sigs
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    FramedStreamIFSlave,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.hw.mem_stream import KernelTask
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation


@dataclass
class _Relay(FreeRunMod):
    """One word in, one word out — the smallest design that can have a boundary port."""

    cpp_kernel_name: ClassVar[str | None] = "relay"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: HwParam[int] = 64
    #: Which endpoint classes the two ports are built from — the whole subject of this module.
    framed: bool = False
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        sl = FramedStreamIFSlave if self.framed else StreamIFSlave
        ma = FramedStreamIFMaster if self.framed else StreamIFMaster
        self.s_in = sl(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w, has_tlast=True)
        self.s_out = ma(sim=self.sim, name=f"{self.name}_s_out", bitwidth=w, has_tlast=True)
        for ep in (self.s_in, self.s_out):
            self.add_endpoint(ep)
        self.boundary = ["s_in", "s_out"]

    def kernel_task(self) -> KernelTask:
        return KernelTask("relay_task", "relay_task.h", ("s_in", "s_out"),
                          template_args=(int(self.bitwidth),))

    def run_iter(self) -> ProcessGen[None]:  # pragma: no cover - never simulated here
        words = yield from self.s_in.get(nwords_max=1)
        yield from self.s_out.write(words)


def _spec(framed: bool):
    return composite_top_spec(elaborate(_Relay, {"bitwidth": 64, "framed": framed}), width=64)


# ---------------------------------------------------------------------------
# The port type, which is the only thing that decides whether Vitis emits a pin
# ---------------------------------------------------------------------------

def test_an_unframed_port_is_a_plain_word_stream():
    """The default, and it must not move: nine RTL-gated designs are built on this decl."""
    ports = _spec(False).ports
    assert [p.decl for p in ports] == ["hls::stream<ap_uint<64> >& s_in",
                                       "hls::stream<ap_uint<64> >& s_out"]
    assert not any(p.axi4s for p in ports)


def test_a_framed_port_is_an_ap_axis_stream():
    """``ap_axis``, not the plain ``{data, last}`` ``framed_word`` — and that is a MEASUREMENT.

    A ``framed_word`` boundary port compiles, and Vitis packs the whole struct into one wide TDATA:
    at ``W=64`` the port came out ``[127:0] s_in_TDATA`` with **no TLAST anywhere**, and the wrapper
    then failed to elaborate against a pin that was never emitted.  The side channels are a property
    of ``ap_axis``, not of having a ``last`` member.  ``framed_word`` stays right for an *internal*
    channel, where ``ap_axis`` is refused outright (HLS 214-208).
    """
    ports = _spec(True).ports
    assert [p.decl for p in ports] == ["hls::stream<streamutils::axi4s_word<64> >& s_in",
                                       "hls::stream<streamutils::axi4s_word<64> >& s_out"]
    assert all(p.axi4s for p in ports)


def test_the_pragma_is_the_same_either_way():
    """``axis`` describes the PROTOCOL; the C++ type describes the beat.

    Worth pinning because the natural guess is that a TLAST pin is asked for in the pragma.  It is
    not — a design that changed the pragma and left the type alone would get no pin and no
    diagnostic.
    """
    for framed in (False, True):
        assert [p.pragmas for p in _spec(framed).ports] == [
            ("#pragma HLS INTERFACE axis port=s_in",),
            ("#pragma HLS INTERFACE axis port=s_out",)]


def test_the_framed_top_includes_the_header_that_defines_framed_word():
    """Derived from the ports, never remembered by the design.

    ``streamutils::framed_word`` IS the port's type, so a top that named it without including
    ``streamutils_hls.h`` would fail in Vitis — a long way from the line that chose the endpoint
    class.
    """
    assert '#include "streamutils_hls.h"' in render_top(_spec(True))
    assert '#include "streamutils_hls.h"' not in render_top(_spec(False))


# ---------------------------------------------------------------------------
# The wrapper, which has to declare the same pins the kernel has
# ---------------------------------------------------------------------------

def test_the_wrapper_adds_the_side_channels_only_for_an_axi4s_port():
    """A wrapper naming a pin the kernel instance does not have does not elaborate — so this is not
    a cosmetic difference, it is the difference between a design that runs and one that does not.

    All three side channels, not TLAST alone: ``ap_axis`` brings TKEEP and TSTRB with it whether the
    design reads them or not, and a wrapper that passed only the pin it cared about would leave two
    unbound on the kernel instance.
    """
    plain, full = _spec(False).ports[0], _spec(True).ports[0]
    assert [s for s, _d, _w in _axis_sigs(plain)] == ["TDATA", "TVALID", "TREADY"]
    assert [s for s, _d, _w in _axis_sigs(full)] == [
        "TDATA", "TVALID", "TREADY", "TKEEP", "TSTRB", "TLAST"]


def test_tkeep_and_tstrb_are_one_bit_per_byte():
    """Byte-granular, per AXI4-Stream — a 64-bit payload has 8-bit qualifiers.

    Pinned because the natural guess is "as wide as TDATA", and a wrapper wire of the wrong width is
    the defect ``_bram_addr_shift`` was written for in the other direction: it elaborates, and the
    truncation is silent.
    """
    widths = dict((s, w) for s, _d, w in _axis_sigs(_spec(True).ports[0]))
    assert widths["TKEEP"] == 8 and widths["TSTRB"] == 8
    assert widths["TLAST"] is False          # one bit
    assert widths["TDATA"] is True           # the payload width


def test_tlast_travels_with_tdata_not_with_the_handshake():
    """TLAST is payload: an *input* stream's TLAST is an input, an output stream's is an output.

    Derived from the kind's TDATA row rather than listed, so the two cannot be given opposite
    directions by a table edited on one line.
    """
    in_p, out_p = _spec(True).ports
    assert dict((s, d) for s, d, _w in _axis_sigs(in_p))["TLAST"] == "input"
    assert dict((s, d) for s, d, _w in _axis_sigs(out_p))["TLAST"] == "output"


# ---------------------------------------------------------------------------
# What the opt-in costs everyone who does not take it
# ---------------------------------------------------------------------------

def test_framing_is_a_subclass_so_existing_signatures_do_not_move():
    """**The reason it is a subclass and not a field.**

    An endpoint's attribute set is part of ``structure_signature``, which is what every calibration
    key is derived from — so a per-instance ``boundary_tlast`` flag would have moved every stored
    measurement in the repo the moment it was added (``tests/calib/test_key_stability.py`` is the
    checker that said so, loudly).  A ``ClassVar`` on a subclass is invisible to the signature except
    through the endpoint's own class name, so exactly the designs that ask for a pin change — and
    they change because they really are different hardware.
    """
    plain = structure_signature(elaborate(_Relay, {"bitwidth": 64, "framed": False}))
    framed = structure_signature(elaborate(_Relay, {"bitwidth": 64, "framed": True}))
    assert plain != framed, "a TLAST pin is a different port; the signature must say so"
    assert StreamIFSlave.boundary_tlast is False
    assert FramedStreamIFSlave.boundary_tlast is True
    ep = FramedStreamIFSlave(sim=Simulation(), name="probe", bitwidth=64)
    assert "boundary_tlast" not in vars(ep), (
        "boundary_tlast must stay a ClassVar: an instance attribute lands in __dict__ and therefore "
        "in every module key in the repo")


@pytest.mark.parametrize("cls,base", [(FramedStreamIFSlave, StreamIFSlave),
                                      (FramedStreamIFMaster, StreamIFMaster)])
def test_the_framed_pair_changes_nothing_but_the_framing(cls, base):
    """Same direction, same protocol, same pysim behaviour — only the RTL beat differs."""
    assert issubclass(cls, base)
    assert cls.boundary_kind == base.boundary_kind
