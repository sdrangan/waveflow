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
    * ``ports`` — the participant's endpoint attribute names, in constructor order.  Each is resolved
      through the TB graph to the RTL port prefix it ends up driving (see :func:`tb_top_spec`).
    * ``extra_args`` — literal C++ expressions appended after the resolved ports, e.g. the words an
      ``AxisMaster`` presents or the arena an ``AxiMmReadSlave`` serves.  They name things the
      hand-written half of the testbench declares.
    * ``shared`` — if set, this model is constructed once and *shared* by name rather than per
      endpoint (the arena behind two m_axi bundles is one ``FlatMemory``, not two).
    """
    cls: str
    ports: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()
    shared: str | None = None


@dataclass(frozen=True)
class TopSpec:
    """A generated free-running (``ap_ctrl_none``) ``hls::task`` top.  For a standalone kernel there
    is one task and no internal streams; a composite adds tasks + ``hls_thread_local`` streams (or
    ``stream_of_blocks``) wiring their internal edges, keeping the external ports the boundary."""
    top_name: str
    ports: tuple[ExtPort, ...]              # external interface ports (signature order)
    tasks: tuple[TaskInst, ...]
    cmd_headers: tuple[str, ...]            # command struct headers to include
    internal_streams: tuple[str, ...] = ()  # hls_thread_local decls (empty for a standalone kernel)
    extra_includes: tuple[str, ...] = ()    # extra system headers (e.g. hls_streamofblocks.h)


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
        if self.depth is not None:
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
    from waveflow.hw.memif import MMIFMaster, MMIFReadMaster, MMIFWriteMaster

    if isinstance(ep, MMIFReadMaster):
        return "maxi_read"
    if isinstance(ep, MMIFWriteMaster):
        return "maxi_write"
    if isinstance(ep, MMIFMaster):
        raise ValueError(
            f"{type(ep).__name__} '{getattr(ep, 'name', '?')}' does not declare a direction, so its "
            f"pointer cannot be lowered (const + stable, or plain?). Construct it as an "
            f"MMIFReadMaster or MMIFWriteMaster — the direction is the type."
        )
    if isinstance(ep, StreamIFSlave):
        return "axis_in"
    if isinstance(ep, StreamIFMaster):
        return "axis_out"
    raise ValueError(f"no boundary kind for endpoint type {type(ep).__name__}")


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
            raise ValueError(
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
            raise ValueError(
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
            raise ValueError(f"_boundary_port: m_axi port {name!r} has no bundle (see bundle_map)")
        return _maxi_port(name, width, const=(kind == "maxi_read"), bundle=bundle)
    raise ValueError(f"composite_top_spec: unknown boundary kind {kind!r} for port {name!r}")


def composite_top_spec(comp, width: int = DEFAULT_MEM_DW) -> TopSpec:
    """Derive the composite :class:`TopSpec` from *comp*'s component/interface graph.

    Reads four things off the built parent, nothing hand-written per top:

    1. ``comp.internal_edges`` -> one channel decl per edge (``edge.decl(width)`` — an ``hls::stream``
       for a :class:`StreamEdge`, an ``hls::stream_of_blocks`` for a :class:`SobEdge`), and a map
       *endpoint -> edge name* (both the master and slave side of an edge resolve to the same name).
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

    internal_streams: list[str] = []
    for edge in comp.internal_edges:
        ep_arg[id(edge.master_ep)] = edge.name
        ep_arg[id(edge.slave_ep)] = edge.name
        internal_streams.append(edge.decl(width))

    ports: list[ExtPort] = []
    bundles = bundle_map(comp.boundary)
    for entry in comp.boundary:
        name, ep = _unpack_boundary(entry)
        bundle = bundles.get(name)
        ep_arg[id(ep)] = name
        ports.append(_boundary_port(name, kind_of_endpoint(ep), width, bundle))

    tasks: list[TaskInst] = []
    for sub in comp.ordered_subcomps:
        kt = sub.kernel_task()
        args: list[str] = []
        for attr in kt.signature:
            ep = getattr(sub, attr)
            arg = ep_arg.get(id(ep))
            if arg is None:
                raise ValueError(
                    f"composite_top_spec: {type(sub).__name__}.{attr} is not wired to any internal "
                    f"edge or boundary port of {type(comp).__name__} — cannot resolve its task arg")
            args.append(arg)
        tasks.append(TaskInst(kt.task_fn, tuple(kt.template_args), tuple(args), kt.header))

    return TopSpec(
        top_name=comp.cpp_kernel_name,
        ports=tuple(ports),
        tasks=tuple(tasks),
        cmd_headers=tuple(getattr(comp, "cmd_headers", ())),
        internal_streams=tuple(internal_streams),
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
        targs = ", ".join(str(a) for a in t.template_args)
        lines.append(
            f"    hls_thread_local hls::task t{i}({t.task_fn}<{targs}>, {call_args});")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Testbench graph -> XSI model resolution
# ---------------------------------------------------------------------------

#: Which BFM model serves a DUT boundary port of each kind.  The AXIS entries are ``None`` because
#: the *participant* decides there (a driver is an `AxisMaster`, a sink an `AxisSlave`); the m_axi
#: entries are fixed because the kernel is the MASTER and the testbench must supply the missing
#: slave — a memory does not get to choose whether it is read or written.
_SLAVE_FOR_KIND = {
    "maxi_read": "AxiMmReadSlave",
    "maxi_write": "AxiMmWriteSlave",
}


@dataclass(frozen=True)
class BfmInst:
    """One model construction in the generated testbench: ``<cls> <name>(dut, "<prefix>", <args>);``"""
    cls: str
    name: str
    xsi_prefix: str                  # the RTL port prefix this model drives
    args: tuple[str, ...] = ()       # extra C++ args after the prefix
    #: Init-time config to emit after construction as ``<name>.<field> = <expr>;`` -- one entry per
    #: DynParam the participant carries (field name, rendered C++ initializer).
    dyn_params: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TbSpec:
    """A generated XSI testbench harness, derived from a testbench `CompositeComp`'s graph."""
    top_name: str                      # the DUT's top name (-> DESIGN_DLL, ports header)
    #: (cls, name, ctor-args, dyn_params) — e.g. the FlatMemory arena two m_axi bundles share.  Its
    #: DynParams (load_segs/dump_segs) attach here, not to the per-bundle models, so they emit once.
    shared: tuple[tuple[str, str, tuple[str, ...], tuple[tuple[str, str], ...]], ...] = ()
    models: tuple[BfmInst, ...] = ()


def _find_dut(tb):
    """The one child that is the DUT: it has a ``boundary`` (RTL ports); participants have
    ``bfm_model()``.  Not ``kernel_task`` — a CompositeComp DUT has none, only its children do."""
    duts = [c for c in tb.ordered_subcomps if hasattr(c, "boundary")]
    if len(duts) != 1:
        raise ValueError(
            f"{type(tb).__name__}: expected exactly one child with a `boundary` (the DUT); "
            f"found {[type(d).__name__ for d in duts]}. A testbench drives one kernel."
        )
    return duts[0]


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
    if isinstance(value, str):
        return '"' + value + '"'
    raise TypeError(
        f"DynParam value {value!r} ({type(value).__name__}) has no C++ rendering yet — "
        f"add a case to _render_dyn_value."
    )


def tb_top_spec(tb) -> TbSpec:
    """Derive the XSI testbench harness from *tb*'s component/interface graph.

    **The DUT's boundary is the spine.** Every RTL port the kernel exposes needs exactly one model to
    drive or answer it, so the walk iterates the DUT's boundary rather than the participants — that
    is what makes "did we cover every port?" structural instead of a review question.

    For each boundary port: find which participant is wired to it (through the testbench's own
    interfaces, by endpoint identity), then resolve the model. An AXIS port takes the participant's
    declared class (`AxisMaster`/`AxisSlave`); an ``m_axi`` port takes the class its *kind* implies,
    because the kernel is the master and the TB must supply the slave.

    Participants declaring ``shared`` (a `MemComponent` -> one `FlatMemory` behind both bundles) are
    constructed once and passed by name.
    """
    from waveflow.hw.hw_component import discover_dyn_params

    dut = _find_dut(tb)

    # endpoint identity -> the participant it belongs to
    owner: dict[int, object] = {}
    for c in tb.ordered_subcomps:
        if c is dut or not hasattr(c, "bfm_model"):
            continue
        for attr in c.bfm_model().ports:
            owner[id(getattr(c, attr))] = c

    # endpoint identity -> the other endpoints on its interface (so a DUT port finds its participant)
    peers: dict[int, list] = {}
    for iface in tb.interfaces.values():
        eps = [ep for ep in getattr(iface, "endpoints", {}).values()]
        for ep in eps:
            peers.setdefault(id(ep), []).extend(e for e in eps if e is not ep)

    shared: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    models: list[BfmInst] = []

    bundles = bundle_map(dut.boundary)
    for entry in dut.boundary:
        name, ep = _unpack_boundary(entry)
        bundle = bundles.get(name)
        kind = kind_of_endpoint(ep)
        port = _boundary_port(name, kind, 0, bundle)      # width irrelevant: we want xsi_prefix
        part = None
        for peer in peers.get(id(ep), []):
            if id(peer) in owner:
                part = owner[id(peer)]
                break
        if part is None:
            raise ValueError(
                f"{type(tb).__name__}: DUT boundary port '{name}' is not wired to any testbench "
                f"participant — nothing would drive it, and the run would hang on that port."
            )
        bm = part.bfm_model()
        # Init-time config: every DynParam the participant carries, rendered to a C++ initializer.
        dyn = tuple((f, _render_dyn_value(v))
                    for f, v in sorted(discover_dyn_params(part).items()))
        if bm.shared is not None:
            # A shared object's DynParams (a memory's load/dump segs) attach to the shared entry so
            # they emit once as `<shared>.<field> = ...;`; the per-bundle slave models carry none.
            shared.setdefault(bm.shared, (bm.cls, bm.shared, bm.extra_args, dyn))
            cls = _SLAVE_FOR_KIND.get(kind)
            if cls is None:
                raise ValueError(
                    f"{type(tb).__name__}: participant {type(part).__name__} is shared but boundary "
                    f"port '{name}' is kind {kind!r}, which names no slave model."
                )
            models.append(BfmInst(cls, name, port.xsi_prefix, (bm.shared,)))
        else:
            models.append(BfmInst(bm.cls, name, port.xsi_prefix, bm.extra_args, dyn))

    return TbSpec(top_name=dut.cpp_kernel_name, shared=tuple(shared.values()),
                  models=tuple(models))


def render_tb_harness(spec: TbSpec) -> str:
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
    ns = f"{spec.top_name}_tb"
    ports_ns = f"{spec.top_name}_ports"
    guard = f"WAVEFLOW_GEN_{ns.upper()}_HARNESS_H"

    shared_names = {name for _cls, name, *_ in spec.shared}
    # A ctor param is a value the *test* must supply: a plain identifier that is not a shared member.
    # A literal ctor arg (e.g. an empty word vector "{}") is not an identifier, so it is not a param.
    ctor_params: list[str] = []
    for m in spec.models:
        for a in m.args:
            if a.isidentifier() and a not in shared_names and a not in ctor_params:
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
    for m in spec.models:
        a = ", ".join(("sim.dut()", f"{ports_ns}::{m.name}") + m.args)
        inits.append(f"{m.name}({a})")
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
    for m in spec.models:
        lines.append(f"        participants_.push_back(&{m.name});")
    dyn_lines = [f"        {name}.{field} = {expr};"
                 for _cls, name, _args, dyn in spec.shared for field, expr in dyn]
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
        f'static const char* const DESIGN_DLL = "xsim.dir/{spec.top_name}/xsimk.dll";',
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


def render_tcl(top_name: str, extra_sources: tuple[str, ...] = ()) -> str:
    """Emit a csynth ``.tcl`` for ``vitis-run --mode hls --tcl`` (concrete width baked in, so the
    cflags carry only the include path — no ``-DMEM_DW``).

    *extra_sources* are additional ``.cpp`` paths (relative to the example root) to add to the
    project.  A self-contained hand-written task body needs none; a **generated** body whose
    ``@synthesizable`` hooks live in their own translation units needs each hook impl added here, or
    csynth cannot resolve them."""
    extra = "".join(f"add_files {s} -cflags $cf\n" for s in extra_sources)
    return f"""\
set part {{xc7z020clg484-1}}
set cf "-I{INCLUDE_DIR}"
puts "WAVEFLOW_INFO: {top_name}"
open_project -reset {top_name}_proj
set_top {top_name}
add_files {GEN_DIR}/{top_name}.cpp -cflags $cf
{extra}open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {{[catch {{csynth_design}} res]}} {{ puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }}
puts "WAVEFLOW_CSYNTH_OK"
exit 0
"""
