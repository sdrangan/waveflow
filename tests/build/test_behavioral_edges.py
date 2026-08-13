"""Behavioral edges — an ``Interface`` that carries behaviour in both backends.

``plans/behavioral_edges.md`` S1 (the ``xsi_model()`` hook) and S3 (the second walk in
``tb_top_spec``).  S2 — the C++ primitive itself — is gated separately in ``test_xsi_channel.py``,
because it compiles and runs with a plain ``g++`` and nothing here needs a toolchain either.

The invariant all of this exists to protect is stated twice in ``plans/xsi_tb_codegen.md``:

    "If those were participants, the pysim graph and the XSI graph would have different nodes and
     'one statement, two backends' breaks on the first example."

Before this, an edge with no DUT port on either end emitted **nothing** — it was not rejected, it was
invisible, and the temptation was to collapse its far peer into a file read by the neighbouring
model.  That is the invariant violation.  The second walk removes the temptation: the edge becomes a
channel, and both peers stay nodes.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
import simpy

from waveflow.build.composite_gen import (
    BfmModel,
    ChannelModel,
    render_tb_harness,
    resolve_channel_model,
    tb_top_spec,
    xsi_channel_classes,
)
from waveflow.build.hwcodegen import LoweringError
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import DynParam, HwModule, HwParam, declares_hook
from waveflow.hw.interface import Interface, InterfaceEndpoint, StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink

_GXX = shutil.which("g++")
_XSI_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"


# ---------------------------------------------------------------------------
# The fixture: a minimal behavioral edge and its two peers
# ---------------------------------------------------------------------------
#
# Deliberately not RFSampIF.  That edge is the motivating case, but its channel model is
# `plans/adc_model.md` stage 2 — writing it here would be building stage 2's deliverable to test
# stage 1's mechanism, and the mechanism is what these tests are for.  This fixture is the other
# clause of the deferred note this plan promotes: a monitor feeding a scoreboard, with no RTL in
# between and a real queue that must exist.

@dataclass
class TokenTx(InterfaceEndpoint):
    """Producer side of a :class:`TokenIF`."""
    type_name = 'token_if_tx'

    def put(self, tok: int) -> ProcessGen[None]:
        yield from self.interface.put(tok)


@dataclass
class TokenRx(InterfaceEndpoint):
    """Consumer side of a :class:`TokenIF`."""
    type_name = 'token_if_rx'

    def get(self) -> ProcessGen[int]:
        tok = yield from self.interface.get()
        return tok


@dataclass
class TokenIF(Interface):
    """A bounded token channel between two testbench models — the minimal behavioral edge.

    Transport only: a depth, a queue, and a drop count.  Nothing that could be called processing.
    """

    depth: int = 4
    #: Init-time config, to prove a channel's DynParams are emitted the way a model's are.
    label: DynParam[str] = ""
    type_name = 'token_if'

    def __post_init__(self) -> None:
        self.endpoint_names = ('tx', 'rx')
        super().__post_init__()
        self.q = simpy.Store(self.env, capacity=int(self.depth))
        self.dropped = 0

    def put(self, tok) -> ProcessGen[None]:
        if len(self.q.items) >= self.q.capacity:
            self.dropped += 1
            yield self.timeout(0)
            return
        yield self.q.put(tok)

    def get(self) -> ProcessGen[int]:
        tok = yield self.q.get()
        return tok

    def xsi_model(self) -> ChannelModel:
        return ChannelModel("BlockChannel<uint64_t>", peers=("tx", "rx"),
                            extra_args=(str(int(self.depth)),))


@dataclass
class TokenMonitor(HwModule):
    """Emits a token per firing onto a :class:`TokenIF` — the "monitor" half."""

    n_tok: int = 3

    def __post_init__(self) -> None:
        super().__post_init__()
        self.tok_out = TokenTx(name=f"{self.name}_tok_out", sim=self.sim)
        self.add_endpoint(self.tok_out)

    def run_proc(self) -> ProcessGen[None]:
        for k in range(int(self.n_tok)):
            yield from self.tok_out.put(k)

    def bfm_model(self) -> BfmModel:
        return BfmModel("TokenMonitor", ports=("tok_out",))


@dataclass
class TokenScoreboard(HwModule):
    """Collects tokens off a :class:`TokenIF` — the "scoreboard" half."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.seen: list[int] = []
        self.tok_in = TokenRx(name=f"{self.name}_tok_in", sim=self.sim)
        self.add_endpoint(self.tok_in)

    def run_proc(self) -> ProcessGen[None]:
        while True:
            self.seen.append((yield from self.tok_in.get()))

    def bfm_model(self) -> BfmModel:
        return BfmModel("TokenScoreboard", ports=("tok_in",))


@dataclass
class EdgeToyDut(FreeRunMod):
    """A trivial pass-through DUT, so the graph has a cut at all."""

    cpp_kernel_name: ClassVar[str | None] = "edge_toy"
    bitwidth: HwParam[int] = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=w,
                                  has_tlast=False)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.m_out)

    def run_iter(self) -> ProcessGen[None]:
        words = yield from self.s_in.get(nwords_max=4)
        yield from self.m_out.write(words)


@dataclass
class EdgeToyTB(FreeRunMod):
    """DUT + two boundary edges + one **behavioral** edge that never touches the DUT."""

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: Build the behavioral edge at all (``False`` gives a graph of the old, boundary-only shape).
    with_edge: bool = True
    #: The edge's ``DynParam``.  Non-empty exercises the emission path; the compile fixture clears it,
    #: because a DynParam has to land on a **real C++ member** and ``BlockChannel`` has no ``label``.
    #: That is not a generator gap — it is the same obligation a model's DynParams carry, and the
    #: same one nothing static can check on either side.
    edge_label: str = "mon"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.dut = EdgeToyDut(name=f"{self.name}_dut", sim=self.sim)
        self.driver = StreamDriver(sim=self.sim, name=f"{self.name}_drv", bitwidth=64,
                                   in_bundle="vectors/s_in")
        self.sink = StreamSink(sim=self.sim, name=f"{self.name}_snk", bitwidth=64)
        self.monitor = TokenMonitor(name=f"{self.name}_mon", sim=self.sim)
        self.board = TokenScoreboard(name=f"{self.name}_sb", sim=self.sim)
        for c in (self.dut, self.driver, self.sink, self.monitor, self.board):
            self.add_comp(c)

        in_if = StreamIF(name=f"{self.name}_in_if", sim=self.sim, clk=self.clk, bitwidth=64)
        in_if.bind("master", self.driver.stream_ep)
        in_if.bind("slave", self.dut.s_in)
        self.add_if(in_if)

        out_if = StreamIF(name=f"{self.name}_out_if", sim=self.sim, clk=self.clk, bitwidth=64)
        out_if.bind("master", self.dut.m_out)
        out_if.bind("slave", self.sink.stream_ep)
        self.add_if(out_if)

        if self.with_edge:
            self.tok_if = TokenIF(name=f"{self.name}_tok_if", sim=self.sim, depth=4,
                                  label=self.edge_label)
            self.tok_if.bind("tx", self.monitor.tok_out)
            self.tok_if.bind("rx", self.board.tok_in)
            self.add_if(self.tok_if)


def _tb(**kw) -> EdgeToyTB:
    return EdgeToyTB(name="tb", sim=Simulation(), **kw)


# ---------------------------------------------------------------------------
# S1 — the hook
# ---------------------------------------------------------------------------

class TestHook:

    def test_the_base_interface_declares_no_model(self):
        """The sentinel: overriding IS the declaration, and the base must not read as one."""
        sim = Simulation()
        plain = StreamIF(name="plain", sim=sim, clk=Clock(freq=1e8), bitwidth=32)
        assert declares_hook(plain, "xsi_model") is False
        with pytest.raises(NotImplementedError, match="declares no xsi_model"):
            plain.xsi_model()

    def test_declares_hook_is_not_hasattr(self):
        """The trap this predicate exists to close, one level up from where it was first found.

        ``hasattr`` is ``True`` for *every* interface the moment ``Interface.xsi_model`` exists — so
        an emitter probing with it would treat every ``StreamIF`` in every design as a behavioral
        edge. The sentinel must be resolved against ``Interface``, not ``HwModule``: comparing
        against ``HwModule`` (which has no ``xsi_model`` at all) also answers ``True`` for
        everything, which is the same bug wearing a different hat.
        """
        sim = Simulation()
        plain = StreamIF(name="plain", sim=sim, clk=Clock(freq=1e8), bitwidth=32)
        assert hasattr(plain, "xsi_model") is True          # ...which is why hasattr is useless
        assert declares_hook(plain, "xsi_model") is False
        assert declares_hook(_tb().tok_if, "xsi_model") is True

    def test_the_node_hook_is_unaffected(self):
        """``declares_hook`` gained a base-resolution step; the module side must be untouched."""
        assert declares_hook(StreamDriver, "bfm_model") is True
        assert declares_hook(EdgeToyDut, "bfm_model") is False
        assert declares_hook(EdgeToyDut, "kernel_task") is True     # no sentinel => present == declared

    def test_resolve_returns_the_declared_record(self):
        cm = resolve_channel_model(_tb().tok_if)
        assert cm.cls == "BlockChannel<uint64_t>"
        assert cm.peers == ("tx", "rx")
        assert cm.extra_args == ("4",)

    def test_an_undeclared_edge_is_refused_with_the_reason(self):
        sim = Simulation()
        plain = StreamIF(name="plain", sim=sim, clk=Clock(freq=1e8), bitwidth=32)
        with pytest.raises(LoweringError, match="declares no xsi_model"):
            resolve_channel_model(plain)

    def test_a_channel_class_that_does_not_exist_is_refused(self):
        tb = _tb()
        tb.tok_if.xsi_model = lambda: ChannelModel("NoSuchChannel", peers=("tx", "rx"))
        with pytest.raises(LoweringError, match="not an XsiSimObj in xsi_channel.h"):
            resolve_channel_model(tb.tok_if)

    def test_the_registry_is_read_from_the_header(self):
        """A Python list of names would be a second copy of the library and would drift."""
        assert "BlockChannel" in xsi_channel_classes()

    def test_a_specialization_resolves_to_its_class(self):
        """``BlockChannel<Foo>`` is a *use*; the library defines ``BlockChannel``."""
        tb = _tb()
        tb.tok_if.xsi_model = lambda: ChannelModel("BlockChannel<SomeStruct>", peers=("tx", "rx"))
        assert resolve_channel_model(tb.tok_if).cls == "BlockChannel<SomeStruct>"

    def test_peers_must_name_this_interfaces_sides(self):
        tb = _tb()
        tb.tok_if.xsi_model = lambda: ChannelModel("BlockChannel<uint64_t>", peers=("tx", "slave"))
        with pytest.raises(LoweringError, match="not one of this interface's sides"):
            resolve_channel_model(tb.tok_if)

    def test_an_unbound_peer_is_refused(self):
        sim = Simulation()
        edge = TokenIF(name="lonely", sim=sim)
        edge.bind("tx", TokenTx(name="t", sim=sim))          # rx left unbound
        with pytest.raises(LoweringError, match="nothing is bound to it"):
            resolve_channel_model(edge)

    def test_exactly_two_peers(self):
        tb = _tb()
        tb.tok_if.xsi_model = lambda: ChannelModel("BlockChannel<uint64_t>", peers=("tx",))
        with pytest.raises(LoweringError, match="a channel connects exactly"):
            resolve_channel_model(tb.tok_if)

    def test_an_unrenderable_dynparam_is_refused(self):
        tb = _tb()
        tb.tok_if.label = object()                            # no C++ rendering
        with pytest.raises(LoweringError, match="cannot be rendered"):
            resolve_channel_model(tb.tok_if)


# ---------------------------------------------------------------------------
# S3 — the second walk
# ---------------------------------------------------------------------------

class TestSecondWalk:

    def test_the_edge_becomes_a_channel_and_two_peer_models(self):
        spec = tb_top_spec(_tb())

        assert len(spec.channels) == 1
        ch = spec.channels[0]
        assert (ch.cls, ch.name, ch.args) == ("BlockChannel<uint64_t>", "tb_tok_if", ("4",))
        assert ch.dyn_params == (("label", '"mon"'),)        # a channel's DynParams emit like a model's

        peers = [m for m in spec.models if m.channel]
        assert [(m.cls, m.name, m.channel) for m in peers] == [
            ("TokenMonitor", "tb_tok_if_tx", "tb_tok_if"),
            ("TokenScoreboard", "tb_tok_if_rx", "tb_tok_if"),
        ]
        assert all(m.xsi_prefix == "" for m in peers), "a peer model drives no RTL port"

    def test_the_boundary_walk_is_unchanged_beside_it(self):
        """The two walks partition the interfaces; neither claims the other's."""
        spec = tb_top_spec(_tb())
        boundary = [m for m in spec.models if not m.channel]
        assert {(m.cls, m.name) for m in boundary} == {
            ("AxisMaster", "s_in"), ("AxisSlave", "m_out")}

    def test_a_graph_with_no_behavioral_edge_emits_no_channel(self):
        spec = tb_top_spec(_tb(with_edge=False))
        assert spec.channels == ()
        assert all(m.channel is None for m in spec.models)

    def test_peers_are_still_nodes_in_both_backends(self):
        """The invariant: the XSI graph has the same nodes as the pysim graph.

        Five modules in Python, and at RTL the DUT plus four models — not three models and a file.
        """
        tb = _tb()
        spec = tb_top_spec(tb)
        assert len(tb.ordered_subcomps) == 5                 # dut + driver + sink + monitor + board
        assert len(spec.models) == 4                         # every non-DUT module has a model
        assert {m.name for m in spec.models} == {
            "s_in", "m_out", "tb_tok_if_tx", "tb_tok_if_rx"}


@dataclass
class PlainTokenIF(TokenIF):
    """A token edge that declares **no** ``xsi_model()`` — a pysim-only edge."""
    type_name = 'plain_token_if'
    xsi_model = Interface.xsi_model          # re-inherit the sentinel: this edge declares nothing


@dataclass
class PysimOnlyBoard(TokenScoreboard):
    """A scoreboard with no ``bfm_model()`` — a pysim-only node."""
    bfm_model = HwModule.bfm_model           # re-inherit the sentinel


def _tb_with_extra_edge(iface_cls=TokenIF, board_cls=TokenScoreboard, *, add_peers=True,
                        name="tb_extra_if"):
    """A graph whose behavioral edge is built from the given classes, for the refusal cases."""
    tb = _tb(with_edge=False)
    mon = TokenMonitor(name=f"{tb.name}_m2", sim=tb.sim)
    board = board_cls(name=f"{tb.name}_b2", sim=tb.sim)
    if add_peers:
        tb.add_comp(mon)
        tb.add_comp(board)
    edge = iface_cls(name=name, sim=tb.sim)
    edge.bind("tx", mon.tok_out)
    edge.bind("rx", board.tok_in)
    tb.add_if(edge)
    return tb


class TestSecondWalkRefusals:
    """An edge that reaches nowhere useful is an **error**, not a no-op — which is what it was."""

    def test_an_edge_with_no_model_is_refused_rather_than_ignored(self):
        """The headline change: before the second walk, this graph emitted nothing at all.

        Silently — the edge was not rejected, it was *invisible*, and the temptation was to collapse
        its far peer into a file read by the neighbouring model. That is the invariant violation.
        """
        with pytest.raises(LoweringError, match="declares no xsi_model"):
            tb_top_spec(_tb_with_extra_edge(iface_cls=PlainTokenIF))

    def test_an_edge_to_a_pysim_only_module_is_refused(self):
        """A graph containing a pysim-only node has no XSI form — a finding, not a defect."""
        with pytest.raises(LoweringError, match="declares no bfm_model"):
            tb_top_spec(_tb_with_extra_edge(board_cls=PysimOnlyBoard))

    def test_an_edge_whose_peer_is_not_a_testbench_child_is_refused(self):
        with pytest.raises(LoweringError, match="not a sub-component of this testbench"):
            tb_top_spec(_tb_with_extra_edge(add_peers=False))

    def test_an_edge_reaching_inside_the_dut_is_refused(self):
        """At RTL there is no such connection point: a TB edge meets a boundary port or it does not
        meet the DUT at all."""
        tb = _tb(with_edge=False)
        mon = TokenMonitor(name="m3", sim=tb.sim)
        tb.add_comp(mon)
        rx = TokenRx(name="dut_tok_in", sim=tb.sim)
        tb.dut.add_endpoint(rx)                       # an endpoint on the DUT, but not a boundary port
        edge = TokenIF(name="tb_inside_if", sim=tb.sim)
        edge.bind("tx", mon.tok_out)
        edge.bind("rx", rx)
        tb.add_if(edge)
        with pytest.raises(LoweringError, match="INSIDE the DUT"):
            tb_top_spec(tb)

    def test_an_edge_endpoint_no_declared_model_names_is_refused(self):
        """What replaced the old dual-role refusal, and it is narrower on purpose.

        A module spanning a boundary port *and* an edge is now legal (see
        ``TestSpanningTheCut``). What is still refused is an edge endpoint that **no declared model
        names**: there is then no class to construct against the channel. Before per-port
        resolution this graph was rejected for the wrong reason — because the module touched both
        sides at all, rather than because nothing covered this port.
        """
        tb = _tb(with_edge=False)
        mon = TokenMonitor(name="m4", sim=tb.sim)
        tb.add_comp(mon)
        rx = TokenRx(name="snk_tok_in", sim=tb.sim)
        # The sink answers a DUT boundary port, and its bfm_model() names only `stream_ep`.
        tb.sink.add_endpoint(rx)
        edge = TokenIF(name="tb_dual_if", sim=tb.sim)
        edge.bind("tx", mon.tok_out)
        edge.bind("rx", rx)
        tb.add_if(edge)
        with pytest.raises(LoweringError, match="none of its declared models names that port"):
            tb_top_spec(tb)

    def test_a_half_wired_edge_is_reported_as_half_wired(self):
        """The message must name the real defect.

        An interface with one side bound would otherwise be reported as "declares no xsi_model()",
        which sends the reader off to write a hook for an edge that is simply not connected.
        """
        tb = _tb(with_edge=False)
        mon = TokenMonitor(name="m5", sim=tb.sim)
        tb.add_comp(mon)
        edge = TokenIF(name="tb_half_if", sim=tb.sim)
        edge.bind("tx", mon.tok_out)                  # rx never bound
        tb.add_if(edge)
        with pytest.raises(LoweringError, match="so it connects nothing"):
            tb_top_spec(tb)

    def test_a_name_collision_between_a_channel_and_a_port_is_refused(self):
        """Every emitted identifier shares one struct scope, so a collision shadows rather than
        fails to compile — a model silently binding the wrong thing."""
        tb = _tb_with_extra_edge(name="s_in")         # collides with the DUT's boundary port model
        with pytest.raises(LoweringError, match="would declare 's_in' twice"):
            tb_top_spec(tb)


# ---------------------------------------------------------------------------
# The emitted harness
# ---------------------------------------------------------------------------

class TestEmission:

    def test_a_channel_is_declared_registered_and_constructed_before_both_peers(self):
        """The ordering claim, checked in all three places it has to hold.

        Declaration order is construction order in C++, and construction order is what puts the
        channel's ``sample()`` first in the participant sweep — which is what makes the transfer
        independent of participant order (``xsi_channel.h``). If any of the three slipped, the
        C++ would still compile and the timing would silently change.
        """
        text = render_tb_harness(tb_top_spec(_tb()))

        def at(needle: str) -> int:
            i = text.find(needle)
            assert i >= 0, f"{needle!r} missing from the generated harness"
            return i

        # 1. member declaration
        assert at("BlockChannel<uint64_t> tb_tok_if;") < at("TokenMonitor tb_tok_if_tx;")
        assert at("BlockChannel<uint64_t> tb_tok_if;") < at("TokenScoreboard tb_tok_if_rx;")
        # 2. constructor init list
        assert at("tb_tok_if(4)") < at("tb_tok_if_tx(tb_tok_if)")
        assert at("tb_tok_if(4)") < at("tb_tok_if_rx(tb_tok_if)")
        # 3. participant registration (the list the five phases iterate)
        assert at("participants_.push_back(&tb_tok_if);") \
            < at("participants_.push_back(&tb_tok_if_tx);")
        assert at("participants_.push_back(&tb_tok_if);") \
            < at("participants_.push_back(&tb_tok_if_rx);")

    def test_a_peer_model_binds_the_channel_not_a_port(self):
        text = render_tb_harness(tb_top_spec(_tb()))
        assert "tb_tok_if_tx(tb_tok_if)" in text
        assert "tb_tok_if_tx(sim.dut()" not in text
        # ...while a boundary model still binds the DUT and its port constant.
        assert "s_in(sim.dut(), edge_toy_ports::s_in, {})" in text

    def test_the_channel_header_is_included_only_when_needed(self):
        with_edge = render_tb_harness(tb_top_spec(_tb()))
        without = render_tb_harness(tb_top_spec(_tb(with_edge=False)))
        assert '#include "xsi_channel.h"' in with_edge
        assert '#include "xsi_channel.h"' not in without

    def test_a_channel_dynparam_is_emitted_as_a_member_assignment(self):
        text = render_tb_harness(tb_top_spec(_tb()))
        assert 'tb_tok_if.label = "mon";' in text

    def test_a_channel_is_not_mistaken_for_a_test_supplied_ctor_param(self):
        """A channel is a harness member, so it must not become a ``Harness(...)`` argument."""
        text = render_tb_harness(tb_top_spec(_tb()))
        sig = [ln for ln in text.splitlines() if ln.startswith("    explicit Harness(")][0]
        assert "tb_tok_if" not in sig


def _committed_tbs():
    """The TB instances the **committed** harnesses were generated from.

    Each example exposes the exact graph its generator uses (``make_xsi_tb`` / the ``generate_tb``
    scenario), which is the only construction whose output can be compared against the committed
    file. Building a TB with library defaults instead compares against a *different scenario* and
    fails for reasons that have nothing to do with this change — that is how the first draft of this
    test failed.
    """
    from examples.interleaver.interleaver_inband import make_xsi_tb as il_tb
    from examples.mem_copy.mem_copy import make_xsi_tb as mc_tb
    from examples.state_toy.state_toy import StateAccumTB

    return [
        ("mem_copy", mc_tb()),
        ("interleaver_inband", il_tb()),
        # state_toy's generator builds the TB inline; nvec/n_cycles are its committed defaults.
        ("state_accum", StateAccumTB(name="tb", sim=Simulation(), nvec=5, n_cycles=400)),
    ]


class TestExistingDesignsUnchanged:
    """The S1/S3 gate: no design that predates behavioral edges may move a byte.

    Compared against the **committed artifact**, not against a snapshot taken in this test — a
    self-captured baseline would pass on any change at all.
    """

    def test_no_existing_design_has_a_behavioral_edge(self):
        for name, tb in _committed_tbs():
            assert tb_top_spec(tb).channels == (), f"{name} unexpectedly grew a channel"

    def test_the_committed_harnesses_regenerate_identically(self):
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        checked = 0
        for name, tb in _committed_tbs():
            spec = tb_top_spec(tb)
            hits = list(repo.glob(f"examples/*/xsi/{spec.top_name}_tb_harness.h"))
            if not hits:
                continue
            got = render_tb_harness(spec).replace("\r\n", "\n")
            want = hits[0].read_text(encoding="utf-8").replace("\r\n", "\n")
            assert got == want, f"{hits[0]} would change ({name})"
            checked += 1
        assert checked >= 2, f"only {checked} committed harnesses were actually compared"


# ---------------------------------------------------------------------------
# The emitted harness must be well-formed C++
# ---------------------------------------------------------------------------

def _vivado_xsim_include() -> "Path | None":
    """Vivado's ``data/xsim/include`` — the one thing a *harness* compile needs that a channel
    compile does not (``xsi_bfm.h`` -> ``xsi_loader.h`` -> ``xsi.h``)."""
    from pathlib import Path

    for base in sorted(Path("C:/Xilinx").glob("*/Vivado/data/xsim/include"), reverse=True):
        if base.is_dir():
            return base
    for base in sorted(Path("/opt/Xilinx").glob("*/Vivado/data/xsim/include"), reverse=True):
        if base.is_dir():
            return base
    return None


@pytest.mark.skipif(_GXX is None, reason="g++ not on PATH")
def test_the_generated_harness_with_a_channel_compiles(tmp_path):
    """Compile the **real** emitted harness, channel and all.

    Syntax-only, and Vivado-gated because ``xsi_bfm.h`` reaches Vivado's ``xsi.h``. That is why the
    channel primitive itself was split out into a header with no toolchain dependency at all
    (``test_xsi_channel.py`` compiles *and runs* it with nothing installed) — this check covers the
    part that genuinely cannot: whether the generator's output is well-formed C++.

    Skips loudly without Vivado rather than passing quietly.
    """

    from waveflow.build.composite_gen import composite_top_spec, render_ports_h

    inc = _vivado_xsim_include()
    if inc is None:
        pytest.skip("no Vivado data/xsim/include found — cannot compile a harness (xsi.h)")

    # edge_label="": a DynParam must land on a real C++ member, and BlockChannel has none to spare.
    tb = _tb(edge_label="")
    spec = tb_top_spec(tb)
    assert len(spec.channels) == 1
    (tmp_path / "edge_toy_ports.h").write_text(
        render_ports_h(composite_top_spec(tb.dut, width=64)), encoding="utf-8")
    (tmp_path / "edge_toy_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")

    # The two peer models the fixture names.  Written here rather than added to the C++ library: they
    # are a test fixture, and a library model that exists only to be tested is not a library model.
    (tmp_path / "peers.cpp").write_text(r"""
#include <cstdint>
#include <vector>
#include "xsi_channel.h"
namespace wfbfm {
struct TokenMonitor : public XsiSimObj {
    BlockChannel<uint64_t>& ch; uint64_t next_ = 0;
    explicit TokenMonitor(BlockChannel<uint64_t>& c) : ch(c) {}
    void update() override { if (next_ < 3) ch.push(next_++); }
};
struct TokenScoreboard : public XsiSimObj {
    BlockChannel<uint64_t>& ch; std::vector<uint64_t> seen;
    explicit TokenScoreboard(BlockChannel<uint64_t>& c) : ch(c) {}
    void update() override { uint64_t v; if (ch.pop(v)) seen.push_back(v); }
};
}  // namespace wfbfm
#include "edge_toy_tb_harness.h"
int main() { edge_toy_tb::Harness h("x.wdb"); h.run(10); h.close(); return 0; }
""", encoding="utf-8")

    r = subprocess.run(
        [_GXX, "-std=c++17", "-fsyntax-only", f"-I{_XSI_SRC}", f"-I{inc}", f"-I{tmp_path}",
         str(tmp_path / "peers.cpp")],
        check=False, capture_output=True, text=True)
    assert r.returncode == 0, f"the generated harness does not compile:\n{r.stderr[-4000:]}"


# ---------------------------------------------------------------------------
# pysim: the same graph still runs
# ---------------------------------------------------------------------------

def test_the_behavioral_edge_runs_in_pysim(tmp_path):
    """An edge with a ``run_proc``-shaped body is a pysim object first; the XSI form is the twin.

    Cheap, but it is the half that proves the graph is a real graph rather than a codegen fixture.
    """
    from waveflow.utils.burst_io import write_burst_bundle

    tb = _tb()
    write_burst_bundle([np.arange(4, dtype=np.uint64)], tmp_path / "vectors" / "s_in")
    tb.driver.root = tmp_path
    tb.sim.run_sim()
    assert tb.board.seen == [0, 1, 2]
    assert tb.tok_if.dropped == 0
