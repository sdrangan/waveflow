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


def _boundary_port(name: str, kind: str, width: int, bundle: str | None) -> ExtPort:
    """Map one boundary (name, kind, bundle) to its :class:`ExtPort` (decl + interface pragmas)."""
    if kind in ("axis_in", "axis_out"):
        return _axis_port(name, width, kind=kind)
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
    edges), the memcpy composite (stream edges), and the SOBIF toy (a block edge) all fall out of this
    *same* generator."""
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
