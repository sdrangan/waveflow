"""rtl_gen.py — the ``rtl_module`` target: a module realized as **hand-written Verilog** beside the
generated top.

The third realization hook, and the resolver for its target.  Its two siblings already exist:

=====================  ==========================================  =====================================
hook                   declares                                    lands as
=====================  ==========================================  =====================================
``kernel_task()``      "my pre-written ``hls::task`` body is *X*"   a task **inside** the generated top
``rtl_module()``       "my pre-written **Verilog** is *Z*"          a module **beside** the generated top
``bfm_model()``        "my pre-written C++ cycle model is *Y*"      an ``XsiSimObj`` **beside** the design
=====================  ==========================================  =====================================

All three *declare* a pre-written artifact; **none extracts or generates one**.  Nothing in this
module writes Verilog: :class:`RtlModule` names a file that a human wrote and a simulator ran, and
:class:`~waveflow.build.rtl_steps.GenRtlStep` copies it verbatim.  A generator here would be
re-deriving verified code, which is the anti-goal this hook family is built around
(``plans/rtl_module.md``, "Not in scope").

**Why a module ever needs this.**  Vitis HLS has no notion of memory shared between processes: an
array crossing two ``hls::task`` bodies becomes a synchronizing PIPO channel — *silently*, with a
handshake that stalls the writer — and a single ``bram`` port used both ways is a hard dataflow
error.  So a buffer with two independent accessors cannot live inside a kernel.  It lives beside it,
in Verilog, and the wrapper joining the two is the design scope a resource estimate can be defined
against (csynth of the kernel alone reports no BRAM at all — the memory is invisible to it).

**Resolved, not derived.**  :func:`resolve_rtl_module` can check that the hook is declared, that the
file exists, that it declares a module of the named name with the ports the port map names, and that
every endpoint has a Verilog port mapping.  It can never check that the module's Python behaviour and
its Verilog agree — nothing static can, and the standing answer is a byte-identical vector gate.
See ``docs/guide/comp_codegen/rtl_module.md``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from waveflow.build.hwcodegen import LoweringError

#: Where the framework's own pre-written Verilog lives, beside the C++ task bodies and the XSI
#: harness it is the peer of.  Shipped as package data so a pip-installed user can build a design
#: containing one.
RTL_SRC_DIR = Path(__file__).resolve().parent / "rtl"


@dataclass(frozen=True)
class RtlModule:
    """The pre-written Verilog module a component is realized as — the RTL twin of
    :class:`~waveflow.build.composite_gen.TaskInst` and :class:`~waveflow.build.composite_gen.BfmModel`.

    * ``module`` — the Verilog module name, as written in the file.  It is what a wrapper
      instantiates, so it is declared rather than derived from the class name: the artifact owns its
      own identity.
    * ``files`` — the pre-written source file(s).  A bare name resolves against :data:`RTL_SRC_DIR`;
      an absolute path is taken as given (an example may ship its own ``.v``).
    * ``ports`` — ``{endpoint attribute name: {role: verilog port}}``.  The *roles* are fixed per
      endpoint kind by :func:`rtl_port_mapping`; the *port names* are facts about the file, which is
      why they are declared and not guessed.  This is the memory-side half of the port-name chain;
      the kernel-side half is :func:`~waveflow.build.composite_gen.bram_port_signals`.
    * ``params`` — Verilog parameter overrides for the instantiation, e.g. ``(("DW", 16),
      ("AW", 10))``.  Parameterizing is not generating: the file is copied byte-for-byte and the
      numbers ride on the instantiation.
    * ``clock`` — the module's clock port, tied to the kernel's ``ap_clk`` by the wrapper.  Named
      here because a hand-written module chose its own spelling.
    """

    module: str
    files: tuple[str, ...] = ()
    ports: dict = field(default_factory=dict)
    params: tuple[tuple[str, int], ...] = ()
    clock: str = "clk"


@dataclass(frozen=True)
class PortMapping:
    """What one endpoint **kind** must supply to be realizable as ports on a Verilog module.

    The mapping table's row type.  It starts with exactly one row (:class:`BramIFSlave`) on purpose:
    the hook is general, and the set of endpoint kinds that have a defined Verilog port mapping grows
    one verified kind at a time.  An unmapped kind is refused *by name* — see
    :func:`rtl_port_mapping`.
    """

    #: The signal roles the port map must name for this kind.  Roles, not port names: the names are
    #: the file's business, the roles are the protocol's.
    roles: tuple[str, ...]
    #: Whether the file must publish a read latency (:func:`rtl_read_latency`).  True for a memory,
    #: because the kernel-side ``latency=`` pragma is emitted from that same number and a
    #: disagreement shifts every read by a cycle with no diagnostic.
    needs_read_latency: bool = False


#: A BRAM port pair as Vitis drives it: an address, an enable, a write-data bus, a read-data bus and
#: a byte-write-enable.  ``Clk``/``Rst`` are deliberately absent — the witness leaves the kernel's
#: ``buf_*_Clk_A`` / ``_Rst_A`` outputs unconnected and clocks the memory from ``ap_clk`` directly,
#: which is the whole reason :attr:`RtlModule.clock` exists.
BRAM_ROLES = ("addr", "en", "din", "dout", "we")


def rtl_port_mapping(ep) -> PortMapping:
    """The Verilog port mapping for endpoint *ep*'s **kind**, or a refusal that names the kind.

    Dispatches on the endpoint *type*, the same call
    :func:`~waveflow.build.composite_gen.kind_of_endpoint` already makes for boundary kinds: the
    protocol is the type, not a tag beside it (``plans/endpoint_types_not_tags.md``).

    The refusal is the point.  Looking the kind up in a dict would raise ``KeyError:
    <StreamIFSlave object>`` from inside a walk, which says nothing about what is wrong or what would
    fix it.  A module with a stream endpoint is not *broken*; it is simply outside what has been
    proven to land on Verilog ports, and it deserves to be told that in those words.
    """
    from waveflow.hw.bram import BramIFSlave

    if isinstance(ep, BramIFSlave):
        return PortMapping(roles=BRAM_ROLES, needs_read_latency=True)
    raise LoweringError(
        f"{type(ep).__name__} has no Verilog port mapping, so a module carrying one cannot be "
        f"realized as 'rtl_module'. Mapped endpoint kinds: BramIFSlave. The hook is general and the "
        f"table grows one VERIFIED kind at a time (plans/rtl_module.md S1) — adding a row means "
        f"knowing the exact port names Vitis emits for that kind, which is a measurement, not a "
        f"guess."
    )


# ---------------------------------------------------------------------------
# Reading the artifact — the file is the source of truth about itself
# ---------------------------------------------------------------------------

#: ``module <name> #(<params>) ( <ports> );`` — the ANSI header.  The parameter block is optional.
_MODULE_HDR = re.compile(
    r"\bmodule\s+(?P<name>\w+)\s*(?:#\s*\((?P<params>[^)]*)\)\s*)?\((?P<ports>[^)]*)\)\s*;",
    re.S)

#: A published read latency: ``localparam READ_LATENCY = 1;`` (or ``parameter``).
_READ_LATENCY = re.compile(r"\b(?:local)?param\s+(?:integer\s+)?READ_LATENCY\s*=\s*(\d+)")

_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def rtl_source_paths(rtl: RtlModule) -> tuple[Path, ...]:
    """Resolve :attr:`RtlModule.files` to absolute paths, refusing one that does not exist.

    A declared-but-missing file is the failure this hook exists to make impossible to ship: the whole
    claim of ``rtl_module()`` is *"the artifact already exists and has been simulated"*, so a name
    pointing at nothing is not a build error to be found later — it is the declaration being false.
    """
    out: list[Path] = []
    for f in rtl.files:
        p = Path(f)
        if not p.is_absolute():
            p = RTL_SRC_DIR / p
        if not p.is_file():
            raise LoweringError(
                f"rtl_module() names the Verilog source {f!r} for module {rtl.module!r}, but no "
                f"such file exists (looked at {p}). The hook declares a PRE-WRITTEN artifact; "
                f"nothing generates it."
            )
        out.append(p)
    if not out:
        raise LoweringError(
            f"rtl_module() for module {rtl.module!r} names no source files. A declared RTL module "
            f"with no Verilog behind it is a module a build cannot place."
        )
    return tuple(out)


def verilog_module_ports(text: str, module: str) -> tuple[str, ...]:
    """The port identifiers of *module*'s ANSI header, in declaration order.

    Deliberately small: enough to answer *"does the file declare the ports the port map names?"* and
    nothing more.  A full Verilog parse is not wanted here — the file is authored and simulated
    elsewhere, and this is a spelling check between two artifacts that must agree, not a front end.
    """
    for m in _MODULE_HDR.finditer(_COMMENTS.sub("", text)):
        if m.group("name") != module:
            continue
        ports: list[str] = []
        for chunk in m.group("ports").split(","):
            names = re.findall(r"\w+", chunk)
            if names:
                ports.append(names[-1])      # `output reg [DW-1:0] a_dout` -> `a_dout`
        return tuple(ports)
    raise LoweringError(
        f"No Verilog module named {module!r} was found in the declared source. rtl_module() names "
        f"the module a wrapper will instantiate, so the name must be the one in the file."
    )


def rtl_read_latency(rtl: RtlModule) -> int | None:
    """The read latency the Verilog **publishes**, or ``None`` if it declares none.

    **This is the single source for latency, and the direction matters.**  The memory's read latency
    is a fact about the hand-written RTL — a registered-output BRAM is 2, this one is 1 — so the
    number lives in the file and the C++ ``#pragma HLS INTERFACE ... latency=N`` is *derived from
    it*.  Python holds no latency of its own to disagree with, which is the only arrangement in which
    the two provably cannot desynchronize.

    Why it is worth this much care: a mismatch does not fail.  It shifts every read by one cycle and
    the design runs, which is why the witness's testbench checks a *ramp* rather than a constant.

    ``localparam`` rather than ``parameter`` in the shipped memory, and that is a statement: the
    latency is a *published property* of the implementation, not a knob an instantiation may turn.
    """
    for p in rtl_source_paths(rtl):
        m = _READ_LATENCY.search(_COMMENTS.sub("", p.read_text(encoding="utf-8")))
        if m:
            return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# The target's rule set
# ---------------------------------------------------------------------------

def resolve_rtl_module(mod) -> RtlModule:
    """Resolve *mod*'s ``rtl_module()``, or raise a :class:`LoweringError` saying why it cannot be
    realized as Verilog beside the top.

    This is the ``rtl_module`` **target's** rule set, and it lives here — beside the descriptor and
    the step that consumes it — rather than in :mod:`waveflow.build.codegen_check`, which converts a
    raise into a verdict and knows no rules of its own.  The peer of
    :func:`~waveflow.build.composite_gen.resolve_bfm_model`, with the checks that are meaningful on
    this side:

    1. **The hook is declared.**  A module with no ``rtl_module()`` names no artifact; that is a
       finding, not an error the module had to anticipate.
    2. **Every endpoint has a Verilog port mapping** (:func:`rtl_port_mapping`) — refused by kind
       name, never by ``KeyError``.
    3. **Every endpoint is in the port map, with every role of its kind.**  An unmapped role is a
       wire nobody drives, which at RTL is a hang with no diagnostic.
    4. **The named file exists and declares that module with those ports** — the two artifacts agree
       on spelling, checked rather than assumed.
    5. **A kind that needs a read latency gets one from the file**, so the C++ pragma has a single
       source to be emitted from.

    What it deliberately does **not** check is behavioural equivalence between the Python model and
    the Verilog.  Nothing here can, and the existence of this function must not be read as covering
    it — the answer is a vector gate, as it is for ``bfm_model()``.
    """
    from waveflow.build.composite_gen import _endpoint_attrs
    from waveflow.hw.hw_module import declares_hook

    name = type(mod).__name__
    if not declares_hook(mod, "rtl_module"):
        raise LoweringError(
            f"{name} declares no rtl_module() hook, so it names no pre-written Verilog to "
            f"instantiate beside the top. A module realized as hand-written RTL overrides "
            f"rtl_module(); a module realized as a generated task inside the top declares "
            f"kernel_task() instead."
        )
    rtl = mod.rtl_module()
    if not isinstance(rtl, RtlModule):
        raise LoweringError(
            f"{name}.rtl_module() returned {type(rtl).__name__}, not an RtlModule. The hook declares "
            f"the artifact: its module name, its source file(s), and its endpoint -> port map."
        )

    endpoints = {a: getattr(mod, a) for a in _endpoint_attrs(mod)}
    for attr, ep in sorted(endpoints.items()):
        mapping = rtl_port_mapping(ep)          # (2) — raises, naming the kind
        pmap = rtl.ports.get(attr)
        if pmap is None:
            raise LoweringError(
                f"{name}.rtl_module() maps no Verilog ports for the endpoint {attr!r} "
                f"({type(ep).__name__}). Every endpoint must reach the module's ports, or it is a "
                f"port the wrapper cannot join. Declared: {sorted(rtl.ports)}."
            )
        missing = [r for r in mapping.roles if r not in pmap]
        if missing:
            raise LoweringError(
                f"{name}.rtl_module() maps endpoint {attr!r} ({type(ep).__name__}) without the "
                f"role(s) {missing}. A {type(ep).__name__} needs {list(mapping.roles)}; an unmapped "
                f"role is a wire nobody drives, which at RTL is a hang with no diagnostic."
            )

    declared_ports: set[str] = set()
    for path in rtl_source_paths(rtl):                                  # (4)
        text = path.read_text(encoding="utf-8")
        if re.search(rf"\bmodule\s+{re.escape(rtl.module)}\b", text):
            declared_ports |= set(verilog_module_ports(text, rtl.module))
    if not declared_ports:
        raise LoweringError(
            f"{name}.rtl_module() names the Verilog module {rtl.module!r}, but none of its declared "
            f"sources {list(rtl.files)} contains a module by that name."
        )
    if rtl.clock not in declared_ports:
        raise LoweringError(
            f"{name}.rtl_module() names the clock port {rtl.clock!r}, which module {rtl.module!r} "
            f"does not declare. Its ports are {sorted(declared_ports)}."
        )
    for attr, pmap in sorted(rtl.ports.items()):
        for role, port in sorted(pmap.items()):
            if port not in declared_ports:
                raise LoweringError(
                    f"{name}.rtl_module() maps {attr}.{role} to the Verilog port {port!r}, which "
                    f"module {rtl.module!r} does not declare. Its ports are "
                    f"{sorted(declared_ports)}."
                )

    if any(rtl_port_mapping(ep).needs_read_latency for ep in endpoints.values()):   # (5)
        if rtl_read_latency(rtl) is None:
            raise LoweringError(
                f"{name}.rtl_module() names Verilog that publishes no READ_LATENCY, but "
                f"{rtl.module!r} carries an endpoint whose kernel-side pragma is emitted with "
                f"`latency=N`. The two must come from ONE number, and the file is where it lives: "
                f"declare `localparam READ_LATENCY = <n>;`. A pragma latency that disagrees with the "
                f"memory shifts every read by a cycle, silently."
            )
    return rtl
