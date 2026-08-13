"""composite_gen.py — the graph-derived free-running (``ap_ctrl_none``) ``hls::task`` top generator.

:func:`composite_top_spec` walks a built hierarchical component's graph — ``ordered_subcomps``
(``add_comp``), ``internal_edges`` (``add_if``), and ``boundary`` ports — and derives the
:class:`TopSpec` for one free-running top, resolving each sub-component's ``hls::task`` signature
(endpoint attr names, from :meth:`~waveflow.hw.mem_stream.KernelTask`) to a top-level port or an
internal edge.  :func:`render_top` emits it.

**What this generates, and what it does not.**  The **top** — ports, interface pragmas, internal
channel declarations, and one ``hls::task`` per active child — is derived from the graph.  The task
**bodies** it instantiates are separate artifacts: hand-written headers (``mem_r_stream_task.h``) or,
for a stream-only leaf, generated ones (:func:`~waveflow.build.hwgen.task_files_to_str`).  This
module never writes a body.

Edge kinds: an edge declares how it lowers.  A :class:`StreamEdge` emits an ``hls::stream`` FIFO; a
:class:`SobEdge` emits an ``hls::stream_of_blocks<T[N], 2>`` (the SOBIF depth-2 ping-pong).  The walk
and arg resolution are otherwise identical.

Framework, not example code: a standalone kernel (one node, no edges), a memcpy composite (stream
edges), and a SOBIF toy (a block edge) all fall out of this *same* generator.  Per-example drivers
(which components, which output dirs) live in the examples.
"""
from __future__ import annotations

from dataclasses import dataclass

from waveflow.build.hwcodegen import LoweringError
from waveflow.hw.hw_module import declares_hook
from waveflow.hw.interface import DEFAULT_STREAM_DEPTH

#: Output-directory convention shared by the generated tops and their csynth .tcl.
INCLUDE_DIR = "include"
GEN_DIR = "gen"
DEFAULT_MEM_DW = 64


# ---------------------------------------------------------------------------
# Top model (a standalone kernel = the 1-task degenerate case of a composite)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExtPort:
    """One external interface port of the generated top: its C++ param decl + interface pragmas.

    ``name`` / ``kind`` / ``bundle`` are kept alongside the rendered strings so the spec stays
    *answerable* rather than only renderable.  A testbench needs to bind the same ports this port
    declares (``s_cmd`` -> ``s_cmd_TVALID``; a ``maxi_read`` on ``gmem0`` -> ``m_axi_gmem0_ARVALID``),
    and deriving both from ONE spec is what stops the TB drifting from the kernel — see
    :func:`render_ports_h`.  Without these the triple is lost in the ``decl`` string.
    """
    decl: str
    pragmas: tuple[str, ...]
    name: str = ""                  # the boundary port name, e.g. "s_cmd" / "m_in"
    kind: str = ""                  # axis_in | axis_out | maxi_read | maxi_write
    bundle: str | None = None       # m_axi bundle for maxi_* kinds, else None

    @property
    def xsi_prefix(self) -> str:
        """The RTL port-name prefix this port presents to a testbench.

        Mechanical, per Vitis: an AXIS port keeps its own name (``s_cmd`` -> ``s_cmd_TDATA``), an
        ``m_axi`` port is named after its BUNDLE, not the port (``m_in`` on ``gmem0`` ->
        ``m_axi_gmem0_ARVALID``).  That asymmetry is exactly the sort of thing a hand-written TB gets
        wrong once and then carries.
        """
        if self.kind in ("maxi_read", "maxi_write"):
            return f"m_axi_{self.bundle}"
        return self.name


@dataclass(frozen=True)
class TaskInst:
    """One ``hls::task`` instantiation inside the top: the templated body + its call args.

    ``template_args`` are the baked-concrete template arguments in order — ``(64,)`` for a
    width-templated mem-stream body (``mem_r_stream_task<64>``), ``(32, 256)`` for the
    ``<EW, N>``-templated compute tiles (``fill_task<32, 256>``)."""
    task_fn: str            # e.g. "mem_r_stream_task"
    template_args: tuple[int, ...]   # baked-concrete template args, e.g. (64,) or (32, 256)
    args: tuple[str, ...]   # arg names (external ports and/or internal streams), in signature order
    header: str             # the task-body header to include

    @property
    def inst_name(self) -> str:
        """The RTL instance name Vitis gives this task: ``<task_fn>_<template args>_U0``.

        Verified against the csynth RTL of both traced designs (``mem_seq_framed_task`` + ``(64,)``
        -> ``mem_seq_framed_task_64_U0``; ``il_compute_task`` + ``(64, 128)`` ->
        ``il_compute_task_64_128_U0``) -- 9 instances, no mismatches.  Everything but the ``_U0``
        suffix is codegen-owned, which is what makes the nets bindable from Python at all.

        The suffix is a **prediction**: a second instance of the same body at the same template
        args would be ``_U1``, and nothing here could tell them apart.  :meth:`TopSpec.trace_manifest`
        rejects that case rather than emitting names that silently bind to the wrong instance."""
        return "_".join([self.task_fn, *(str(a) for a in self.template_args)]) + "_U0"


@dataclass(frozen=True)
class BfmModel:
    """The XSI testbench model a participant lowers to — the testbench twin of :class:`TaskInst`.

    A testbench participant does not *lower* to C++; it **maps** to a pre-written, cycle-exact model
    in :mod:`waveflow.build.xsi` (``AxisMaster``, ``AxiMmReadSlave``, ...).  Extracting an equivalent
    FSM from Python would be re-deriving verified code, so a participant just declares which model it
    is — exactly as :meth:`~waveflow.hw.mem_stream.KernelTask` declares which hand-written
    ``hls::task`` body a component uses.  That is what makes the TB emitter a *resolver* rather than
    an extractor.

    * ``cls`` — the C++ model class, e.g. ``"AxisMaster"``.
    * ``ports`` — the participant's endpoint attribute names, in constructor order.  **Each resolves
      by its own kind** (:func:`_resolve_model_binding`): an endpoint facing a DUT boundary port
      contributes ``sim.dut(), <ports_ns>::<port>``; an endpoint on a behavioral edge contributes
      that edge's channel variable.
    * ``extra_args`` — literal C++ expressions appended after the resolved ports, e.g. the words an
      ``AxisMaster`` presents or the arena an ``AxiMmReadSlave`` serves.  They name things the
      hand-written half of the testbench declares.
    * ``shared`` — if set, this model is constructed once and *shared* by name rather than per
      endpoint (the arena behind two m_axi bundles is one ``FlatMemory``, not two).

    **A module may declare more than one.**  ``bfm_model()`` returns one of these or a sequence of
    them (see :func:`bfm_models`), because two facts turned out not to be per-*module*:

    1. **The class is per data path, not per module.**  ``bfm_dual_class`` hands back the
       participant's declared class for AXI-Stream, so one declaration cannot give a converter's
       receive port an ``RfdcAdcMaster`` and its transmit port an ``RfdcDacSlave``.
    2. **The constructor shape is per port.**  A model that spans *both* sides of the cut — RTL pins
       on one side, a behavioral edge on the other — is exactly what a converter is, and its ports
       cannot all resolve the same way.

    A single ``BfmModel`` whose ports are all boundary ports resolves precisely as it always did,
    which is every design that existed before this generalization.
    """
    cls: str
    ports: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()
    shared: str | None = None


@dataclass(frozen=True)
class ChannelModel:
    """The XSI **channel** a behavioral edge lowers to — the edge-side twin of :class:`BfmModel`.

    A ``BfmModel`` says "this *module* is realized as model *X*, bound to these RTL pins".  A
    ``ChannelModel`` says "this *interface* is realized as channel *X*, bound by these two peer
    models".  The difference is the whole of what a behavioral edge is: both its endpoints lie
    outside the cut, so there is no DUT port between them and no BFM dual to look up — but the edge
    is not therefore invisible, because the endpoint set is invariant across backends.

    * ``cls`` — the C++ channel class, e.g. ``"BlockChannel<uint64_t>"``.  Template arguments are
      part of the name; the registry check strips them (a class exists, a specialization is a use).
    * ``peers`` — this interface's **endpoint side names** (keys of ``Interface.endpoints``), the
      producer first.  Side names rather than attribute names because an interface *owns* its sides —
      the naming problem :class:`BfmModel` has, where ``ports`` must be attribute names because C++
      constructor order is recorded nowhere else, simply does not arise here.
    * ``extra_args`` — literal C++ ctor args, e.g. the depth.  What the channel needs and the graph
      does not carry.

    Order matters and is stated rather than inferred: ``peers[0]`` is the side that *pushes*.  A
    channel is one-producer/one-consumer (multi-producer is deferred, ``plans/behavioral_edges.md``
    S5), so a wrong order is a direction error, not a cosmetic one.
    """
    cls: str
    peers: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntChannel:
    """One internal channel of the generated top: its ``hls_thread_local`` decl + what it connects.

    The internal-edge counterpart of :class:`ExtPort`, and for the same reason.  ``composite_top_spec``
    used to keep only ``edge.decl(width)`` — a rendered string — so the spec could *emit* a channel
    but could not *answer* anything about it.  Recovering the name from the decl means parsing C++
    back out of a string, and the endpoints were simply gone.

    What needs answering is the same question :class:`ExtPort` already answers for boundary ports:
    which RTL nets does this become?  A channel named ``cmd`` between two ``hls::task`` bodies
    lowers to the top-scope nets ``cmd_dout`` / ``cmd_empty_n`` / ``cmd_full_n``, plus
    ``<producer_inst>_cmd_din`` / ``_cmd_write`` and ``<consumer_inst>_cmd_read`` — Vitis lifts
    dataflow channel wires into the top scope beside the task instances.  Deriving those names from
    THIS spec is what stops a trace/timing consumer drifting from the kernel.

    * ``kind``  — ``stream`` (``ap_uint`` FIFO) | ``framed`` (``framed_word``, packet boundary in the
      bit above the payload) | ``sob`` (``stream_of_blocks`` ping-pong).
    * ``width`` — payload width ``W``.  For a ``framed`` channel the RTL net is ``W+1`` bits with
      ``last`` on top; for ``sob`` it is the element width.
    * ``master_task`` / ``slave_task`` — indices into :attr:`TopSpec.tasks`, i.e. the producer and
      consumer.  Indices rather than names because two instances of the same task body are
      distinct RTL instances but share a ``task_fn``.
    """
    decl: str
    name: str = ""                      # the C++ channel variable, e.g. "cmd" / "copy_data"
    kind: str = ""                      # stream | framed | sob
    width: int = 0                      # payload width W
    master_task: int | None = None      # index into TopSpec.tasks (producer)
    slave_task: int | None = None       # index into TopSpec.tasks (consumer)


@dataclass(frozen=True)
class TopSpec:
    """A generated free-running (``ap_ctrl_none``) ``hls::task`` top.  For a standalone kernel there
    is one task and no internal streams; a composite adds tasks + ``hls_thread_local`` streams (or
    ``stream_of_blocks``) wiring their internal edges, keeping the external ports the boundary."""
    top_name: str
    ports: tuple[ExtPort, ...]              # external interface ports (signature order)
    tasks: tuple[TaskInst, ...]
    cmd_headers: tuple[str, ...]            # command struct headers to include
    channels: tuple[IntChannel, ...] = ()   # internal edges (empty for a standalone kernel)
    extra_includes: tuple[str, ...] = ()    # extra system headers (e.g. hls_streamofblocks.h)

    @property
    def internal_streams(self) -> tuple[str, ...]:
        """The ``hls_thread_local`` decls, in channel order.

        Derived from :attr:`channels` rather than stored, so there is one source for a channel and
        the decl cannot drift from what the channel says it is."""
        return tuple(c.decl for c in self.channels)

    def trace_manifest(self) -> dict:
        """The RTL net names this top lowers to, as a JSON-ready dict.

        Turns the spec into the one thing a waveform consumer needs and cannot safely guess:
        *which exact net* carries each channel, port and task boundary.  Derived entirely at
        elaborate time -- no RTL is read, no simulation is run -- so it is cheap, DAG-cacheable and
        unit-testable without Vivado.

        **Why a manifest instead of matching names in the trace.**  Substring matching is not merely
        fragile here, it is wrong: an interleaver trace contains both ``ywords_fifo_cap[2:0]`` (the
        channel) and ``il_store_..._U0_ywords_fifo_cap[31:0]`` (the instance's own copy).  They differ
        in width and meaning, and a matcher picks whichever it sees first.  The names are already
        known here -- codegen chose them -- so binding is exact, and a name that has gone missing
        fails loudly instead of silently extracting nothing.

        Names are stored bare, without the ``[hi:lo]`` suffix a VCD appends to a vector; the loader
        resolves that (see :func:`waveflow.utils.trace.load_trace`).

        Scope is the top's own scope: boundary ports, inter-task channels, and per-task ``ap_*``
        pins.  That is exactly what a level-1 ``$dumpvars`` of the top captures, because Vitis lifts
        dataflow channel wires up beside the task instances.  Tracing *inside* a task body needs a
        hierarchical path and a scan of the generated Verilog, and is not covered here.

        Raises
        ------
        ValueError
            If two tasks share a ``(task_fn, template_args)`` pair.  Both would predict the same
            ``_U0`` instance, and every net derived from them would bind to whichever Vitis happened
            to name first.  Neither current design does this; the guard exists so the day one does,
            it is a build error rather than a wrong timing model.
        """
        seen: dict[tuple, str] = {}
        for t in self.tasks:
            key = (t.task_fn, t.template_args)
            if key in seen:
                raise ValueError(
                    f"{self.top_name}: two tasks share the body {t.task_fn}{list(t.template_args)}, "
                    f"so both predict instance {t.inst_name}. Vitis would name them _U0/_U1 and the "
                    f"manifest cannot tell which is which -- resolving this needs the generated "
                    f"Verilog, so the manifest declines rather than guess.")
            seen[key] = t.inst_name

        return {
            "version": _TRACE_MANIFEST_VERSION,
            "top": self.top_name,
            # HLS names the generated kernel's clock and reset; they are not derived from the graph.
            "clock": "ap_clk",
            "reset": "ap_rst_n",
            "tasks": [_task_trace(t) for t in self.tasks],
            "boundary": _boundary_trace(self.ports),
            "channels": [_channel_trace(c, self.tasks) for c in self.channels],
        }


# ---------------------------------------------------------------------------
# Trace manifest — the RTL net names the spec lowers to.
#
# Every rule below was checked against the csynth RTL of mem_copy and interleaver_canon.  They are
# the whole reason the manifest can be derived from Python: Vitis picks only the `_U0` instance
# suffix, and codegen owns everything else (channel names are the C++ variable names, port names
# are the boundary names, `m_axi` nets are named after the bundle).
# ---------------------------------------------------------------------------

_TRACE_MANIFEST_VERSION = 1

#: Control pins Vitis puts on every task instance.  In a free-running (`ap_ctrl_none`) top these are
#: mostly degenerate -- `ap_idle` never asserts, so a task reads as busy for the whole run -- but
#: `ap_done` pulses once per firing and is a free per-job completion event.
_TASK_PINS = ("ap_start", "ap_done", "ap_idle", "ap_ready", "ap_continue")

#: AXI4-Full signal groups.  `AWLEN`/`WLAST`/`ARLEN`/`RLAST` are absent on an AXI4-Lite bundle; the
#: manifest names them anyway and the loader treats absence as "not this flavour" rather than an
#: error (see `waveflow.utils.trace`).
_AXI_WRITE_SIGS = ("AWADDR", "AWVALID", "AWREADY", "AWLEN",
                   "WDATA", "WVALID", "WREADY", "WLAST", "BVALID", "BREADY")
_AXI_READ_SIGS = ("ARADDR", "ARVALID", "ARREADY", "ARLEN",
                  "RDATA", "RVALID", "RREADY", "RLAST")


def _task_trace(task: TaskInst) -> dict:
    # `args` are the channel / boundary-port names this task is wired to, in signature order.  They
    # are what makes a component's OWN surface answerable -- "which streams does this component
    # touch" is otherwise only derivable by scanning every channel for a matching endpoint.
    #
    # `id` is the CONFIGURATION-QUALIFIED id, not the bare `task_fn`.  A measured span belongs to the
    # body at the template args it was synthesized with -- `config_id` says so, and says it is shared
    # "between the resource path and the timing path, so the two cannot drift on what counts as the
    # same configuration".  This side had drifted: it emitted `mem_w_stream_framed_done_task` while
    # the attached StreamTimingModel keyed itself `mem_w_stream_framed_done_task_64_8`, so
    # `collect_rtl` -- which matches the two by equality -- filed an EMPTY rtl corpus beside a full
    # pysim one, and the fit died on a headerless csv.  That is why mem_copy's RTL spans were
    # hand-typed constants: the automated path silently produced nothing.
    #
    # `body` keeps the bare name alongside it, so "look this component up by its task body" -- which
    # ComponentView documents and callers use -- still works without anyone having to know the
    # template args.  Two fields rather than one lossy field: the qualified id is what a measurement
    # is keyed by, the body is what a human names.
    from waveflow.calib.module_key import config_id
    return {"id": config_id(task),
            "body": task.task_fn,
            "inst": task.inst_name,
            "args": list(task.args),
            "signals": {p: f"{task.inst_name}_{p}" for p in _TASK_PINS}}


def _boundary_trace(ports: tuple[ExtPort, ...]) -> list[dict]:
    """Boundary ports, keyed the way the RTL names them.

    Note the asymmetry :class:`ExtPort` exists to record: an AXIS port keeps its own name
    (``s_cmd`` -> ``s_cmd_TVALID``) while an ``m_axi`` port is named after its BUNDLE
    (``m_in`` on ``gmem0`` -> ``m_axi_gmem0_ARVALID``).  Two ports sharing a bundle are therefore
    ONE entry here, carrying whichever directions they collectively use."""
    entries: list[dict] = []
    bundles: dict[str, set[str]] = {}          # insertion-ordered, so the output is deterministic
    bundle_ports: dict[str, list[str]] = {}    # bundle -> the port names folded into it

    for p in ports:
        if p.kind in ("axis_in", "axis_out"):
            entries.append({
                "id": p.name,
                "kind": p.kind,
                "ports": [p.name],
                "signals": {"tdata": f"{p.name}_TDATA",
                            "tvalid": f"{p.name}_TVALID",
                            "tready": f"{p.name}_TREADY",
                            # Optional: a plain hls::stream<ap_uint<W> > boundary port has no TLAST
                            # wire at all (mem_copy's s_cmd).  Named so a framed port still binds.
                            "tlast": f"{p.name}_TLAST"},
            })
        elif p.kind in ("maxi_read", "maxi_write"):
            bundle = p.bundle or p.name
            bundles.setdefault(bundle, set()).add(
                "read" if p.kind == "maxi_read" else "write")
            bundle_ports.setdefault(bundle, []).append(p.name)

    for bundle, dirs in bundles.items():
        sigs: dict[str, str] = {}
        if "read" in dirs:
            sigs.update({s: f"m_axi_{bundle}_{s}" for s in _AXI_READ_SIGS})
        if "write" in dirs:
            sigs.update({s: f"m_axi_{bundle}_{s}" for s in _AXI_WRITE_SIGS})
        # `ports` recovers the port -> bundle mapping.  A task's args name the PORT (`m_in`) while
        # the RTL nets are named after the BUNDLE (`m_axi_gmem0_*`), and two ports can share one
        # bundle -- without this, a task arg cannot be resolved back to its boundary entry.
        entries.append({"id": bundle, "kind": "maxi", "ports": bundle_ports[bundle],
                        "directions": sorted(dirs), "signals": sigs})
    return entries


def _channel_trace(ch: IntChannel, tasks: tuple[TaskInst, ...]) -> dict:
    """One internal channel's nets, both ends.

    The two ends are not redundant: the write side shows when the producer offered a word, the read
    side when the consumer took it, and the gap between them is the channel's occupancy -- which is
    the quantity a latency model actually wants."""
    prod = tasks[ch.master_task].inst_name if ch.master_task is not None else None
    cons = tasks[ch.slave_task].inst_name if ch.slave_task is not None else None
    entry = {"id": ch.name, "kind": ch.kind, "width": ch.width,
             "producer": prod, "consumer": cons}

    if ch.kind == "sob":
        # A stream_of_blocks is a ping-pong block RAM plus a lock handshake, not a FIFO: there is no
        # din/write/full_n vocabulary to bind, so the channel is declared but has no burst view.
        return entry

    entry["write"] = {"din": f"{prod}_{ch.name}_din",
                      "write": f"{prod}_{ch.name}_write",
                      "full_n": f"{ch.name}_full_n"}
    entry["read"] = {"dout": f"{ch.name}_dout",
                     "read": f"{cons}_{ch.name}_read",
                     "empty_n": f"{ch.name}_empty_n"}
    # The FIFO's own occupancy counters.  These are the only RELIABLE way to see backpressure: HLS
    # gates the write enable, so a task blocked on a full channel stalls its pipeline WITHOUT ever
    # asserting `write` -- a `write & !full_n` metric reads zero even while the producer is stuck.
    # Comparing `level` against `cap` is what actually located mem_copy's 30 cycles/job of blocking.
    entry["depth"] = {"level": f"{ch.name}_num_data_valid", "cap": f"{ch.name}_fifo_cap"}
    return entry


def _axis_port(name: str, width: int, kind: str = "axis_in") -> ExtPort:
    """An AXIS boundary port.  *kind* (``axis_in``/``axis_out``) does not change the emitted C++ — a
    kernel's ``hls::stream&`` is the same either way — but it records the DIRECTION, which is what a
    testbench needs to know whether to model a master or a slave against it."""
    return ExtPort(f"hls::stream<ap_uint<{width}> >& {name}",
                   (f"#pragma HLS INTERFACE axis port={name}",),
                   name=name, kind=kind, bundle=None)


def _maxi_port(name: str, width: int, *, const: bool, bundle: str = "gmem0") -> ExtPort:
    """An ``m_axi`` master port on *bundle* (one AXI bundle per distinct memory port — a composite
    with independent read/write memories places them on ``gmem0``/``gmem1``).  The read owner is
    ``const`` (the ``@port_read`` capability -> a stray write is a compile error) and gets
    ``#pragma HLS stable``; the write owner is plain."""
    qual = "const " if const else ""
    pragmas = [f"#pragma HLS INTERFACE m_axi port={name} offset=slave bundle={bundle} depth=8192"]
    if const:
        pragmas.append(f"#pragma HLS stable variable={name}")
    return ExtPort(f"{qual}ap_uint<{width}>* {name}", tuple(pragmas),
                   name=name, kind=("maxi_read" if const else "maxi_write"), bundle=bundle)


# ---------------------------------------------------------------------------
# Internal-edge kinds — each knows how it lowers to a C++ channel declaration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StreamEdge:
    """A stream internal edge (the default) -> ``hls_thread_local hls::stream<ap_uint<W> >``.

    ``depth`` (optional) sets the FIFO depth via ``#pragma HLS STREAM`` — the only edge that needs it
    is the interleaver's ``p_words`` (P is loaded first and buffered whole while X fills the block, so
    the FIFO must hold ``>= n/LW`` words; this is exactly sob3's ``#pragma HLS STREAM ... depth=1024``).
    ``None`` uses the default depth-2 FIFO.  This is edge configuration, not a new codegen kind."""
    name: str
    master_ep: object
    slave_ep: object
    depth: int | None = None

    def decl(self, width: int) -> str:
        line = f"hls_thread_local hls::stream<ap_uint<{width}> > {self.name};"
        if self.depth is not None and self.depth != DEFAULT_STREAM_DEPTH:
            # The HLS default IS DEFAULT_STREAM_DEPTH, so emitting a pragma for it would only churn
            # the generated C++ (and force a re-csynth) for identical RTL.  Emit only a non-default.
            line += f"\n    #pragma HLS STREAM variable={self.name} depth={self.depth}"
        return line


@dataclass(frozen=True)
class FramedEdge:
    """A **framed** internal edge -> ``hls_thread_local hls::stream<streamutils::framed_word<W> >``.

    Like :class:`StreamEdge`, but the channel word carries a per-beat packet boundary
    (``framed_word{data, last}``) so a consumer can relay an opaque packet it refuses to parse (a
    countless read needs a boundary, not a count).  ``ap_axis`` cannot be used on an internal FIFO
    (Vitis HLS 214-208), so the boundary rides on the ``framed_word`` struct instead; the real TLAST
    lives only on the top-level ``axi4s`` boundary ports.  See ``plans/memstream_inband.md``.

    Produced by ``derive_internal_edges`` for a ``StreamIF`` whose ``framed`` flag is set; a plain
    ``StreamIF`` still lowers to a :class:`StreamEdge`, so nothing existing changes."""
    name: str
    master_ep: object
    slave_ep: object
    depth: int | None = None

    def decl(self, width: int) -> str:
        line = (f"hls_thread_local hls::stream<streamutils::framed_word<{width}> > "
                f"{self.name};")
        if self.depth is not None and self.depth != DEFAULT_STREAM_DEPTH:
            # See StreamEdge.decl: the default depth is HLS's own, so no pragma for it (keeps the
            # generated C++ and its RTL byte-identical).
            line += f"\n    #pragma HLS STREAM variable={self.name} depth={self.depth}"
        return line


@dataclass(frozen=True)
class SobEdge:
    """A block (SOBIF) internal edge -> ``hls_thread_local hls::stream_of_blocks<T[N], 2>`` (the
    depth-2 ping-pong)."""
    name: str
    master_ep: object
    slave_ep: object
    elem_bw: int
    block_n: int
    depth: int = 2

    def decl(self, width: int) -> str:
        return (f"hls_thread_local hls::stream_of_blocks<ap_uint<{self.elem_bw}>[{self.block_n}], "
                f"{self.depth}> {self.name};")


def kind_of_endpoint(ep) -> str:
    """The boundary kind an endpoint lowers to, **derived from its type**.

    The direction is the type, not a tag beside it: a ``StreamIFSlave`` is an input, a
    ``MMIFReadMaster`` is a ``const`` pointer.  Nothing infers, and nothing has to be told separately
    — the same call this codebase already makes for execution models (``hw_freerun``: *"the execution
    model is declared by the class ... codegen never has to infer"*).  See
    ``plans/endpoint_types_not_tags.md``.

    A bare :class:`~waveflow.hw.memif.MMIFMaster` is **refused**, not defaulted.  It is legal
    hardware (a read+write ``m_axi`` lowers to a plain pointer with all channels), but it
    under-specifies: guessing a direction here is exactly the side-channel this function exists to
    delete, and guessing wrong emits a ``const`` pointer for a port that is written.
    """
    from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
    from waveflow.hw.memif import MMIFMaster, MMIFReadMaster, MMIFSlave, MMIFWriteMaster
    from waveflow.hw.regmap import RegMapMMIFSlave

    # A regmap slave is the AXI4-Lite control port Vitis creates for a host-activated kernel.  It has
    # a real kind, so a walk that meets one can SAY so — `_boundary_port` still refuses to lower it
    # (there is no ap_ctrl_none top with an s_axilite port) and `BFM_DUALS` records that no model
    # drives it.  Naming the kind is what turns both refusals from "unknown endpoint type" into the
    # actual diagnosis.  See plans/design_cut.md S2/S7.
    if isinstance(ep, RegMapMMIFSlave):
        return "axilite_slave"
    # A plain AXI-MM slave — a memory's bus-facing port.  Never a *kernel* boundary port in this flow
    # (the kernel is always the master), so `_boundary_port` still refuses it; but it is a real kind a
    # participant presents, and naming it is what lets the BFM-dual lookup answer for a `MemoryMod`.
    if isinstance(ep, MMIFSlave):
        return "mm_slave"
    if isinstance(ep, MMIFReadMaster):
        return "maxi_read"
    if isinstance(ep, MMIFWriteMaster):
        return "maxi_write"
    if isinstance(ep, MMIFMaster):
        raise LoweringError(
            f"{type(ep).__name__} '{getattr(ep, 'name', '?')}' does not declare a direction, so its "
            f"pointer cannot be lowered (const + stable, or plain?). Construct it as an "
            f"MMIFReadMaster or MMIFWriteMaster — the direction is the type."
        )
    if isinstance(ep, StreamIFSlave):
        return "axis_in"
    if isinstance(ep, StreamIFMaster):
        return "axis_out"
    raise LoweringError(f"no boundary kind for endpoint type {type(ep).__name__}")


def _edge_name(comp, iface) -> str:
    """The C++ channel variable name for *iface*: its own name, minus the owner prefix and ``_if``.

    The interface is named ``f"{comp.name}_{edge}_if"`` by convention, so the edge name is already
    written down — it does not need restating in a parallel list.
    """
    name = iface.name
    prefix = f"{comp.name}_"
    if name.startswith(prefix):
        name = name[len(prefix):]
    if name.endswith("_if"):
        name = name[: -len("_if")]
    return name


def derive_internal_edges(comp) -> list:
    """The internal edges of *comp*, **derived from the interfaces it registered with ``add_if``**.

    ``add_if`` already "records the master↔slave connection so the composite codegen can lower it",
    which is the whole content of an edge: the two endpoints, the channel name, and how it lowers.
    So a composite that wires its children with ``add_if`` has already declared its edges, and a
    parallel ``internal_edges`` list could only restate them — or disagree with them.

    The lowering kind is the interface's TYPE, exactly as a boundary port's direction is its
    endpoint's type (:func:`kind_of_endpoint`): a :class:`~waveflow.hw.interface.StreamIF` is a FIFO,
    a :class:`~waveflow.hw.interface.StreamOfBlocksIF` is a ping-pong block channel whose element
    width and block length come from its ``element_type`` (the single source the SOBIF refactor made
    typed — restating them word-granularly on an edge is how the two drift apart).
    """
    from waveflow.hw.interface import StreamIF, StreamOfBlocksIF

    edges: list = []
    for iface in comp.interfaces.values():
        name = _edge_name(comp, iface)
        master = iface.endpoints.get("master")
        slave = iface.endpoints.get("slave")
        if master is None or slave is None:
            raise LoweringError(
                f"derive_internal_edges: interface {iface.name!r} on {type(comp).__name__} is not "
                f"bound on both sides (master={master!r}, slave={slave!r}) — an internal edge needs "
                f"both, so this is a wiring bug, not an edge.")
        if isinstance(iface, StreamOfBlocksIF):
            et = iface.element_type
            if et is None:
                raise LoweringError(
                    f"derive_internal_edges: SOBIF {iface.name!r} has no element_type, so its block "
                    f"width/length cannot be derived. Construct it with element_type=<DataArray>.")
            edges.append(SobEdge(name, master, slave, elem_bw=int(et.element_type.bitwidth),
                                 block_n=int(et.max_shape[0]), depth=int(iface.depth)))
        elif isinstance(iface, StreamIF):
            # A framed StreamIF lowers to a FramedEdge (framed_word FIFO, carries the packet
            # boundary); a plain one to a StreamEdge (ap_uint FIFO).  See plans/memstream_inband.md.
            # The channel's `depth` is single-source (pysim queue_size + this pragma).  `None` is
            # explicit-unbounded -- fine for pysim exploration, but a FIFO going to hardware must
            # have a depth, so reject it at the synthesis boundary rather than emit an unsized FIFO.
            depth = getattr(iface, "depth", DEFAULT_STREAM_DEPTH)
            if depth is None:
                raise LoweringError(
                    f"derive_internal_edges: internal channel {name!r} on {type(comp).__name__} has "
                    f"depth=None (explicit unbounded). An unbounded FIFO is not synthesizable — give "
                    f"the StreamIF a depth (default {DEFAULT_STREAM_DEPTH}), or keep it unbounded only "
                    f"for pysim exploration, not for codegen.")
            if getattr(iface, "framed", False):
                edges.append(FramedEdge(name, master, slave, depth=depth))
            else:
                edges.append(StreamEdge(name, master, slave, depth=depth))
        else:
            raise LoweringError(
                f"derive_internal_edges: no edge lowering for interface type "
                f"{type(iface).__name__} ({iface.name!r}). Add one here rather than hand-declaring "
                f"the edge, so every composite lowers the same way.")
    return edges


def derive_boundary(comp, names) -> tuple[tuple[str, object], ...]:
    """Pair *names* with *comp*'s boundary endpoints, **derived from the component graph**.

    A boundary port is simply a child endpoint that is *not* bound to one of this composite's own
    internal interfaces — the graph already knows which those are, and in what order, because
    ``add_endpoint`` records every port on its owner (insertion-ordered, exactly as ``add_comp``
    records children).  So the endpoints and their order are derived; only the external *names* are
    the declarer's to say, and they must be, because local port names collide: both ``MemRStream``
    and ``MemWStream`` call their AXI port ``m_mem``, and the top needs ``m_in`` / ``m_out``.

    Order is significant — :func:`bundle_map` assigns ``gmem`` bundles in boundary order — and it is
    the walk order (children in ``add_comp`` order, ports in ``add_endpoint`` order).
    """
    internal = {id(ep) for iface in comp.interfaces.values()
                for ep in iface.endpoints.values() if ep is not None}
    eps = [ep for child in comp.ordered_subcomps
           for ep in child.endpoints.values() if id(ep) not in internal]

    if len(names) != len(eps):
        got = ", ".join(getattr(e, "name", "?") for e in eps)
        raise LoweringError(
            f"{type(comp).__name__}.boundary names {len(names)} port(s) {tuple(names)!r} but the "
            f"graph has {len(eps)} unwired child endpoint(s): [{got}]. Every child endpoint not "
            f"bound to an internal interface is a boundary port, so either a name is missing or a "
            f"port was left unwired.")
    return tuple(zip(names, eps))


def _unpack_boundary(entry) -> tuple[str, object]:
    """A boundary entry is ``(name, endpoint)``. Nothing else is the declarer's to say.

    It used to be ``(name, ep, kind, bundle)``. Both extra fields are gone:

    * ``kind`` is the endpoint's TYPE (:func:`kind_of_endpoint`) — restating it could only disagree.
    * ``bundle`` is the assembler's allocation, and :func:`bundle_map` decides it by policy.

    Legacy 3- and 4-tuples are still accepted so old boundaries keep working, but a stated ``kind``
    must AGREE with the type, and a stated ``bundle`` must agree with the policy. Silently trusting a
    stated value is how a ``const`` pointer ends up on a port that gets written.
    """
    if len(entry) == 2:
        return entry[0], entry[1]
    name, ep = entry[0], entry[1]
    if len(entry) == 4:
        kind = entry[2]
        derived = kind_of_endpoint(ep)
        if kind != derived:
            raise LoweringError(
                f"boundary port '{name}' declares kind {kind!r} but its endpoint "
                f"({type(ep).__name__}) implies {derived!r}. The type is the source of truth — drop "
                f"the kind from the boundary entry, or construct the endpoint with the direction you "
                f"meant."
            )
    return name, ep


def bundle_map(boundary) -> dict[str, str]:
    """Assign an ``m_axi`` bundle to each memory port: **gmem0, gmem1, ... in declaration order.**

    A bundle is an allocation decision by whoever assembles the top, not a fact about the port — the
    same ``MemWStream.m_mem`` is ``gmem0`` standalone and ``gmem1`` inside ``MemCopy``. So it was the
    one field a boundary still had to state by hand. A policy deletes that field rather than
    relocating it, and unlike a hand-written value it cannot disagree with itself. It reproduces every
    value that was hand-written here (verified byte-identical).

    **The policy is one bundle per m_axi port**, so two ports CANNOT share a bundle. Nothing wants
    that today. If something ever does — sharing costs AXI ports and serialises the traffic, so it is
    a real design choice — it needs a way to be *asked for*, and that ask belongs on the assembler
    (which allocates), not back on the port. AXIS ports have no bundle; they are named for themselves.
    """
    n = 0
    out: dict[str, str] = {}
    for entry in boundary:
        name, ep = _unpack_boundary(entry)
        if kind_of_endpoint(ep) in ("maxi_read", "maxi_write"):
            out[name] = f"gmem{n}"
            n += 1
        if len(entry) == 4 and entry[3] is not None and out.get(name, None) != entry[3]:
            raise LoweringError(
                f"boundary port '{name}' declares bundle {entry[3]!r} but the assembler's policy "
                f"(gmem0, gmem1, ... in declaration order) gives {out.get(name)!r}. Drop the bundle "
                f"from the boundary entry; if the order is wrong, reorder the boundary."
            )
    return out


def _boundary_port(name: str, kind: str, width: int, bundle: str | None) -> ExtPort:
    """Map one boundary (name, kind, bundle) to its :class:`ExtPort` (decl + interface pragmas).

    *bundle* comes from :func:`bundle_map` (the assembler's policy) for the m_axi kinds, and is
    ``None`` for AXIS. There is deliberately no per-kind default here: a default would be a second
    place that decides bundles, and the two could drift.
    """
    if kind in ("axis_in", "axis_out"):
        return _axis_port(name, width, kind=kind)
    if kind in ("maxi_read", "maxi_write"):
        if bundle is None:
            raise LoweringError(f"_boundary_port: m_axi port {name!r} has no bundle (see bundle_map)")
        return _maxi_port(name, width, const=(kind == "maxi_read"), bundle=bundle)
    raise LoweringError(f"composite_top_spec: unknown boundary kind {kind!r} for port {name!r}")


def _int_channel(edge, width: int, ep_task: dict[int, int]) -> IntChannel:
    """Lower one internal edge to an :class:`IntChannel`.

    The kind is the edge's TYPE, exactly as ``derive_internal_edges`` derives the edge from the
    interface's type -- no table, no flag to keep in sync.  ``width`` is the payload ``W``: a
    ``framed`` channel's RTL net is one bit wider (``last`` on top, see
    :func:`waveflow.utils.vcd.split_framed_word`), and a ``sob`` channel carries its own element
    width."""
    if isinstance(edge, SobEdge):
        kind, payload = "sob", edge.elem_bw
    elif isinstance(edge, FramedEdge):
        kind, payload = "framed", width
    else:
        kind, payload = "stream", width
    return IntChannel(
        decl=edge.decl(width),
        name=edge.name,
        kind=kind,
        width=payload,
        master_task=ep_task.get(id(edge.master_ep)),
        slave_task=ep_task.get(id(edge.slave_ep)),
    )


def _kernel_task_of(sub, comp):
    """*sub*'s ``kernel_task()`` descriptor, or a :class:`LoweringError` naming the missing hook.

    ``kernel_task()`` is the realization hook of a module **inside** the cut: it says *"here is my
    pre-written ``hls::task`` body"* (or, for a generated leaf, is derived from the module itself).
    A module that does not declare one has no body to place in a top, and that is a *verdict* about
    this (module, cut) pair, not a bug — so it must read as one.  Without this the walk raised a bare
    ``AttributeError``, which :func:`~waveflow.build.codegen_check.check` correctly refuses to swallow.

    The message names the peer hook when it is present, because that is the actual diagnosis: a
    module carrying only ``bfm_model()`` is not un-realizable, it is realized on the *other* side of
    the cut.  See ``plans/design_cut.md``.
    """
    kt = getattr(sub, "kernel_task", None)
    if kt is None:
        outside = " It does declare bfm_model(), which is the realization hook for a module " \
                  "OUTSIDE the cut (an XSI testbench model) — so it belongs beside the top, not " \
                  "inside it." if declares_hook(sub, "bfm_model") else ""
        raise LoweringError(
            f"composite_top_spec: {type(sub).__name__} (a child of {type(comp).__name__}) declares "
            f"no kernel_task() hook, so it has no hls::task body to instantiate inside the top."
            f"{outside}"
        )
    return kt()


def composite_top_spec(comp, width: int = DEFAULT_MEM_DW) -> TopSpec:
    """Derive the composite :class:`TopSpec` from *comp*'s component/interface graph.

    Reads four things off the built parent, nothing hand-written per top:

    1. ``comp.internal_edges`` -> one :class:`IntChannel` per edge (its ``hls_thread_local`` decl —
       an ``hls::stream`` for a :class:`StreamEdge`, an ``hls::stream_of_blocks`` for a
       :class:`SobEdge` — plus the name, kind, width and the producer/consumer task it connects),
       and a map *endpoint -> edge name* (both the master and slave side resolve to the same name).
    2. ``comp.boundary`` -> the external ports (AXIS / ``m_axi`` bundles) and a map
       *endpoint -> top-port name*.
    3. ``comp.ordered_subcomps`` -> one ``hls::task`` per active child; each child's
       :meth:`~waveflow.hw.mem_stream.KernelTask` signature (endpoint attr names) is resolved through
       the two maps above to concrete call args, and its ``template_args`` are baked in.
    4. Optional ``comp.cmd_headers`` / ``comp.extra_includes`` -> extra ``#include``s for the top.

    Because the args and channel decls come from the graph, the standalone kernel (one node, no
    edges), the memcpy composite (stream edges), and the SOBIF toy (a block edge) all fall out of this
    *same* generator."""
    ep_arg: dict[int, str] = {}

    for edge in comp.internal_edges:
        ep_arg[id(edge.master_ep)] = edge.name
        ep_arg[id(edge.slave_ep)] = edge.name

    ports: list[ExtPort] = []
    bundles = bundle_map(comp.boundary)
    for entry in comp.boundary:
        name, ep = _unpack_boundary(entry)
        bundle = bundles.get(name)
        ep_arg[id(ep)] = name
        ports.append(_boundary_port(name, kind_of_endpoint(ep), width, bundle))

    # Tasks are built before channels so a channel can record WHICH task drives it: the producer
    # and consumer task indices are what turn a channel name into RTL net names
    # (`<producer_inst>_<ch>_write`), and only this loop knows which endpoint belongs to which task.
    tasks: list[TaskInst] = []
    ep_task: dict[int, int] = {}
    for sub in comp.ordered_subcomps:
        kt = _kernel_task_of(sub, comp)
        args: list[str] = []
        for attr in kt.signature:
            ep = getattr(sub, attr)
            arg = ep_arg.get(id(ep))
            if arg is None:
                raise LoweringError(
                    f"composite_top_spec: {type(sub).__name__}.{attr} is not wired to any internal "
                    f"edge or boundary port of {type(comp).__name__} — cannot resolve its task arg")
            args.append(arg)
            ep_task[id(ep)] = len(tasks)
        tasks.append(TaskInst(kt.task_fn, tuple(kt.template_args), tuple(args), kt.header))

    channels = tuple(_int_channel(edge, width, ep_task) for edge in comp.internal_edges)

    return TopSpec(
        top_name=comp.cpp_kernel_name,
        ports=tuple(ports),
        tasks=tuple(tasks),
        cmd_headers=tuple(getattr(comp, "cmd_headers", ())),
        channels=channels,
        extra_includes=tuple(getattr(comp, "extra_includes", ())),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def render_top(spec: TopSpec) -> str:
    """Emit the generated free-running ``ap_ctrl_none`` ``hls::task`` top .cpp for *spec*.

    Iterates *spec.ports* (external boundary), *spec.internal_streams* (``hls_thread_local`` FIFOs —
    empty for a standalone kernel), and *spec.tasks* (``hls::task`` instances at a baked concrete
    width).  No ``#define MEM_DW`` and no ``while`` — the width is a template argument and the
    ``hls::task`` runtime re-fires each single-firing body."""
    task_headers = tuple(dict.fromkeys(t.header for t in spec.tasks))  # unique, order-preserving
    includes = ["hls_task.h", "hls_stream.h"]
    lines = [
        f"// {spec.top_name}.cpp — GENERATED by waveflow (build/composite_gen.py::render_top).",
        "// DO NOT EDIT: regenerate instead.  Free-running (ap_ctrl_none) hls::task top, derived from",
        "// the component/interface graph — ports, pragmas, channels, one task per active child.",
        "// The task BODIES instantiated below are separate artifacts and are not generated here.",
        "// Verify via XSI — ap_ctrl_none Vitis cosim is unreliable.",
    ]
    lines += [f'#include "{h}"' for h in includes]
    lines.append("#include <ap_int.h>")
    lines.append('#include "memmgr.hpp"')
    lines += [f'#include "{h}"' for h in spec.extra_includes]   # e.g. hls_streamofblocks.h (SOBIF)
    lines += [f'#include "{h}"' for h in spec.cmd_headers]
    lines += [f'#include "{h}"' for h in task_headers]
    lines.append("")

    port_decls = ",\n".join(f"    {p.decl}" for p in spec.ports)
    lines.append(f"void {spec.top_name}(")
    lines.append(port_decls)
    lines.append(") {")
    for p in spec.ports:
        lines += list(p.pragmas)
    lines.append("#pragma HLS INTERFACE ap_ctrl_none port=return")
    for s in spec.internal_streams:               # empty for a standalone kernel
        lines.append(f"    {s}")
    for i, t in enumerate(spec.tasks):
        call_args = ", ".join(t.args)
        # A body with no template args is a plain function: `f<>` is a compile error on a
        # non-template, so the brackets appear only when there is something to put in them.
        # (Reachable since a GENERATED task body bakes its width when the endpoints were built
        # from an already-int()'d HwParam — nothing stays symbolic to template on.)
        targs = ", ".join(str(a) for a in t.template_args)
        fn = f"{t.task_fn}<{targs}>" if t.template_args else t.task_fn
        lines.append(
            f"    hls_thread_local hls::task t{i}({fn}, {call_args});")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Testbench graph -> XSI model resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BfmDual:
    """What a testbench must present against a DUT boundary port of one kind — its **dual**.

    A testbench port is never free: it is the *opposite role* of the DUT port it faces.  The DUT
    drives an AXIS output, so the TB answers as a slave; the DUT is the ``m_axi`` master, so the TB
    supplies the missing memory slave.  This records that pairing once, per ``(protocol, role)``.

    * ``protocol`` / ``role`` — the pair, spelled out.  They exist so a *missing* entry can be
      reported as "no BFM presents the **slave** role of **AXI4-Lite**" rather than as a ``KeyError``
      on a kind string.
    * ``model`` — the C++ class in :mod:`waveflow.build.xsi`, or ``None`` for a **known gap**: the
      protocol and role are real, and no model implements them yet.
    * ``participant_declares`` — whether the *participant* picks the concrete class.

    On that last field, which is the one asymmetry in the table.  For AXI-Stream the role still fixes
    the direction, but which specialization plays it is the participant's to say: a source is an
    ``AxisMaster``, a sink an ``AxisSlave``, and a peer whose *protocol behaviour* differs (one that
    never backpressures, say) is a third class in the same role.  For ``m_axi`` there is nothing to
    choose — a memory does not get to decide whether it is read or written; the DUT's port kind
    decides, and the participant supplies only the arena.
    """
    protocol: str
    role: str
    model: str | None
    participant_declares: bool = False


#: **The** protocol × role table: which BFM serves a DUT boundary port of each kind.
#:
#: Lifted out of a two-entry ``_SLAVE_FOR_KIND`` dict whose AXIS rows were *implicit* — they lived in
#: whatever class each participant happened to declare, so "which duals exist?" had no single answer
#: and a kind with no dual surfaced as a ``KeyError`` rather than as a named gap.
#:
#: Keys are :func:`kind_of_endpoint`'s vocabulary, so this table and the boundary-port lowering cannot
#: disagree about what kinds there are.  See ``docs/guide/build/bfm.md``.
BFM_DUALS: dict[str, BfmDual] = {
    "axis_in":       BfmDual("AXI4-Stream", "master", "AxisMaster", participant_declares=True),
    "axis_out":      BfmDual("AXI4-Stream", "slave", "AxisSlave", participant_declares=True),
    "maxi_read":     BfmDual("AXI4-MM", "read slave", "AxiMmReadSlave"),
    "maxi_write":    BfmDual("AXI4-MM", "write slave", "AxiMmWriteSlave"),
    # A port that is an AXI-MM *slave* would need the testbench to MASTER the bus into it.  No model
    # does that, and none is planned: in this flow the kernel is always the m_axi master and the
    # testbench always supplies the memory.  The row exists so a module that would need it gets that
    # sentence instead of a KeyError.
    "mm_slave":      BfmDual("AXI4-MM", "master", None),
    # THE KNOWN GAP.  A regmap / HostActivated DUT presents an AXI4-Lite control slave, and no model
    # in waveflow/build/xsi/ answers it — so such a DUT cannot be XSI-lowered at all today.  Recorded
    # here, as a row with no model, rather than in prose: "which duals exist" is one lookup, and the
    # hole is part of the answer.  Deferred to plans/design_cut.md S7.
    "axilite_slave": BfmDual("AXI4-Lite", "master", None),
}


def bfm_dual_class(kind: str, declared: str | None) -> str:
    """The C++ BFM class for a DUT boundary port of *kind*, given the participant's *declared* class.

    The one place the choice is made.  Raises a :class:`~waveflow.build.hwcodegen.LoweringError`
    naming the protocol and the role when there is no dual — which is what turns "this design cannot
    be XSI-lowered" from a ``KeyError`` into an answer.
    """
    dual = BFM_DUALS.get(kind)
    if dual is None:
        raise LoweringError(
            f"no BFM dual is registered for boundary kind {kind!r}, so nothing can drive or answer a "
            f"DUT port of that kind. Add a row to composite_gen.BFM_DUALS naming the protocol, the "
            f"role the testbench must present, and the model that implements it. Registered kinds: "
            f"{sorted(BFM_DUALS)}."
        )
    if dual.model is None:
        raise LoweringError(
            f"no BFM implements the {dual.role} role of {dual.protocol} (boundary kind {kind!r}), so "
            f"a DUT exposing such a port cannot be driven at RTL. This is a known gap, recorded in "
            f"composite_gen.BFM_DUALS; see plans/design_cut.md S7."
        )
    if dual.participant_declares and declared is not None:
        return declared
    return dual.model


@dataclass(frozen=True)
class BfmInst:
    """One model construction in the generated testbench.

    Two shapes, and which one applies is decided by :attr:`channel`:

    * a **boundary** model — ``<cls> <name>(sim.dut(), ports::<name>, <args>);`` — binds RTL pins;
    * a **peer** model — ``<cls> <name>(<channel>, <args>);`` — binds a
      :class:`ChannelInst` instead, because the port it would otherwise drive does not exist: both
      ends of its edge lie outside the cut.

    ...and a model may be **both at once**: a converter binds RTL pins on its fabric side and a
    channel on its RF side, in one object.  Which is why the leading arguments are *resolved* into
    :attr:`binds` rather than derived from a flag at render time.
    """
    cls: str
    name: str
    xsi_prefix: str                  # the RTL port prefix this model drives ("" for a peer model)
    args: tuple[str, ...] = ()       # extra C++ args after the resolved binds
    #: Init-time config to emit after construction as ``<name>.<field> = <expr>;`` -- one entry per
    #: DynParam the participant carries (field name, rendered C++ initializer).
    dyn_params: tuple[tuple[str, str], ...] = ()
    #: The first :class:`ChannelInst` variable this model binds, or ``None``.  **Informational** —
    #: rendering reads :attr:`binds`.  Kept because "does this model touch a behavioral edge?" is a
    #: question about the graph that callers ask, and recovering it from ``binds`` would mean
    #: pattern-matching C++ text.
    channel: str | None = None
    #: The leading constructor arguments, already resolved against the graph and in declaration
    #: order: ``("sim.dut()", "<ns>::<port>")`` per boundary port, one channel variable per edge
    #: port, concatenated across :attr:`BfmModel.ports`.  ``args`` follows.
    #:
    #: Resolved here rather than reconstructed by the renderer because the mapping is a property of
    #: the **graph**, which the renderer does not have: a model spanning two sides of the cut has no
    #: single rule the emitter could apply from the model's name alone.
    binds: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChannelInst:
    """One channel construction in the generated testbench: ``<cls> <name>(<args>);``

    The edge-side counterpart of :class:`BfmInst`, and it takes no ``sim.dut()`` because it binds no
    RTL: a behavioral edge sits *between two models*.  Declared before both of its peers, which is
    what puts its ``sample()`` first in every phase sweep and therefore makes the transfer
    order-independent — see ``waveflow/build/xsi/xsi_channel.h``.
    """
    cls: str
    name: str
    args: tuple[str, ...] = ()
    dyn_params: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TbSpec:
    """A generated XSI testbench harness, derived from a testbench composite's graph."""
    top_name: str                      # the DUT's top name (-> DESIGN_DLL, ports header)
    #: (cls, name, ctor-args, dyn_params) — e.g. the FlatMemory arena two m_axi bundles share.  Its
    #: DynParams (load_segs/dump_segs) attach here, not to the per-bundle models, so they emit once.
    shared: tuple[tuple[str, str, tuple[str, ...], tuple[tuple[str, str], ...]], ...] = ()
    models: tuple[BfmInst, ...] = ()
    #: Behavioral edges — one per TB interface with **neither** endpoint on the DUT boundary.  Empty
    #: for every design that has only boundary edges, which is every design built before this existed.
    channels: tuple[ChannelInst, ...] = ()


def _find_dut(tb):
    """The one child that is the DUT: it has a ``boundary`` (RTL ports); participants have
    ``bfm_model()``.  Not ``kernel_task`` — a composite DUT has none, only its children do.

    **The default cut, not the only one.**  Discovery works because today's graphs contain exactly
    one synthesizable child, which is a property of the examples rather than of the design: the whole
    point of ``plans/design_cut.md`` is that a graph of three modules can be cut in several places.
    So this is what :func:`tb_top_spec` falls back to when no ``dut=`` is named, and the refusal below
    is the honest one — *this* graph is ambiguous, name the cut you meant."""
    duts = [c for c in tb.ordered_subcomps if hasattr(c, "boundary")]
    if len(duts) != 1:
        raise LoweringError(
            f"{type(tb).__name__}: expected exactly one child with a `boundary` (the DUT); "
            f"found {[type(d).__name__ for d in duts]}. Discovery only works when the graph has one "
            f"synthesizable child; name the cut explicitly with tb_top_spec(tb, dut=...) when it has "
            f"more (or fewer) than one."
        )
    return duts[0]


def _resolve_dut(tb, dut):
    """Validate an explicitly named *dut* against *tb*'s graph.

    Accepts the child module itself or its ``sub_comps`` name.  Both are checked against the graph
    rather than trusted: a DUT that is not a child of this testbench would produce a harness whose
    models bind to ports no participant is wired to, and the failure would land thousands of cycles
    into an RTL run rather than here.
    """
    children = list(tb.ordered_subcomps)
    if isinstance(dut, str):
        by_name = {c.name: c for c in children}
        if dut not in by_name:
            raise LoweringError(
                f"{type(tb).__name__}: dut={dut!r} is not one of its sub-components "
                f"{sorted(by_name)}."
            )
        dut = by_name[dut]
    elif not any(c is dut for c in children):
        raise LoweringError(
            f"{type(tb).__name__}: dut={type(dut).__name__} is not a sub-component of this "
            f"testbench, so nothing in the graph wires participants to its ports."
        )
    if not hasattr(dut, "boundary"):
        raise LoweringError(
            f"{type(tb).__name__}: dut={type(dut).__name__} has no `boundary`, so it exposes no RTL "
            f"ports for the testbench to drive. A module on the far side of the cut declares "
            f"bfm_model() and is a participant, not the DUT."
        )
    return dut


#: The model library the ``bfm_model()`` hook resolves against.
_XSI_BFM_HEADER = "xsi_bfm.h"

#: Every header that may define a node model, in the order a reader would meet them.  More than one
#: because a model is grouped by **what it binds**, not by being a model: ``xsi_bfm.h`` holds the ones
#: that bind RTL pins and therefore need Vivado's ``xsi.h``; ``xsi_rf_block.h`` holds the file-backed
#: peers of a behavioral edge, which bind nothing and are gated under a plain ``g++``; ``xsi_rfdc.h``
#: holds the converters, which bind both.  Scanning the set rather than one file is what keeps that
#: split from silently shrinking the registry — a model in the "wrong" header would otherwise be
#: reported as not existing.
_XSI_MODEL_HEADERS = ("xsi_bfm.h", "xsi_rf_block.h", "xsi_rfdc.h")


def xsi_model_classes() -> frozenset[str]:
    """Every ``XsiSimObj`` subclass the C++ model library defines, read **from the library itself**.

    Not a Python list of names.  A list would be a second copy of the library's contents, and the two
    would drift the first time a model was added or renamed — the same shadow ``codegen_check``'s
    docstring forbids for extraction rules.  The header is the source, so a model that exists is
    found and one that does not cannot be faked.

    Only ``XsiSimObj`` subclasses count: ``XsiSim`` and ``Dut`` are in the header but are the
    simulator and its handle, not participants, and naming one as a ``bfm_model()`` would compile
    into nonsense rather than a model.
    """
    import re
    from pathlib import Path

    xsi = Path(__file__).resolve().parent / "xsi"
    found: set[str] = set()
    for name in _XSI_MODEL_HEADERS:
        text = (xsi / name).read_text(encoding="utf-8")
        found |= set(re.findall(r"^(?:class|struct)\s+(\w+)\s*:\s*public\s+XsiSimObj\b",
                                text, re.M))
    return frozenset(found)


def _xsi_type_headers() -> dict[str, str]:
    """``{declared type name: the header that declares it}`` across the model headers.

    Read from the headers rather than listed, for the same reason :func:`xsi_model_classes` is: a
    list would be a second copy of the library and would drift the first time a model moved.  Covers
    ``class`` / ``struct`` / ``using`` declarations, because a harness may name any of the three —
    ``RfdcAdcMaster`` is a class, ``RfdcFormat`` a struct, ``RfChannel`` an alias.
    """
    import re
    from pathlib import Path

    xsi = Path(__file__).resolve().parent / "xsi"
    out: dict[str, str] = {}
    for name in _XSI_MODEL_HEADERS:
        text = (xsi / name).read_text(encoding="utf-8")
        for m in re.findall(r"^(?:class|struct)\s+(\w+)\s*[:{]", text, re.M):
            out.setdefault(m, name)
        for m in re.findall(r"^using\s+(\w+)\s*=", text, re.M):
            out.setdefault(m, name)
    return out


def _harness_extra_includes(spec: "TbSpec") -> list[str]:
    """The framework headers *spec*'s named classes come from, beyond the ones always included.

    ``xsi_bfm.h`` and (when there are channels) ``xsi_channel.h`` are unconditional; anything else a
    model or channel names — a converter, an RF peer, the block message a channel is specialized on —
    has to be included or the harness does not compile.  Derived from the spec so that adding a model
    to a new header needs no generator change.
    """
    import re

    known = _xsi_type_headers()
    named: list[str] = []
    for text in ([m.cls for m in spec.models] + [c.cls for c in spec.channels]
                 + [a for m in spec.models for a in m.args]):
        for ident in re.findall(r"\w+", text):
            hdr = known.get(ident)
            if hdr is not None and hdr != _XSI_BFM_HEADER and hdr not in named:
                named.append(hdr)
    return named


def resolve_bfm_model(mod, crossing=None):
    """Resolve *mod*'s ``bfm_model()`` against the cut, or raise a
    :class:`~waveflow.build.hwcodegen.LoweringError` saying why it cannot be realized beside a top.

    This is the ``xsi_bfm_model`` **target's** rule set, and it lives here — beside the registry and
    the walk that consumes it — rather than in :mod:`waveflow.build.codegen_check`, which converts a
    raise into a verdict and knows no rules of its own.

    Four mechanically checkable things (``plans/design_cut.md``, "the predicate"):

    1. **The hook is declared.**  A module with no ``bfm_model()`` has no pre-written model; that is
       a finding, not an error the module had to anticipate.
    2. **The named class exists** in the C++ library (:func:`xsi_model_classes`).
    3. **Its ``ports`` cover every crossing endpoint.**  An uncovered port is a wire nobody drives —
       at RTL that is a hang thousands of cycles later with no diagnostic.
    4. **Every crossing endpoint has a dual** in :data:`BFM_DUALS`, and every ``DynParam`` the module
       carries renders to a C++ initializer.

    *crossing* names the endpoints that cross the cut, as attribute names.  ``None`` — the default —
    means **every registered endpoint**, which is the strictest and the only *cut-independent* answer:
    a module that passes under it can be placed outside any cut, so the verdict does not silently
    depend on a graph the caller did not supply.  Narrow it when asking about a specific build.

    What this deliberately does **not** check is item 5, behavioural equivalence between the Python
    body and the C++ model.  Nothing here can, and the existence of this function must not be read as
    covering it — see ``docs/guide/custom_hooks/bfm_model.md``.

    A module may declare **several** models (:func:`bfm_models`); every check above then applies per
    model, except coverage, which is the union.  The return is the **first**, which is all a
    single-model module ever had and all any caller uses — the verdict is the raise, not the value.
    """
    from waveflow.hw.hw_module import declares_hook, discover_dyn_params

    name = type(mod).__name__
    if not declares_hook(mod, "bfm_model"):
        raise LoweringError(
            f"{name} declares no bfm_model() hook, so it has no pre-written cycle model to place "
            f"beside a top. A module realized OUTSIDE the cut overrides bfm_model() to name one; a "
            f"module realized INSIDE the cut declares kernel_task() instead."
        )
    models = bfm_models(mod)

    known = xsi_model_classes()
    for bm in models:
        if bm.cls not in known:
            raise LoweringError(
                f"{name}.bfm_model() names the C++ class {bm.cls!r}, which is not an XsiSimObj in "
                f"{_XSI_BFM_HEADER}. Known models: {sorted(known)}."
            )

    attrs = list(_endpoint_attrs(mod)) if crossing is None else list(crossing)
    cross = {a: _bfm_port_endpoint(mod, a) for a in attrs}

    # Coverage is the UNION over every declared model: what matters is that no crossing endpoint is
    # left undriven, not which of a module's objects drives it.
    covered = {id(_bfm_port_endpoint(mod, a)) for bm in models for a in bm.ports}
    uncovered = sorted(a for a, ep in cross.items() if id(ep) not in covered)
    if uncovered:
        declared = ", ".join(f"{bm.cls}{list(bm.ports)}" for bm in models)
        raise LoweringError(
            f"{name}.bfm_model() ({declared}) leaves {uncovered} "
            f"uncovered. Every endpoint that CROSSES the cut needs a model, or the port is a wire "
            f"nobody drives — at RTL that is a hang with no diagnostic, thousands of cycles later. "
            f"If those endpoints do not cross this particular cut, say so with crossing=(...)."
            + (" (No cut was given, so the strictest one was used: every registered endpoint.)"
               if crossing is None else "")
        )

    # The dual is asked per endpoint, against the class of the model that actually spans it — with
    # several models the question "which class faces this port?" has a different answer per port,
    # which is the whole reason several exist.
    cls_of_ep = {id(_bfm_port_endpoint(mod, a)): bm.cls for bm in models for a in bm.ports}
    for attr, ep in cross.items():
        for facing in _facing_kinds(ep, mod, attr):
            bfm_dual_class(facing, cls_of_ep.get(id(ep)))

    for field, value in sorted(discover_dyn_params(mod).items()):
        try:
            _render_dyn_value(value)
        except LoweringError as e:
            raise LoweringError(
                f"{name}.{field} is a DynParam the harness would emit as "
                f"`<model>.{field} = ...;`, but it cannot be rendered: {e}"
            ) from e
    return models[0]


# ---------------------------------------------------------------------------
# The edge-side hook: xsi_model() -> a channel between two peer models.
# ---------------------------------------------------------------------------
#
# **A separate registry, not rows in BFM_DUALS**, and the reason is structural rather than tidiness:
# `BFM_DUALS` is keyed by the *DUT's boundary port kind*, because the DUT boundary is the spine the
# testbench walk iterates.  A model<->model edge has no DUT port kind — that is the definition of a
# behavioral edge — so the table cannot answer for it and a row would have nothing to key on.  What
# a channel needs answered is a different question with a different shape: "does the named C++
# channel class exist?"

#: The channel library the ``xsi_model()`` hook resolves against — the edge-side peer of
#: :data:`_XSI_BFM_HEADER`.
_XSI_CHANNEL_HEADER = "xsi_channel.h"


def xsi_channel_classes() -> frozenset[str]:
    """Every channel class the C++ library defines, read **from the library itself**.

    The same discipline as :func:`xsi_model_classes`, and for the same reason: a Python list of names
    would be a second copy of the header's contents and would drift the first time a channel was
    added or renamed.

    Tolerates a preceding ``template <...>`` line, because a channel is generic over what it carries
    while a bus model is not — that is the one syntactic difference between the two libraries.
    """
    import re
    from pathlib import Path

    hdr = Path(__file__).resolve().parent / "xsi" / _XSI_CHANNEL_HEADER
    text = hdr.read_text(encoding="utf-8")
    return frozenset(re.findall(
        r"^(?:template\s*<[^>\n]*>\s*\n)?(?:class|struct)\s+(\w+)\s*:\s*public\s+XsiSimObj\b",
        text, re.M))


def _channel_base_class(cls: str) -> str:
    """``"BlockChannel<uint64_t>"`` -> ``"BlockChannel"`` — the name to look up in the registry.

    A template *specialization* is a use of a class, not a class: the library defines
    ``BlockChannel``, and every edge names a different instantiation of it.  Checking the base is
    what makes "you named a channel that does not exist" catchable while leaving the payload type
    free.
    """
    return cls.split("<", 1)[0].strip()


def resolve_channel_model(iface):
    """Resolve *iface*'s ``xsi_model()``, or raise a
    :class:`~waveflow.build.hwcodegen.LoweringError` saying why the edge cannot be realized.

    The edge-side peer of :func:`resolve_bfm_model`, with the checks it can meaningfully make:

    1. **The hook is declared.**  An interface with no ``xsi_model()`` has no channel; that is a
       finding about the graph, not an error the interface had to anticipate.
    2. **The named class exists** in the C++ channel library (:func:`xsi_channel_classes`).
    3. **``peers`` names exactly this interface's two sides**, each of them bound.  A peer naming a
       side that does not exist, or an unbound one, is a channel with nothing on one end — at RTL
       that is a queue nobody fills or nobody drains, and the run simply produces nothing.
    4. **Every ``DynParam`` renders** to a C++ initializer.

    What it deliberately does **not** check — the same gap ``resolve_bfm_model`` documents — is that
    the C++ behaves like the Python ``run_proc``.  Nothing static can, which is exactly why the bar
    for what an edge may own is "obviously the same in ten lines".
    """
    from waveflow.hw.hw_module import declares_hook, discover_dyn_params

    name = f"{type(iface).__name__} '{getattr(iface, 'name', '?')}'"
    if not declares_hook(iface, "xsi_model"):
        raise LoweringError(
            f"{name} declares no xsi_model() hook, so this edge has no pre-written channel model. "
            f"An interface whose endpoints BOTH lie outside the cut is a behavioral edge and must "
            f"override xsi_model() to name one; an interface that crosses the cut is a boundary "
            f"port and takes a BFM dual instead."
        )
    cm = iface.xsi_model()

    known = xsi_channel_classes()
    if _channel_base_class(cm.cls) not in known:
        raise LoweringError(
            f"{name}.xsi_model() names the C++ channel {cm.cls!r}, whose class "
            f"{_channel_base_class(cm.cls)!r} is not an XsiSimObj in {_XSI_CHANNEL_HEADER}. "
            f"Known channels: {sorted(known)}."
        )

    sides = getattr(iface, "endpoints", {})
    if len(cm.peers) != 2:
        raise LoweringError(
            f"{name}.xsi_model() names {list(cm.peers)} as its peers; a channel connects exactly "
            f"two, the producer first. Multi-producer channels are deferred "
            f"(plans/behavioral_edges.md S5)."
        )
    for side in cm.peers:
        if side not in sides:
            raise LoweringError(
                f"{name}.xsi_model() names peer side {side!r}, which is not one of this "
                f"interface's sides {sorted(sides)}. ChannelModel.peers are endpoint SIDE names "
                f"(the keys of Interface.endpoints), not attribute names on a module."
            )
        if sides[side] is None:
            raise LoweringError(
                f"{name}.xsi_model() names peer side {side!r}, but nothing is bound to it — the "
                f"channel would have no model on that end, so at RTL it is a queue nobody fills or "
                f"nobody drains and the run produces nothing with no diagnostic."
            )

    for field, value in sorted(discover_dyn_params(iface).items()):
        try:
            _render_dyn_value(value)
        except LoweringError as e:
            raise LoweringError(
                f"{name}.{field} is a DynParam the harness would emit as "
                f"`<channel>.{field} = ...;`, but it cannot be rendered: {e}"
            ) from e
    return cm


#: A module's **own** endpoint kind → the DUT boundary-port kind(s) it faces across the cut.
#:
#: :data:`BFM_DUALS` is keyed by the *DUT's* port kind, because that is the spine the testbench walk
#: iterates.  Asking the question from the module's side needs this inversion, and it is an inversion
#: rather than a lookup because **a module presents the opposite role of the port it faces**: a module
#: that drives a stream faces a DUT that receives one.
#:
#: A memory slave faces *either* m_axi direction — it is the DUT's port kind that decides, not the
#: memory's — so both are required to have a dual, which is exactly the "a memory does not get to
#: choose whether it is read or written" rule stated from the other end.
#:
#: AXI-Lite maps to itself, and that is not a slip: there is no model in **either** role, so the one
#: row answers the question from both sides.
_FACING_KINDS: dict[str, tuple[str, ...]] = {
    "axis_out": ("axis_in",),
    "axis_in": ("axis_out",),
    "mm_slave": ("maxi_read", "maxi_write"),
    "maxi_read": ("mm_slave",),      # an m_axi master outside the cut would need the TB to be a slave
    "maxi_write": ("mm_slave",),
    "axilite_slave": ("axilite_slave",),
}


def _facing_kinds(ep, mod, attr: str) -> tuple[str, ...]:
    """What *ep* faces on the other side of the cut — a verdict, never a bare raise.

    ``kind_of_endpoint`` refuses an endpoint type it has no lowering for, which is right for the
    kernel walk and wrong here: "this endpoint type has no BFM" is precisely the answer
    ``xsi_bfm_model`` exists to give, so the refusal is re-framed rather than propagated.
    """
    try:
        kind = kind_of_endpoint(ep)
    except LoweringError as e:
        raise LoweringError(
            f"{type(mod).__name__}.{attr} ({type(ep).__name__}) has no boundary kind, so no BFM dual "
            f"can be looked up for it: {e}"
        ) from e
    facing = _FACING_KINDS.get(kind)
    if facing is None:
        raise LoweringError(
            f"{type(mod).__name__}.{attr} is kind {kind!r}, which has no entry in _FACING_KINDS — so "
            f"what it faces across the cut is unknown and no dual can be resolved for it."
        )
    return facing


def bfm_models(mod) -> tuple[BfmModel, ...]:
    """The models *mod* declares, always as a tuple.

    ``bfm_model()`` may return one :class:`BfmModel` or a sequence of them.  One is the common case
    and stays exactly what it was; several is what a module needs when its C++ realization is more
    than one object — see :class:`BfmModel` for the two reasons that turned out to be per-*path*
    rather than per-*module*.

    Normalizing here, rather than at each of the four call sites, is what keeps "a module declares a
    model" and "a module declares three" from being two code paths.
    """
    got = mod.bfm_model()
    models = tuple(got) if isinstance(got, (tuple, list)) else (got,)
    if not models:
        raise LoweringError(
            f"{type(mod).__name__}.bfm_model() returned no models. A module that declares the hook "
            f"must name at least one; a module with no C++ realization declares no hook at all, "
            f"which is a finding rather than an empty answer."
        )
    for m in models:
        if not isinstance(m, BfmModel):
            raise LoweringError(
                f"{type(mod).__name__}.bfm_model() returned {type(m).__name__}, not a BfmModel "
                f"(or a sequence of them)."
            )
    return models


def _resolve_model_binding(part, bm: BfmModel, ports_ns: str, boundary_of: dict,
                           channel_of: dict) -> tuple[tuple[str, ...], str, "str | None"]:
    """Resolve *bm*'s ports into constructor arguments — **each by its own kind**.

    This is the per-port half of the generalization.  A port is not asked "are you a boundary port?"
    as a property of the model; it is looked up in the graph, and *where its peer sits relative to
    the cut* decides what it contributes:

    ===========================  ==========================================
    the port's peer is...        it contributes
    ===========================  ==========================================
    a DUT boundary port          ``sim.dut(), <ns>::<port>``  (two arguments)
    on a behavioral edge         that edge's channel variable  (one argument)
    neither                      a refusal
    ===========================  ==========================================

    Returns ``(binds, xsi_prefix, channel)`` — the arguments, the first RTL prefix (informational,
    for traces), and the first channel variable (likewise).

    The refusal is the load-bearing case: a named port bound to nothing the harness can reach is a
    constructor argument that cannot be written, and guessing one would produce C++ that compiles
    against the wrong object or does not compile at all.
    """
    binds: list[str] = []
    prefix = ""
    channel: str | None = None
    for attr in bm.ports:
        ep = _bfm_port_endpoint(part, attr)
        if id(ep) in boundary_of:
            bname, bport = boundary_of[id(ep)]
            binds += ["sim.dut()", f"{ports_ns}::{bname}"]
            prefix = prefix or bport.xsi_prefix
        elif id(ep) in channel_of:
            chan = channel_of[id(ep)]
            binds.append(chan)
            channel = channel or chan
        else:
            raise LoweringError(
                f"{type(part).__name__}.bfm_model() model {bm.cls!r} names port {attr!r}, but that "
                f"endpoint is neither wired to a DUT boundary port nor bound to a behavioral edge — "
                f"so there is nothing for the model to bind there. Either wire it, or drop it from "
                f"this model's ports: a constructor argument the graph cannot supply is not "
                f"something the generator may guess."
            )
    return tuple(binds), prefix, channel


def _bfm_port_endpoint(part, attr: str):
    """*part*'s endpoint named by ``BfmModel.ports`` entry *attr*, validated against its registry.

    Two namespaces meet here, and reconciling them is what this function is for.  ``BfmModel.ports``
    are **attribute names** in the C++ model's constructor order (``"stream_ep"``) — they have to be,
    because constructor order is a fact about the C++ and nothing else records it.  ``add_endpoint``
    keys by ``endpoint.name`` (``"streamdriver3_stream_ep"``), which is a different string entirely.

    Before participants had an endpoint registry there was nothing to check against, so a renamed
    attribute produced a bare ``AttributeError`` — or worse, resolved to some *other* attribute that
    happened to exist and quietly modelled the wrong port.  Now that a participant is an
    :class:`~waveflow.hw.hw_module.HwModule` (``plans/design_cut.md`` S1) the registry exists, so the
    name can be checked at elaboration time and the failure names both namespaces.
    """
    ep = getattr(part, attr, None)
    if ep is None:
        raise LoweringError(
            f"{type(part).__name__}.bfm_model() names port {attr!r}, but the module has no such "
            f"attribute. BfmModel.ports are ATTRIBUTE names in the C++ model's constructor order; "
            f"this module's endpoint attributes are {sorted(_endpoint_attrs(part))}."
        )
    if id(ep) not in {id(e) for e in getattr(part, "endpoints", {}).values()}:
        raise LoweringError(
            f"{type(part).__name__}.bfm_model() names port {attr!r}, which is an attribute but was "
            f"never registered with add_endpoint — so it is not part of the module's surface and "
            f"nothing can resolve it through the graph. Registered endpoints: "
            f"{sorted(getattr(part, 'endpoints', {}))}."
        )
    return ep


def _endpoint_attrs(part) -> list[str]:
    """The **attribute** names under which *part*'s registered endpoints are reachable — what the
    error above needs in order to suggest what the author probably meant.

    Reads the instance ``__dict__`` only.  Walking ``dir(type(part))`` would evaluate every class
    property on the way past (``boundary``, ``internal_edges``, …), several of which raise by design —
    building an error message must not be able to raise a different error.
    """
    reg = {id(e) for e in getattr(part, "endpoints", {}).values()}
    return [a for a, v in vars(part).items() if id(v) in reg]


def _render_dyn_value(value) -> str:
    """Render a ``DynParam`` value as a C++ initializer for a ``<model>.<field> = <expr>;`` line.

    A value that knows its own C++ form provides ``to_cpp()`` (e.g. ``MemSeg``); a list/tuple becomes
    an aggregate initializer of its elements.  Extend the scalar cases per type as needed.
    """
    if hasattr(value, "to_cpp"):
        return value.to_cpp()
    if isinstance(value, (list, tuple)):
        return "{ " + ", ".join(_render_dyn_value(v) for v in value) + " }"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    # `repr` rather than `str`: a float's C++ initializer has to round-trip exactly, and str() drops
    # digits at the precisions a derived rate lands on (0.21333333333333335 -> 0.213333333333).
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return '"' + value + '"'
    raise LoweringError(
        f"DynParam value {value!r} ({type(value).__name__}) has no C++ rendering yet — "
        f"add a case to _render_dyn_value."
    )


def _dut_interior(dut) -> set:
    """``id()`` of *dut* and every module inside it — what "on the far side of the cut" means.

    A TB interface reaching one of these *without* touching a boundary port is reaching **inside**
    the synthesized top, which is a graph error rather than a behavioral edge: at RTL there is no
    such connection point.  Naming it needs the interior set, not just the DUT itself.
    """
    seen: set = set()
    stack = [dut]
    while stack:
        m = stack.pop()
        if id(m) in seen:
            continue
        seen.add(id(m))
        stack.extend(getattr(m, "sub_comps", {}).values())
    return seen


@dataclass(frozen=True)
class _BehavioralEdge:
    """One discovered behavioral edge: its interface, its channel model, and its bound peers."""
    iface: object
    model: ChannelModel
    chan: str
    sides: dict            # side name -> the endpoint bound there


def _discover_behavioral_edges(tb, dut, model_of_ep: dict) -> list[_BehavioralEdge]:
    """Find and validate the TB interfaces with *neither* endpoint on the DUT boundary.

    The first walk iterates ``dut.boundary`` — one model per RTL port — and that spine is what makes
    "did we cover every port?" structural.  It is also blind by construction: an edge with no DUT
    port on either end emits nothing, and is not rejected so much as *invisible*.  This closes that
    blind spot.

    **The two walks must not double-count.**  An interface with at least one endpoint on the DUT
    boundary is the existing case and stays there; this one claims only the rest.  The partition is
    on the interface, not on the model, so no edge can be claimed twice or missed.

    **Discovery is split from emission** because the boundary walk needs the answer *first*: a model
    that spans both sides of the cut has to resolve one of its ports to a channel variable, and that
    variable does not exist until the edges are known.  Emission then happens in
    :func:`_emit_behavioral_edges`, after the boundary walk has said which endpoints it already
    claimed.

    Returns an empty list for a graph whose edges all touch the DUT — which is every design that
    existed before behavioral edges did, hence byte-identical output for them.
    """
    # Through _unpack_boundary, not a bare 2-tuple unpack: a boundary entry may carry a third field,
    # and discovery now runs BEFORE the boundary walk, so a malformed entry would surface here as a
    # ValueError instead of there as the diagnosis the caller needs.
    boundary_eps = {id(_unpack_boundary(entry)[1]) for entry in dut.boundary}
    interior = _dut_interior(dut)
    children = {id(c) for c in tb.ordered_subcomps}

    edges: list[_BehavioralEdge] = []
    for iface in tb.interfaces.values():
        bound = {side: ep for side, ep in getattr(iface, "endpoints", {}).items() if ep is not None}
        if any(id(ep) in boundary_eps for ep in bound.values()):
            continue                                   # walk 1's case
        if len(bound) < 2:
            # Checked before resolving so the message is about the real defect. An interface with
            # nothing (or one thing) bound would otherwise be reported as "declares no xsi_model()",
            # which sends the reader to write a hook for an edge that is simply not wired.
            raise LoweringError(
                f"{type(tb).__name__}: interface '{iface.name}' has {len(bound)} bound endpoint(s) "
                f"({sorted(bound)}), so it connects nothing. Bind both sides, or drop it from the "
                f"graph — an edge in the testbench's interface list is a claim that two things are "
                f"connected."
            )
        cm = resolve_channel_model(iface)              # raises if this edge cannot be realized
        chan = _cpp_ident(iface.name)

        parts: dict[str, object] = {}
        for side in cm.peers:
            ep = bound[side]                           # resolve_channel_model proved it is bound
            part = getattr(ep, "comp", None)
            if part is None:
                raise LoweringError(
                    f"{type(tb).__name__}: interface '{iface.name}' side {side!r} is bound to an "
                    f"endpoint that belongs to no module (add_endpoint was never called), so there "
                    f"is nothing to realize on that end of the channel."
                )
            if id(part) in interior:
                raise LoweringError(
                    f"{type(tb).__name__}: interface '{iface.name}' reaches {type(part).__name__} "
                    f"INSIDE the DUT, but not through a boundary port. At RTL there is no such "
                    f"connection point — a testbench edge either meets a DUT boundary port or joins "
                    f"two modules outside the cut."
                )
            if id(part) not in children:
                raise LoweringError(
                    f"{type(tb).__name__}: interface '{iface.name}' side {side!r} belongs to "
                    f"{type(part).__name__}, which is not a sub-component of this testbench. An "
                    f"edge reaching neither a participant nor the DUT has nothing to emit against."
                )
            if not declares_hook(part, "bfm_model"):
                raise LoweringError(
                    f"{type(tb).__name__}: interface '{iface.name}' side {side!r} belongs to "
                    f"{type(part).__name__}, which declares no bfm_model() — so it is a pysim-only "
                    f"node and this graph has no XSI realization. That is a finding about the "
                    f"graph, not a defect in the module: build a different testbench for RTL, or "
                    f"give the module a model."
                )
            # NOTE: a module with endpoints on BOTH a DUT boundary port and this edge used to be
            # refused here.  It is now the RFDC's shape and it works: one model spans both, and
            # _emit_behavioral_edges below skips a side the boundary walk already claimed.
            #
            # What replaces that refusal is narrower and still true: the endpoint has to be NAMED by
            # one of the module's models.  A module can declare a model for its stream port and
            # simply not mention its edge port, and then there is no class to construct against this
            # channel — which used to be reachable only as a KeyError.
            if id(ep) not in model_of_ep:
                declared = ", ".join(f"{m.cls}{list(m.ports)}" for m in bfm_models(part))
                raise LoweringError(
                    f"{type(tb).__name__}: interface '{iface.name}' side {side!r} is "
                    f"{type(part).__name__}'s endpoint, but none of its declared models names that "
                    f"port — it declares {declared}. An endpoint on a behavioral edge needs a model "
                    f"to bind the channel, whether that is a model of its own or one that also "
                    f"spans a boundary port."
                )
            parts[side] = part

        edges.append(_BehavioralEdge(iface=iface, model=cm, chan=chan,
                                     sides={s: bound[s] for s in cm.peers}))
    return edges


def _emit_behavioral_edges(edges, model_of_ep: dict, claimed_eps: set,
                           dyn_of) -> tuple[list, list]:
    """Turn discovered edges into a channel each, plus a peer model per **unclaimed** side.

    A side is *claimed* when the boundary walk already emitted a model spanning that endpoint — the
    converter case, where one object binds RTL pins on its fabric side and this channel on its RF
    side.  Emitting a separate peer for it would construct a second object against the same edge and
    leave the two disagreeing about what crossed it.

    The channel itself is always emitted: it exists because the edge does, regardless of who binds
    it.  An edge with both sides claimed is two spanning models talking to each other, which is a
    perfectly good graph and needs no peers at all.
    """
    channels: list[ChannelInst] = []
    peers: list[BfmInst] = []
    for e in edges:
        channels.append(ChannelInst(e.model.cls, e.chan, e.model.extra_args, dyn_of(e.iface)))
        for side, ep in e.sides.items():
            if id(ep) in claimed_eps:
                continue
            part, bm, _key = model_of_ep[id(ep)]
            peers.append(BfmInst(bm.cls, f"{e.chan}_{side}", "", bm.extra_args,
                                 dyn_of(part), channel=e.chan, binds=(e.chan,)))
    return channels, peers


def _cpp_ident(name: str) -> str:
    """*name* as a C++ identifier — the generated variable for a channel.

    Interface names are Python-side and already identifier-shaped in every graph today; this is the
    guard for the day one is not, so a bad name is a build error rather than uncompilable C++.
    """
    if not name or not name.isidentifier():
        raise LoweringError(
            f"interface name {name!r} is not a valid C++ identifier, so no channel variable can be "
            f"emitted for it. Name the interface something identifier-shaped."
        )
    return name


def tb_top_spec(tb, dut=None) -> TbSpec:
    """Derive the XSI testbench harness from *tb*'s component/interface graph, cut at *dut*.

    **The cut is an argument.**  *dut* names the child that is synthesized — as the module or as its
    ``sub_comps`` name — and everything else in the graph becomes a testbench model beside it. That
    is what lets ONE graph produce more than one harness: which modules are inside the boundary is a
    property of the build, not of the classes (``plans/design_cut.md``).

    ``None`` (the default) discovers it with :func:`_find_dut`, which is exactly today's behaviour and
    stays the right answer whenever the graph has one synthesizable child.

    **The DUT's boundary is the spine.** Every RTL port the kernel exposes needs exactly one model to
    drive or answer it, so the walk iterates the DUT's boundary rather than the participants — that
    is what makes "did we cover every port?" structural instead of a review question.

    For each boundary port: find which participant is wired to it (through the testbench's own
    interfaces, by endpoint identity), then resolve the model. An AXIS port takes the participant's
    declared class (`AxisMaster`/`AxisSlave`); an ``m_axi`` port takes the class its *kind* implies,
    because the kernel is the master and the TB must supply the slave.

    Participants declaring ``shared`` (a `MemoryMod` -> one `FlatMemory` behind both bundles) are
    constructed once and passed by name.

    **There are two walks.**  The one above claims every interface that touches a DUT boundary port.
    A second, :func:`_behavioral_edge_walk`, claims the rest: an interface whose endpoints *both*
    lie outside the cut is a **behavioral edge**, and it emits a channel plus the two peer models
    bound to it rather than a BFM per port.  The partition is on the interface, so nothing is
    claimed twice, and an interface reaching neither a participant nor the DUT is an error rather
    than a silent no-op — which is what it used to be.
    """
    from waveflow.hw.hw_module import discover_dyn_params

    dut = _find_dut(tb) if dut is None else _resolve_dut(tb, dut)
    ports_ns = f"{dut.cpp_kernel_name}_ports"

    # endpoint identity -> (participant, the model spanning it, that model's identity).  Built over
    # EVERY declared model, so a module with one and a module with three walk the same path.
    owner: dict[int, object] = {}
    model_of_ep: dict[int, tuple] = {}
    for c in tb.ordered_subcomps:
        if c is dut or not declares_hook(c, "bfm_model"):
            continue
        for i, bm in enumerate(bfm_models(c)):
            for attr in bm.ports:
                ep = _bfm_port_endpoint(c, attr)
                owner[id(ep)] = c
                model_of_ep[id(ep)] = (c, bm, (id(c), i))

    # endpoint identity -> the other endpoints on its interface (so a DUT port finds its participant)
    peers: dict[int, list] = {}
    for iface in tb.interfaces.values():
        eps = [ep for ep in getattr(iface, "endpoints", {}).values()]
        for ep in eps:
            peers.setdefault(id(ep), []).extend(e for e in eps if e is not ep)

    def dyn_of(obj) -> tuple[tuple[str, str], ...]:
        return tuple((f, _render_dyn_value(v)) for f, v in sorted(discover_dyn_params(obj).items()))

    # Edges are discovered BEFORE the boundary walk: a model spanning both sides of the cut resolves
    # one of its ports to a channel variable, which does not exist until the edges are known.
    edges = _discover_behavioral_edges(tb, dut, model_of_ep)
    channel_of: dict[int, str] = {id(ep): e.chan for e in edges for ep in e.sides.values()}

    bundles = bundle_map(dut.boundary)
    # participant endpoint -> the DUT boundary port it faces.  Precomputed because a model's ports
    # are resolved as a set, not in the order the boundary happens to be walked.
    boundary_of: dict[int, tuple[str, ExtPort]] = {}
    for entry in dut.boundary:
        name, dep = _unpack_boundary(entry)
        port = _boundary_port(name, kind_of_endpoint(dep), 0, bundles.get(name))
        for peer in peers.get(id(dep), []):
            boundary_of.setdefault(id(peer), (name, port))

    shared: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    models: list[BfmInst] = []
    #: Endpoints a boundary-walk model already spans.  Emission of the edges consults it so a
    #: converter's RF port does not also get a separate channel peer.
    claimed_eps: set = set()
    #: Models already emitted, by identity.  A model spanning two boundary ports is ONE object; the
    #: second port it covers must not construct it again.  Shared models are exempt — there the same
    #: endpoint faces two bundles and two slave instances are exactly what is wanted.
    emitted: set = set()

    for entry in dut.boundary:
        name, ep = _unpack_boundary(entry)
        bundle = bundles.get(name)
        kind = kind_of_endpoint(ep)
        port = _boundary_port(name, kind, 0, bundle)      # width irrelevant: we want xsi_prefix
        part = pep = None
        for peer in peers.get(id(ep), []):
            if id(peer) in owner:
                part, pep = owner[id(peer)], peer
                break
        if part is None:
            raise LoweringError(
                f"{type(tb).__name__}: DUT boundary port '{name}' is not wired to any testbench "
                f"participant — nothing would drive it, and the run would hang on that port."
            )
        _p, bm, key = model_of_ep[id(pep)]
        # Init-time config: every DynParam the participant carries, rendered to a C++ initializer.
        dyn = dyn_of(part)
        if bm.shared is not None:
            # A shared object's DynParams (a memory's load/dump segs) attach to the shared entry so
            # they emit once as `<shared>.<field> = ...;`; the per-bundle slave models carry none.
            # `bm.cls` here is the SHARED object's class (a `FlatMemory` arena), never the per-port
            # model — so the dual is resolved from the boundary kind alone, with nothing declared.
            shared.setdefault(bm.shared, (bm.cls, bm.shared, bm.extra_args, dyn))
            cls = bfm_dual_class(kind, None)
            models.append(BfmInst(cls, name, port.xsi_prefix, (bm.shared,),
                                  binds=("sim.dut()", f"{ports_ns}::{name}")))
            continue
        if key in emitted:
            continue          # this boundary port is covered by a model already constructed
        binds, prefix, chan = _resolve_model_binding(part, bm, ports_ns, boundary_of, channel_of)
        models.append(BfmInst(bfm_dual_class(kind, bm.cls), name, prefix or port.xsi_prefix,
                              bm.extra_args, dyn, channel=chan, binds=binds))
        emitted.add(key)
        claimed_eps.update(id(_bfm_port_endpoint(part, a)) for a in bm.ports)

    channels, peer_models = _emit_behavioral_edges(edges, model_of_ep, claimed_eps, dyn_of)
    models.extend(peer_models)

    # Every emitted C++ identifier lives in one struct scope, so a collision would shadow rather than
    # fail to compile -- a model silently binding the wrong thing.  Checked once, over all three
    # sources of names, rather than trusted.
    emitted: dict[str, str] = {}
    for kind, nm in ([("shared", s[1]) for s in shared.values()]
                     + [("channel", c.name) for c in channels]
                     + [("model", m.name) for m in models]):
        if nm in emitted:
            raise LoweringError(
                f"{type(tb).__name__}: the generated harness would declare {nm!r} twice (as a "
                f"{emitted[nm]} and as a {kind}), and the second would shadow the first. Rename the "
                f"interface or the DUT port it collides with."
            )
        emitted[nm] = kind

    return TbSpec(top_name=dut.cpp_kernel_name, shared=tuple(shared.values()),
                  models=tuple(models), channels=tuple(channels))


def render_tb_harness(spec: TbSpec, ns: str | None = None) -> str:
    """Emit ``<top>_tb_harness.h`` — the testbench's participants, phases and fixed-N loop.

    This is the derivable half of an XSI testbench. What is left for the hand-written half is the
    *test*: what to put in memory, and what to check afterwards. The split is not arbitrary — the
    harness is a function of the component graph, and the golden is the thing the graph cannot know.

    **Why a fixed-N loop with no early termination**, and why that is the whole point: nothing here
    blocks, so there is no sequencing to schedule and the DUT's pipelining survives *by
    construction*. The source keeps offering commands the moment they are accepted, so the kernel is
    already on job j+1 while it is still storing job j. A loop that instead drove one job and awaited
    it would produce a correct result and destroy the overlap — silently.

    Completion time is NOT reported here: the sink timestamps its own words
    (``AxisSlave::cycle_of_word``). The loop's bound is a testbench constant, and conflating the two
    is how three of the four hand-written testbenches came to report a drain tail as latency.
    """
    # *ns* names this harness.  It defaults to the DUT's top, which is right when a DUT has one
    # testbench -- and every design had exactly one until an RF loopback and a DUT-alone gate came to
    # share `rf_pass_through`.  Two harnesses in one workspace need two namespaces and two include
    # guards, or the second silently takes the first's.
    ns = ns or f"{spec.top_name}_tb"
    ports_ns = f"{spec.top_name}_ports"
    guard = f"WAVEFLOW_GEN_{ns.upper()}_HARNESS_H"

    # A ctor param is a value the *test* must supply: a plain identifier that is not already a member
    # of the harness.  A literal ctor arg (e.g. an empty word vector "{}") is not an identifier, so it
    # is not a param; a shared arena or a channel is a member, so neither is one either.
    members = ({name for _cls, name, *_ in spec.shared} | {c.name for c in spec.channels})
    ctor_params: list[str] = []
    for m in spec.models:
        for a in m.args:
            if a.isidentifier() and a not in members and a not in ctor_params:
                ctor_params.append(a)

    lines = [
        f"#ifndef {guard}",
        f"#define {guard}",
        f"// {ns}_harness.h -- GENERATED by waveflow (build/composite_gen.py::render_tb_harness)",
        f"// from the {spec.top_name} testbench graph.  DO NOT EDIT: regenerate instead.",
        "//",
        "// The derivable half of an XSI testbench: which models drive which RTL ports, the three",
        "// per-cycle phases, and the run loop.  What it deliberately does NOT contain is the TEST --",
        "// what goes in memory and what is checked afterwards.  Those are the golden, and the graph",
        "// cannot know them.",
        '#include <cstdint>',
        '#include <string>',
        '#include <vector>',
        '#include "xsi_bfm.h"',
    ]
    # Only when the graph HAS a behavioral edge: a workspace with no channel should not carry a
    # dependency on the channel library, and every harness generated before this existed must stay
    # byte-identical.
    if spec.channels:
        lines.append('#include "xsi_channel.h"')
    # ...and whichever framework headers declare the classes this harness actually names.  Derived,
    # so a model living outside xsi_bfm.h is included because the spec names it, not because someone
    # remembered to.
    for hdr in _harness_extra_includes(spec):
        lines.append(f'#include "{hdr}"')
    lines += [
        f'#include "{ports_ns}.h"',
        "",
        f"namespace {ns} {{",
        "",
        "using namespace wfbfm;",
        "",
        "struct Harness {",
        "    // Declaration order IS construction order: the sim must exist before any model can",
        "    // resolve a port against it, and a shared arena before the slaves that serve from it.",
        "    XsiSim sim;",
    ]
    for cls, name, *_ in spec.shared:
        lines.append(f"    {cls} {name};")
    if spec.channels:
        lines.append("    // Behavioral edges. Declared BEFORE their peer models, so each channel's")
        lines.append("    // sample() commits before any peer observes it -- which is what makes the")
        lines.append("    // transfer independent of participant order (see xsi_channel.h).")
        for c in spec.channels:
            lines.append(f"    {c.cls} {c.name};")
    for m in spec.models:
        lines.append(f"    {m.cls} {m.name};")
    lines.append("    // Every participant by base pointer, in construction order (shared arenas")
    lines.append("    // first): the one list the five lifecycle phases iterate, mirroring how")
    lines.append("    // Simulation.run_sim() drives its SimObjs.")
    lines.append("    std::vector<wfbfm::XsiSimObj*> participants_;")

    params = ", ".join(f"const std::vector<uint64_t>& {p}" for p in ctor_params)
    sig = f"    explicit Harness(const std::string& wdb{', ' + params if params else ''})"
    inits = [f"sim({ports_ns}::DESIGN_DLL, wdb)"]
    for _cls, name, args, _dyn in spec.shared:
        inits.append(f"{name}({', '.join(args)})")
    for c in spec.channels:
        inits.append(f"{c.name}({', '.join(c.args)})")
    for m in spec.models:
        # `binds` is already resolved per port against the graph -- `(sim.dut(), ports::X)` for a
        # boundary port, a channel variable for a behavioral-edge port, and both in order for a model
        # that spans the cut.  The renderer does not re-derive it: which side of the cut a port sits
        # on is a fact about the graph, which is not in scope here.
        inits.append(f"{m.name}({', '.join(tuple(m.binds) + m.args)})")
    lines.append("")
    lines.append(sig)
    lines.append("      : " + ",\n        ".join(inits))
    lines.append("    {")
    lines.append("        // Every TB-driven input the models above do not themselves drive.  Absent")
    lines.append("        // names are skipped; an undriven input is X, and X on a handshake hangs.")
    lines.append(f"        sim.pin_low({ports_ns}::ZERO_PORTS, {ports_ns}::ZERO_PORTS_N);")
    lines.append("        // Register participants for the lifecycle phases (shared arenas first, so a")
    lines.append("        // memory's pre_sim runs before the models that serve from it).")
    for _cls, name, *_ in spec.shared:
        lines.append(f"        participants_.push_back(&{name});")
    for c in spec.channels:
        lines.append(f"        participants_.push_back(&{c.name});")
    for m in spec.models:
        lines.append(f"        participants_.push_back(&{m.name});")
    dyn_lines = [f"        {name}.{field} = {expr};"
                 for _cls, name, _args, dyn in spec.shared for field, expr in dyn]
    dyn_lines += [f"        {c.name}.{field} = {expr};"
                  for c in spec.channels for field, expr in c.dyn_params]
    dyn_lines += [f"        {m.name}.{field} = {expr};"
                  for m in spec.models for field, expr in m.dyn_params]
    if dyn_lines:
        lines.append("        // Init-time config (DynParams): each is a knob the pysim participant")
        lines.append("        // carries, emitted here as a member assignment (e.g. a model's bundle).")
        lines.extend(dyn_lines)
    lines.append("    }")

    # The five lifecycle phases, each iterating the one participant list.  A participant that does
    # not override a phase inherits XsiSimObj's no-op, so a passive memory costs only empty calls and
    # the per-cycle model order is unchanged from the old unrolled form.
    for phase in ("pre_sim", "sample", "update", "drive", "post_sim"):
        lines.append(f"    void {phase}() {{ for (auto* p : participants_) p->{phase}(); }}")

    lines += [
        "",
        "    /// Run exactly *n_cycles*.  No early termination: see the note in the file header --",
        "    /// nothing blocks, so the DUT's pipelining survives by construction.  Undersize n and",
        "    /// the caller's own completion check fails loudly rather than passing quietly.",
        "    void run(long n_cycles) {",
        "        pre_sim();                       // participants seed memory / load vectors",
        "        sim.reset([this]{ drive(); });",
        "        for (long c = 0; c < n_cycles; ++c) {",
        "            sim.clock_low();",
        "            sample();",
        "            sim.clock_high();",
        "            update();",
        "            drive();",
        "        }",
        "        post_sim();                      // participants dump results / collect metrics",
        "    }",
        "",
        "    void close() { sim.close(); }",
        "};",
        "",
        f"}}  // namespace {ns}",
        "",
        f"#endif  // {guard}",
    ]
    return "\n".join(lines) + "\n"


#: Channels of an m_axi bundle a testbench does NOT drive, by bundle kind.  The complement of what
#: the BFM's slave model drives: for a READ bundle the TB owns ARREADY/R*, so everything on the write
#: side (plus the R sidebands it does not source) is pinned low; for a WRITE bundle the TB owns
#: AWREADY/WREADY/B*, so the read side (plus the B sidebands) is pinned.  Pinned, not left floating:
#: an undriven input is X, and X on a handshake is a hang with no diagnostic.
_MAXI_UNDRIVEN = {
    "maxi_read": ("AWREADY", "WREADY", "BVALID", "BRESP", "BID", "BUSER",
                  "RRESP", "RID", "RUSER"),
    "maxi_write": ("ARREADY", "RVALID", "RDATA", "RLAST", "RRESP", "RID", "RUSER",
                   "BRESP", "BID", "BUSER"),
}

#: The AXI-Lite control slave Vitis creates for `offset=slave` m_axi ports.  Pinning it quiescent is
#: what makes every offset register read 0, i.e. element coordinates == byte addresses / BPW.
_CONTROL_UNDRIVEN = (
    "s_axi_control_AWVALID", "s_axi_control_AWADDR", "s_axi_control_WVALID", "s_axi_control_WDATA",
    "s_axi_control_WSTRB", "s_axi_control_ARVALID", "s_axi_control_ARADDR", "s_axi_control_RREADY",
    "s_axi_control_BREADY",
)


def render_ports_h(spec: TopSpec) -> str:
    """Emit ``<top>_ports.h`` — the testbench's port binding, derived from *spec*.

    **Why this exists.**  A TB must name the same RTL ports the top's ``#pragma HLS INTERFACE`` lines
    create.  Hand-written, those two lists drift silently: rename a bundle and the TB keeps compiling
    and starts hanging.  Deriving both from ONE ``TopSpec`` makes that drift impossible by
    construction — this is the same spec :func:`render_top` renders the pragmas from.

    Emits, per boundary port, the RTL name prefix a BFM model binds against (an AXIS port keeps its
    own name; an ``m_axi`` port is named after its bundle), plus ``ZERO_PORTS`` — every TB-driven
    input the models do not otherwise drive, derived from the bundle kinds rather than hand-listed.

    Deliberately NOT parsed out of the generated RTL or the csynth report: those are downstream of
    the thing the TB is supposed to be testing, so binding to them would make a broken kernel look
    self-consistent.
    """
    ns = f"{spec.top_name}_ports"
    guard = f"WAVEFLOW_GEN_{ns.upper()}_H"
    lines = [
        f"#ifndef {guard}",
        f"#define {guard}",
        f"// {ns}.h -- GENERATED by waveflow (build/composite_gen.py::render_ports_h) from the SAME",
        f"// TopSpec that emits {spec.top_name}'s interface pragmas.  DO NOT EDIT: regenerate instead.",
        "// One spec, two consumers -- so a testbench cannot drift from the kernel it drives.",
        "",
        f"namespace {ns} {{",
        "",
        f'static const char* const TOP        = "{spec.top_name}";',
        "",
        "// xelab -dll emits the platform's native shared library, so the name differs by OS.  The",
        "// conditional lives in the generated header rather than in the generator so ONE emitted",
        "// header serves a Windows and a Linux build of the same testbench.",
        "#ifdef _WIN32",
        f'static const char* const DESIGN_DLL = "xsim.dir/{spec.top_name}/xsimk.dll";',
        "#else",
        f'static const char* const DESIGN_DLL = "xsim.dir/{spec.top_name}/xsimk.so";',
        "#endif",
        "",
    ]
    for p in spec.ports:
        comment = p.kind if p.kind else "?"
        if p.bundle:
            comment += f" on {p.bundle}"
        lines.append(f'static const char* const {p.name:<8} = "{p.xsi_prefix}";   // {comment}')

    zero: list[str] = []
    if any(p.kind in ("maxi_read", "maxi_write") for p in spec.ports):
        zero.extend(_CONTROL_UNDRIVEN)
    for p in spec.ports:
        for ch in _MAXI_UNDRIVEN.get(p.kind, ()):
            zero.append(f"{p.xsi_prefix}_{ch}")

    lines += [
        "",
        "// Every TB-driven input the interface models above do not themselves drive.  Absent names",
        "// are skipped at bind time (XsiSim::pin_low), so this may name channels a given kernel",
        "// does not expose.",
        "static const char* const ZERO_PORTS[] = {",
    ]
    lines += [f'    "{z}",' for z in zero]
    lines += [
        "};",
        "static const int ZERO_PORTS_N = (int)(sizeof(ZERO_PORTS)/sizeof(ZERO_PORTS[0]));",
        "",
        f"}}  // namespace {ns}",
        "",
        f"#endif  // {guard}",
    ]
    return "\n".join(lines) + "\n"


def render_tb_main(spec: TbSpec, n_cycles: int, ns: str | None = None,
                   harness_header: str | None = None, wdb: str | None = None) -> str:
    """Emit ``<top>_bfm_tb.cpp`` — the whole TB ``main``, now derivable from the graph.

    Once every participant loads its inputs in ``pre_sim`` and dumps its outputs in ``post_sim``, the
    main is just: construct the generated harness, run a fixed number of cycles, close.  There is no
    golden here — correctness is checked in Python from the dumped bundles (memory arena + the sink's
    capture with per-word arrival cycles), so nothing example-specific remains in the C++.
    """
    # See render_tb_harness on *ns*: one DUT may carry more than one testbench.
    ns = ns or f"{spec.top_name}_tb"
    harness_header = harness_header or f"{spec.top_name}_tb_harness.h"
    wdb = wdb or f"{spec.top_name}_bfm.wdb"
    lines = [
        f"// {spec.top_name}_bfm_tb.cpp -- GENERATED by waveflow (build/composite_gen.py::render_tb_main)",
        f"// from the {spec.top_name} testbench graph.  DO NOT EDIT: regenerate instead.",
        "//",
        "// The whole TB main: construct the generated harness, run a fixed number of cycles (the",
        "// participants load their input bundles in pre_sim and dump their outputs in post_sim), then",
        "// close.  No golden here -- correctness is checked in Python from the dumped output bundles.",
        f'#include "{harness_header}"',
        "",
        "int main() {",
        f'    {ns}::Harness h("{wdb}");',
        f"    h.run({int(n_cycles)});",
        "    h.close();",
        "    return 0;",
        "}",
    ]
    return "\n".join(lines) + "\n"


def render_vectors_h(ns: str, scalars=None, arrays=None, note: str = "") -> str:
    """Emit a plain-C++ header of generated test vectors: ``<ns>.h``.

    **Why data, and not a generated packer.**  An XSI testbench is compiled by mingw g++ against the
    xsim headers ONLY — it cannot include the schema's own C++ (``copy_cmd.h`` needs ``ap_int.h`` /
    ``hls_stream.h``).  So a TB physically cannot call ``CopyCmd::write_stream``, which is why the
    hand-written ones re-implemented the packing (``src | dst<<32``) in C++ and why that
    re-implementation could drift from the schema silently.

    Emitting the **output** of the real ``DataSchema.serialize()`` sidesteps that: the schema stays
    the single source, and no second implementation exists to drift.  The alternative — generating a
    plain-C++ packer from the schema — would create exactly the second implementation we are trying
    to delete.

    Deliberately HLS-free: ``int`` / ``uint64_t`` only, so the header compiles in a host toolchain.

    *scalars* is ``{name: int}``; *arrays* is ``{name: (cpp_type, values)}``.
    """
    scalars = scalars or {}
    arrays = arrays or {}
    guard = f"WAVEFLOW_GEN_{ns.upper()}_H"
    lines = [
        f"#ifndef {guard}",
        f"#define {guard}",
        f"// {ns}.h -- GENERATED by waveflow (build/composite_gen.py::render_vectors_h).",
        "// DO NOT EDIT: regenerate instead.  Plain C++ (no HLS types) so a host-compiled testbench",
        "// can include it.  Command words are the output of the schema's own serialize() -- the",
        "// testbench never re-implements a packing rule, so it cannot drift from the schema.",
    ]
    if note:
        lines += [f"// {ln}" for ln in note.strip().splitlines()]
    lines += ["#include <cstdint>", "", f"namespace {ns} {{", ""]

    for k, v in scalars.items():
        lines.append(f"static const int {k} = {int(v)};")
    if scalars and arrays:
        lines.append("")
    for k, (cpp_t, vals) in arrays.items():
        vals = list(vals)
        body = ", ".join(
            (f"{int(v)}ULL" if cpp_t == "uint64_t" else str(int(v))) for v in vals
        )
        lines.append(f"static const {cpp_t} {k}[{len(vals)}] = {{ {body} }};")

    lines += ["", f"}}  // namespace {ns}", "", f"#endif  // {guard}"]
    return "\n".join(lines) + "\n"


def render_rtl_f(top_name: str, root) -> str:
    """Emit the ``xvlog`` file list (``rtl_<top>.f``) for *top*'s elaborated RTL.

    **Why generate it.**  This list was hand-maintained, and it names RTL modules explicitly — so a
    module rename silently invalidates it.  Combined with a cached ``xsimk.dll`` that is how a stale
    ``.f`` fakes a PASS: xvlog compiles a file set that no longer matches the design, xelab reuses
    what it already built, and the run goes green while proving nothing.  It bit us for real when a
    generated task body renamed ``..._s_r_xfer_msg_RAM`` to ``..._s_mr_xfer_msg_RAM`` (the body's
    local is ``mr``, not ``r``).

    *root* is the example directory holding ``<top>_proj/``; paths are emitted relative to the
    sibling ``xsi/`` directory the ``.f`` lives in.  Requires csynth to have run.
    """
    from pathlib import Path

    vdir = Path(root) / f"{top_name}_proj" / "solution1" / "syn" / "verilog"
    if not vdir.is_dir():
        raise FileNotFoundError(
            f"No elaborated RTL at {vdir} — run csynth for '{top_name}' before generating its .f"
        )
    names = sorted(p.name for p in vdir.glob("*.v"))
    if not names:
        raise FileNotFoundError(f"No .v files in {vdir} — csynth for '{top_name}' produced no RTL")
    return "".join(f"../{top_name}_proj/solution1/syn/verilog/{n}\n" for n in names)


#: The default csynth target when no platform is selected — the historical hardcoded values, kept so an
#: example built without a ``platform`` emits byte-identical TCL (and therefore identical RTL).
DEFAULT_PART = "xc7z020clg484-1"
DEFAULT_PERIOD_NS = 10


def tcl_target(config) -> tuple[str, float]:
    """The ``(part, period_ns)`` a csynth TCL should pin, taken from *config*'s resolved platform.

    This is the single-source link: a build that selected a :class:`~waveflow.calib.platform.Platform`
    synthesises for *its* part/clock (so the RTL the calibration measures is the RTL the platform's fit
    is valid for); a build with no platform falls back to the historical default."""
    info = getattr(config, "platform_info", None) if config is not None else None
    if info is None:
        return DEFAULT_PART, DEFAULT_PERIOD_NS
    part = info.part or DEFAULT_PART
    period = info.synth_period_ns or DEFAULT_PERIOD_NS
    return part, period


def render_tcl(top_name: str, extra_sources: tuple[str, ...] = (), *,
               part: str = DEFAULT_PART, period_ns: float = DEFAULT_PERIOD_NS) -> str:
    """Emit a csynth ``.tcl`` for ``vitis-run --mode hls --tcl`` (concrete width baked in, so the
    cflags carry only the include path — no ``-DMEM_DW``).

    *extra_sources* are additional ``.cpp`` paths (relative to the example root) to add to the
    project.  A self-contained hand-written task body needs none; a **generated** body whose
    ``@synthesizable`` hooks live in their own translation units needs each hook impl added here, or
    csynth cannot resolve them.

    *part* / *period_ns* pin the synthesis target — pass :func:`tcl_target` of the build's config to
    drive them from the selected platform; the defaults reproduce the historical TCL byte-for-byte."""
    extra = "".join(f"add_files {s} -cflags $cf\n" for s in extra_sources)
    period = int(period_ns) if float(period_ns).is_integer() else period_ns
    return f"""\
set part {{{part}}}
set cf "-I{INCLUDE_DIR}"
puts "WAVEFLOW_INFO: {top_name}"
open_project -reset {top_name}_proj
set_top {top_name}
add_files {GEN_DIR}/{top_name}.cpp -cflags $cf
{extra}open_solution -reset "solution1"
set_part $part
create_clock -period {period}
if {{[catch {{csynth_design}} res]}} {{ puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }}
puts "WAVEFLOW_CSYNTH_OK"
exit 0
"""
