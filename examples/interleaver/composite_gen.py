"""composite_gen.py — the generic graph-derived composite-top generator (promoted from mem_copy.py).

``composite_top_spec(comp)`` walks a built hierarchical component's graph — ``ordered_subcomps``
(``add_comp``), ``internal_edges`` (``add_if``), and ``boundary`` ports — and derives the
:class:`~examples.interleaver.mem_stream_gen.TopSpec` for one free-running (``ap_ctrl_none``)
``hls::task`` top.  It resolves each sub-component's ``hls::task`` signature (endpoint attr names, from
:meth:`~waveflow.hw.mem_stream.KernelTask`) to a top-level port or an internal edge.

Edge kinds (the Phase-3 extension): an edge declares how it lowers.  A :class:`StreamEdge` emits an
``hls::stream`` FIFO (the P2 default); a :class:`SobEdge` emits an ``hls::stream_of_blocks<T[N], 2>``
(the SOBIF depth-2 ping-pong).  The walk + arg resolution are otherwise identical — the SOB branch is
exactly the one-line extension the P2 seam was built for.
"""
from __future__ import annotations

from dataclasses import dataclass

from examples.interleaver.mem_stream_gen import (
    DEFAULT_MEM_DW,
    ExtPort,
    TaskInst,
    TopSpec,
    _axis_port,
    _maxi_port,
)


# ---------------------------------------------------------------------------
# Internal-edge kinds — each knows how it lowers to a C++ channel declaration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StreamEdge:
    """A stream internal edge (the P2 default) -> ``hls_thread_local hls::stream<ap_uint<W> >``.

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
    depth-2 ping-pong).  This is the one branch Phase 3 adds to the P2 edge loop."""
    name: str
    master_ep: object
    slave_ep: object
    elem_bw: int
    block_n: int
    depth: int = 2

    def decl(self, width: int) -> str:
        return (f"hls_thread_local hls::stream_of_blocks<ap_uint<{self.elem_bw}>[{self.block_n}], "
                f"{self.depth}> {self.name};")


def _boundary_port(name: str, kind: str, width: int, bundle: str | None) -> ExtPort:
    """Map one boundary (name, kind, bundle) to its :class:`ExtPort` (decl + interface pragmas)."""
    if kind in ("axis_in", "axis_out"):
        return _axis_port(name, width)
    if kind == "maxi_read":
        return _maxi_port(name, width, const=True, bundle=bundle or "gmem0")
    if kind == "maxi_write":
        return _maxi_port(name, width, const=False, bundle=bundle or "gmem1")
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
    edges), the P2 memcpy (stream edges), and the P3 SOBIF toy (a block edge) all fall out of this
    *same* generator — the seam Phase 4 extends further with the word-granular Gather."""
    ep_arg: dict[int, str] = {}

    internal_streams: list[str] = []
    for edge in comp.internal_edges:
        ep_arg[id(edge.master_ep)] = edge.name
        ep_arg[id(edge.slave_ep)] = edge.name
        internal_streams.append(edge.decl(width))

    ports: list[ExtPort] = []
    for name, ep, kind, bundle in comp.boundary:
        ep_arg[id(ep)] = name
        ports.append(_boundary_port(name, kind, width, bundle))

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
