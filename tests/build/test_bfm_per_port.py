"""``BfmModel`` per-port resolution — ``plans/adc_model.md``, "Stage 2's opening prerequisite".

Two gaps, both confirmed against the code before anything was written:

1. **One class cannot serve two boundary ports.** ``bfm_dual_class`` returns the participant's single
   declared class for AXI-Stream, so a converter's receive and transmit ports would get the *same*
   model — but they need ``RfdcAdcMaster`` and ``RfdcDacSlave``.
2. **A port's constructor contribution depends on which side of the cut its peer is.** A boundary
   endpoint contributes ``dut, "prefix"``; an edge endpoint contributes a channel variable. The
   boundary walk used to assume every port of a model was a boundary port.

The resolving shape is one declaration per *data path* rather than per module, with each port
resolved by its own kind. The ADC path is then **one object** binding RTL pins on one side and a
channel on the other — which is what a converter is.

``xsi_rfdc.h`` is the spec here, not this file: those two models were written first, precisely so the
constructor shapes would be facts rather than a guess. :func:`test_emitted_ctor_matches_the_header`
reads the signatures back out of the header and checks the emitter against them, so a change to
either side that breaks the pairing fails here rather than at ``g++`` time in a design nobody has
built yet.

**Scope.** These are synthetic fixtures on purpose. The real ``rf_loopback`` graph cannot be walked
yet — ``tb_top_spec`` needs ``dut.boundary``, and ``RfSampPassThrough`` is a ``FreeRunMod`` leaf whose
boundary derives from a ``kernel_task()`` signature that does not exist. Synthesizing it is the next
step, not this one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest

from waveflow.build.composite_gen import (
    BfmModel,
    bfm_models,
    render_tb_harness,
    tb_top_spec,
)
from waveflow.build.hwcodegen import LoweringError
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwModule
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.simulation.simulation import Simulation

from tests.build.test_behavioral_edges import (
    EdgeToyDut,
    TokenIF,
    TokenRx,
    TokenScoreboard,
    TokenTx,
)

_RFDC_H = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi" / "xsi_rfdc.h"


# ---------------------------------------------------------------------------
# Fixture 1 — the converter shape: two paths, each spanning both sides of the cut
# ---------------------------------------------------------------------------

@dataclass
class ToyConverter(HwModule):
    """The RFDC's shape in miniature: four endpoints, two data paths, two C++ objects.

    Each path binds an RTL port on the fabric side and a behavioral edge on the RF side — one
    object, not a boundary model glued to a separate channel peer.
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        # ADC path: RF blocks in (edge) -> AXI-Stream out to the fabric (boundary).
        self.rx_rf = TokenRx(name=f"{self.name}_rx_rf", sim=self.sim)
        self.rx_stream = StreamIFMaster(name=f"{self.name}_rx_stream", sim=self.sim,
                                        bitwidth=64, has_tlast=False)
        # DAC path: AXI-Stream in from the fabric (boundary) -> RF blocks out (edge).
        self.tx_stream = StreamIFSlave(name=f"{self.name}_tx_stream", sim=self.sim,
                                       bitwidth=64, has_tlast=False)
        self.tx_rf = TokenTx(name=f"{self.name}_tx_rf", sim=self.sim)
        for ep in (self.rx_rf, self.rx_stream, self.tx_stream, self.tx_rf):
            self.add_endpoint(ep)

    def bfm_model(self):
        # Port order IS constructor order, and it is the header's: (dut, prefix) then the channel,
        # then the literal extras.  See test_emitted_ctor_matches_the_header.
        return (
            BfmModel("RfdcAdcMaster", ports=("rx_stream", "rx_rf"),
                     extra_args=("RfdcFormat{16, 1, 4}", "0.8533333333")),
            BfmModel("RfdcDacSlave", ports=("tx_stream", "tx_rf"),
                     extra_args=("RfdcFormat{16, 1, 4}", "0.8533333333", "256")),
        )


@dataclass
class TokenEnvSource(TokenScoreboard):
    """The RF environment beyond the ADC edge: a producer, so that edge has an unclaimed side.

    Its presence is the point — it is what distinguishes "walk 2 skipped the converter" from
    "walk 2 emitted nothing".
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        self.tok_out_ep = TokenTx(name=f"{self.name}_tok_out_ep", sim=self.sim)
        self.add_endpoint(self.tok_out_ep)

    def bfm_model(self):
        return BfmModel("TokenSource", ports=("tok_out_ep",))


@dataclass
class ConverterTB(FreeRunMod):
    """DUT + one converter spanning both of its ports + an RF source and sink beyond the edges."""

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.dut = EdgeToyDut(name=f"{self.name}_dut", sim=self.sim)
        self.conv = ToyConverter(name=f"{self.name}_conv", sim=self.sim)
        self.env_src = TokenEnvSource(name=f"{self.name}_src", sim=self.sim)    # feeds the ADC edge
        self.env_snk = TokenScoreboard(name=f"{self.name}_snk", sim=self.sim)   # drains the DAC edge
        for c in (self.dut, self.conv, self.env_src, self.env_snk):
            self.add_comp(c)

        # Fabric side: the converter drives the DUT's input and answers its output.
        adc_axis = StreamIF(name=f"{self.name}_adc_axis", sim=self.sim, clk=self.clk, bitwidth=64)
        adc_axis.bind("master", self.conv.rx_stream)
        adc_axis.bind("slave", self.dut.s_in)
        self.add_if(adc_axis)

        dac_axis = StreamIF(name=f"{self.name}_dac_axis", sim=self.sim, clk=self.clk, bitwidth=64)
        dac_axis.bind("master", self.dut.m_out)
        dac_axis.bind("slave", self.conv.tx_stream)
        self.add_if(dac_axis)

        # RF side: two behavioral edges, neither touching the DUT.
        self.adc_rf = TokenIF(name=f"{self.name}_adc_rf", sim=self.sim, depth=4)
        self.adc_rf.bind("tx", self.env_src.tok_out_ep)
        self.adc_rf.bind("rx", self.conv.rx_rf)
        self.add_if(self.adc_rf)

        self.dac_rf = TokenIF(name=f"{self.name}_dac_rf", sim=self.sim, depth=4)
        self.dac_rf.bind("tx", self.conv.tx_rf)
        self.dac_rf.bind("rx", self.env_snk.tok_in)
        self.add_if(self.dac_rf)


def _conv_tb() -> ConverterTB:
    return ConverterTB(name="ctb", sim=Simulation())


# ---------------------------------------------------------------------------
# Fixture 2 — two models over disjoint boundary ports (gap 1 in isolation)
# ---------------------------------------------------------------------------

@dataclass
class TwoPortParticipant(HwModule):
    """One module answering **both** of the DUT's stream ports, with a different class on each.

    No behavioral edge anywhere: this isolates gap 1 (the class is per data path, not per module)
    from gap 2 (the constructor shape is per port).
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        self.drv = StreamIFMaster(name=f"{self.name}_drv", sim=self.sim, bitwidth=64,
                                  has_tlast=False)
        self.cap = StreamIFSlave(name=f"{self.name}_cap", sim=self.sim, bitwidth=64,
                                 has_tlast=False)
        self.add_endpoint(self.drv)
        self.add_endpoint(self.cap)

    def bfm_model(self):
        return (BfmModel("AxisMaster", ports=("drv",), extra_args=("{}",)),
                BfmModel("AxisSlave", ports=("cap",)))


@dataclass
class TwoPortTB(FreeRunMod):
    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: Name a port on the second model that is wired to nothing — the negative case.
    stray_port: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        self.dut = EdgeToyDut(name=f"{self.name}_dut", sim=self.sim)
        self.part = TwoPortParticipant(name=f"{self.name}_part", sim=self.sim)
        if self.stray_port:
            stray = StreamIFSlave(name=f"{self.name}_stray", sim=self.sim, bitwidth=64,
                                  has_tlast=False)
            self.part.add_endpoint(stray)
            self.part.stray = stray
            self.part.bfm_model = lambda: (
                BfmModel("AxisMaster", ports=("drv",), extra_args=("{}",)),
                BfmModel("AxisSlave", ports=("cap", "stray")),
            )
        for c in (self.dut, self.part):
            self.add_comp(c)

        in_if = StreamIF(name=f"{self.name}_in_if", sim=self.sim, clk=self.clk, bitwidth=64)
        in_if.bind("master", self.part.drv)
        in_if.bind("slave", self.dut.s_in)
        self.add_if(in_if)

        out_if = StreamIF(name=f"{self.name}_out_if", sim=self.sim, clk=self.clk, bitwidth=64)
        out_if.bind("master", self.dut.m_out)
        out_if.bind("slave", self.part.cap)
        self.add_if(out_if)


# ---------------------------------------------------------------------------
# The declaration itself
# ---------------------------------------------------------------------------

class TestDeclaration:

    def test_one_model_still_normalizes_to_a_tuple(self):
        """Back-compatible by construction: the single-model form is untouched."""
        from waveflow.simulation.stream_tb import StreamDriver

        d = StreamDriver(name="d", sim=Simulation(), in_bundle="v")
        got = bfm_models(d)
        assert len(got) == 1 and got[0].cls == "AxisMaster"

    def test_several_models_come_back_in_declaration_order(self):
        conv = ToyConverter(name="c", sim=Simulation())
        assert [m.cls for m in bfm_models(conv)] == ["RfdcAdcMaster", "RfdcDacSlave"]

    def test_an_empty_declaration_is_refused(self):
        mod = ToyConverter(name="c", sim=Simulation())
        mod.bfm_model = lambda: ()
        with pytest.raises(LoweringError, match="returned no models"):
            bfm_models(mod)

    def test_a_non_model_is_refused(self):
        mod = ToyConverter(name="c", sim=Simulation())
        mod.bfm_model = lambda: ("AxisMaster",)
        with pytest.raises(LoweringError, match="not a BfmModel"):
            bfm_models(mod)


# ---------------------------------------------------------------------------
# Gap 1 — one class cannot serve two boundary ports
# ---------------------------------------------------------------------------

class TestTwoModelsOverTwoBoundaryPorts:

    def test_each_boundary_port_gets_its_own_class(self):
        spec = tb_top_spec(TwoPortTB(name="tp", sim=Simulation()))
        got = {m.name: m.cls for m in spec.models}
        assert got == {"s_in": "AxisMaster", "m_out": "AxisSlave"}, (
            "one declaration per module would have put the same class on both ports")

    def test_each_model_keeps_its_own_extra_args(self):
        spec = tb_top_spec(TwoPortTB(name="tp", sim=Simulation()))
        by = {m.name: m.args for m in spec.models}
        assert by["s_in"] == ("{}",) and by["m_out"] == ()

    def test_the_ctor_is_the_ordinary_boundary_one(self):
        """A model whose ports are all boundary ports resolves exactly as it always did."""
        spec = tb_top_spec(TwoPortTB(name="tp", sim=Simulation()))
        by = {m.name: m.binds for m in spec.models}
        assert by["s_in"] == ("sim.dut()", "edge_toy_ports::s_in")
        assert by["m_out"] == ("sim.dut()", "edge_toy_ports::m_out")

    def test_a_port_wired_to_nothing_fails_loudly(self):
        """The negative: a named port that is neither a boundary port nor on a behavioral edge.

        There is no argument the generator could write for it, and guessing one would bind the
        wrong object or not compile.
        """
        with pytest.raises(LoweringError, match="neither wired to a DUT boundary port nor bound"):
            tb_top_spec(TwoPortTB(name="tp", sim=Simulation(), stray_port=True))


# ---------------------------------------------------------------------------
# Gap 2 — a model spanning both sides of the cut
# ---------------------------------------------------------------------------

class TestSpanningTheCut:

    def test_one_object_binds_a_dut_port_and_a_channel(self):
        spec = tb_top_spec(_conv_tb())
        by = {m.name: m for m in spec.models}

        adc = by["s_in"]
        assert adc.cls == "RfdcAdcMaster"
        assert adc.binds == ("sim.dut()", "edge_toy_ports::s_in", "ctb_adc_rf")
        assert adc.args == ("RfdcFormat{16, 1, 4}", "0.8533333333")
        assert adc.channel == "ctb_adc_rf"

        dac = by["m_out"]
        assert dac.cls == "RfdcDacSlave"
        assert dac.binds == ("sim.dut()", "edge_toy_ports::m_out", "ctb_dac_rf")
        assert dac.args == ("RfdcFormat{16, 1, 4}", "0.8533333333", "256")

    def test_the_edge_still_emits_its_channel(self):
        """The channel exists because the edge does, whoever binds it."""
        spec = tb_top_spec(_conv_tb())
        assert {c.name for c in spec.channels} == {"ctb_adc_rf", "ctb_dac_rf"}

    def test_a_spanning_model_is_not_also_emitted_as_a_channel_peer(self):
        """The claim the plan makes: walk 2 must SKIP a module already claimed that way.

        Emitting a peer as well would construct a second object against the same edge, and the two
        would disagree about what crossed it.
        """
        spec = tb_top_spec(_conv_tb())
        names = {m.name for m in spec.models}
        # The converter sits on the ADC edge's `rx` side and the DAC edge's `tx` side. Those are the
        # two a peer would have been emitted for; the *other* two sides are the environment and must
        # still get one (see the next test).
        assert "ctb_adc_rf_rx" not in names, f"a peer was emitted for the ADC path: {names}"
        assert "ctb_dac_rf_tx" not in names, f"a peer was emitted for the DAC path: {names}"
        # Four models: the two converter paths, and the two RF-environment peers beyond the edges.
        assert len(spec.models) == 4, names

    def test_the_unclaimed_side_of_each_edge_still_gets_a_peer(self):
        """Only the claimed side is skipped — the environment beyond the edge is still a node."""
        spec = tb_top_spec(_conv_tb())
        peers = {m.name for m in spec.models if m.channel and m.binds == (m.channel,)}
        assert peers == {"ctb_adc_rf_tx", "ctb_dac_rf_rx"}

    def test_the_harness_declares_the_channel_before_the_model_that_binds_it(self):
        """Declaration order is construction order: a model cannot take a reference to a channel
        declared after it."""
        text = render_tb_harness(tb_top_spec(_conv_tb()))
        assert text.index(" ctb_adc_rf;") < text.index("RfdcAdcMaster s_in;")
        assert text.index(" ctb_dac_rf;") < text.index("RfdcDacSlave m_out;")

    def test_the_emitted_constructions_are_the_headers(self):
        text = render_tb_harness(tb_top_spec(_conv_tb()))
        assert ("s_in(sim.dut(), edge_toy_ports::s_in, ctb_adc_rf, RfdcFormat{16, 1, 4}, 0.8533333333)" in text)
        assert ("m_out(sim.dut(), edge_toy_ports::m_out, ctb_dac_rf, RfdcFormat{16, 1, 4}, 0.8533333333, 256)" in text)


# ---------------------------------------------------------------------------
# The header is the spec
# ---------------------------------------------------------------------------

def _ctor_params(cls_name: str) -> list[str]:
    """The declared constructor parameter *types* of *cls_name*, read from ``xsi_rfdc.h``."""
    text = _RFDC_H.read_text(encoding="utf-8")
    m = re.search(rf"^\s*{cls_name}\((.*?)\)\s*$", text, re.M | re.S)
    assert m, f"no {cls_name}(...) constructor found in xsi_rfdc.h"
    params = [p.strip() for p in m.group(1).split(",")]
    # "Dut& dut" -> "Dut&"; "const RfdcFormat& fmt" -> "const RfdcFormat&"
    return [re.sub(r"\s+\w+$", "", p) for p in params]


class TestEmittedCtorMatchesTheHeader:
    """``xsi_rfdc.h`` states the contract; the emitter must satisfy it, not the other way round."""

    @pytest.mark.parametrize("cls,port,chan,n_extra", [
        ("RfdcAdcMaster", "s_in", "ctb_adc_rf", 2),
        ("RfdcDacSlave", "m_out", "ctb_dac_rf", 3),
    ])
    def test_arity_and_order(self, cls, port, chan, n_extra):
        params = _ctor_params(cls)
        # The header's shape: a Dut, the AXIS port LIST, the RF channel, then the model's own config.
        #
        # `AxisPortList` and not `const char*` since the converter became a tile: one model spans
        # every AXIS port of its direction, because the RF edge behind them carries every channel in
        # one block and n_ch models cannot each own it.  A bare port name still binds -- the list is
        # implicitly constructible from one -- which is why the arity is unchanged.
        assert params[0] == "Dut&"
        assert params[1] == "AxisPortList"
        assert params[2] == "RfChannel&"
        assert len(params) == 3 + n_extra

        m = {i.name: i for i in tb_top_spec(_conv_tb()).models}[port]
        emitted = tuple(m.binds) + m.args
        assert len(emitted) == len(params), (
            f"{cls} takes {len(params)} arguments but the harness would pass {len(emitted)}: "
            f"{emitted}")
        # Position by position, against what the header asks for.
        assert emitted[0] == "sim.dut()"                    # Dut&
        assert emitted[1] == f"edge_toy_ports::{port}"      # const char*
        assert emitted[2] == chan                           # RfChannel&

    def test_the_two_paths_take_different_classes(self):
        """The gap-1 claim, restated against the header: these are two classes, not one."""
        assert _ctor_params("RfdcAdcMaster") != _ctor_params("RfdcDacSlave")


# ---------------------------------------------------------------------------
# Back-compatibility
# ---------------------------------------------------------------------------

class TestExistingDesignsUnchanged:
    """Every design that predates this generalization must emit the same bytes."""

    def _committed(self):
        from examples.interleaver.interleaver_inband import make_xsi_tb as il
        from examples.mem_copy.mem_copy import make_xsi_tb as mc
        from examples.state_toy.state_toy import StateAccumTB

        return [("mem_copy", mc()), ("interleaver_inband", il()),
                ("state_accum", StateAccumTB(name="tb", sim=Simulation(), nvec=5, n_cycles=400))]

    def test_the_committed_harnesses_regenerate_identically(self):
        repo = Path(__file__).resolve().parents[2]
        checked = 0
        for name, tb in self._committed():
            spec = tb_top_spec(tb)
            hits = list(repo.glob(f"examples/*/xsi/{spec.top_name}_tb_harness.h"))
            if not hits:
                continue
            got = render_tb_harness(spec).replace("\r\n", "\n")
            want = hits[0].read_text(encoding="utf-8").replace("\r\n", "\n")
            assert got == want, f"{hits[0]} would change ({name})"
            checked += 1
        assert checked >= 2, f"only {checked} committed harnesses were compared"

    def test_a_single_model_over_boundary_ports_resolves_the_old_way(self):
        """The shape every existing design has: binds is exactly what the renderer used to derive."""
        from examples.mem_copy.mem_copy import make_xsi_tb

        spec = tb_top_spec(make_xsi_tb())
        ns = f"{spec.top_name}_ports"
        for m in spec.models:
            assert m.binds == ("sim.dut()", f"{ns}::{m.name}")
            assert m.channel is None
