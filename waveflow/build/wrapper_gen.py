"""wrapper_gen.py — the **wrapper**: the generated kernel plus its hand-written RTL, as one module.

``plans/rtl_module.md`` S2.  A kernel that reaches a memory through ``mode=bram`` ports is only half
a design: the ports are real, but nothing is on the other end of them.  The wrapper is the other
half — it instantiates the kernel, instantiates each memory the graph declares through
:meth:`~waveflow.hw.hw_module.HwModule.add_rtl_mod`, and joins them.

**Three things this module is, in order of importance:**

1. **The design scope.**  What is inside the wrapper is what a resource estimate should count, and
   the memory *is* inside it.  ``csynth`` of the kernel alone reports **no BRAM at all** — the memory
   is invisible to it — so the wrapper is the first boundary an area number can even be defined
   against (``plans/resource_model.md``).
2. **The elaborated top.**  From here on the simulator elaborates the wrapper, not the kernel: the
   ``.f``, the snapshot and the shared library are all named for it.  What that buys is that the
   memory becomes *internal*, so a testbench sees only AXI-Stream — which is the whole reason S3 is
   small and the BFM library is untouched.
3. **Entirely mechanical.**  Both sides' port names are known at generate time — the kernel's from
   :func:`~waveflow.build.composite_gen.bram_port_signals`, the memory's from its declared port map —
   so there is nothing here to decide at emit time.  The witness's hand-written ``rx_top.v`` is 49
   lines, and this emits its equivalent.

**The A/B question, settled by the witness rather than rediscovered here.**  Vitis emits a *full
true-dual-port pair* per ``bram`` interface, so one kernel-side memory port presents four physical
ports.  The witness wires the **A half of each interface** and ties the B halves off, and that is
what this emitter does: two interfaces, two memory ports, four halves, of which two are used.

**Two conventions have to be reconciled here, and until 2026-08-24 one of them was not.**  The
kernel's ``Addr_A`` is a **byte** address — the generated RTL literally contains
``Addr_A_local = Addr_A_orig << 32'd3`` for a 64-bit array — while a word-addressed memory like
:class:`~waveflow.hw.bram.T2pBram` indexes ``mem[addr[AW-1:0]]``.  Joining them straight through
scales every address by the element's byte width, so a memory of ``N`` words is reachable only at
``N / (W/8)`` distinct locations and **everything above that aliases onto a live word, silently**.
That is what :func:`_bram_addr_shift` undoes, and the same pass widens the ``WEN`` wire to the byte
count Vitis actually drives (it was hard-coded at 2, correct only for a 16-bit word).

It went unnoticed because the scaling is *consistent*: a design that writes and reads through the
same scaled address round-trips perfectly right up to the point where the memory wraps.
The retired ``bram_toy`` filled 256 of 1024 words at 16 bits — byte addresses 0…510, no wrap — and was
therefore green either way, which is what a witness cannot prove and a wider design immediately can:
``examples/rf_shot_buf`` at 64 bits wrote 256 words into 1024 and got the second half back twice.
:func:`~waveflow.build.wrapper_gen.render_wrapper`'s output is now checked against the *actual*
generated RTL by ``tests/examples/test_rf_shot_buf_xsi.py``, so the shift is a measurement of what
Vitis emits rather than a belief about it.
"""
from __future__ import annotations

from dataclasses import dataclass

from waveflow.build.composite_gen import bram_port_signals, wrapper_name
from waveflow.hw.bram import bram_emits_b_half
from waveflow.build.hwcodegen import LoweringError
from waveflow.build.rtl_gen import resolve_rtl_module

#: Verilog port direction per boundary kind, from the kernel's point of view, as ``(signal, dir,
#: width-expr)`` where a width of 1 is a scalar.  Only the AXI-Stream kinds appear: a ``bram`` port is
#: never a wrapper port (it is joined inside), and ``m_axi`` in a wrapped design is future work — a
#: design that has one gets a clear refusal rather than a silently missing port.
_AXIS_SIGS = {
    "axis_in":  (("TDATA", "input", True), ("TVALID", "input", False), ("TREADY", "output", False)),
    "axis_out": (("TDATA", "output", True), ("TVALID", "output", False), ("TREADY", "input", False)),
}

#: The kernel-side role each memory-port role is wired to.  Mechanical, and it is the join the whole
#: wrapper exists to make: the memory's ``din`` takes the kernel's ``Din``, and the memory's ``dout``
#: drives the kernel's ``Dout``.
_ROLE_TO_VITIS = {"addr": "Addr", "en": "EN", "din": "Din", "dout": "Dout", "we": "WEN"}


def _bram_addr_shift(width: int) -> int:
    """Bits the kernel's ``Addr_A`` is scaled up by — ``log2(width / 8)``.

    Vitis addresses a ``mode=bram`` port in **bytes**: the generated task RTL contains
    ``Addr_A_local = Addr_A_orig << 32'd3`` for a 64-bit array, ``<< 1`` for a 16-bit one.  A
    word-addressed memory needs that undone, and the wrapper is where the two conventions meet.

    **Refused rather than guessed** for a width that is not a power-of-two byte count.  The scaling
    for such a width is not something this module knows, and the failure mode of guessing it is the
    one that motivated the function: an address that is wrong by a factor, aliasing high words onto
    low ones with no tool saying anything and a design that round-trips perfectly until it wraps.
    """
    w = int(width)
    if w < 8 or w % 8:
        raise LoweringError(
            f"a bram port {w} bits wide is not a whole number of bytes, and Vitis's byte-address "
            f"scaling for such a width is not something this emitter knows. Widen the port to a "
            f"byte multiple, or teach _bram_addr_shift the rule and gate it against real RTL.")
    nbytes = w // 8
    shift = nbytes.bit_length() - 1
    if (1 << shift) != nbytes:
        raise LoweringError(
            f"a bram port {w} bits wide is {nbytes} bytes, which is not a power of two; Vitis's "
            f"address scaling is a SHIFT, so this emitter cannot express it. Use a power-of-two "
            f"byte width.")
    return shift


@dataclass(frozen=True)
class MemInst:
    """One hand-written RTL module instantiated in the wrapper."""
    inst: str                              # instance name, e.g. "buf"
    module: str                            # Verilog module name, e.g. "bram_t2p"
    params: tuple[tuple[str, int], ...]    # #(.DW(16), .AW(10))
    clock: str                             # the module's clock port
    #: ``(memory port, expression)`` pairs, in declaration order.  Usually a bare wrapper wire; for
    #: the address and the write enable it is the adaptation between the kernel's convention and the
    #: memory's — see :func:`_bram_addr_shift` and the module docstring.
    conns: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class WrapperSpec:
    """The wrapper as data: its ports, the kernel instance, the memories, and the wires between.

    Answerable rather than only renderable, exactly as :class:`~waveflow.build.composite_gen.TopSpec`
    is: a test can ask *"is the A half wired and the B half tied off?"* without parsing Verilog.
    """
    name: str                                     # the wrapper module, e.g. "bram_access_top"
    kernel: str                                   # the kernel module it instantiates
    ports: tuple[tuple[str, str, int], ...]       # (port, direction, width) in declaration order
    mems: tuple[MemInst, ...] = ()
    #: Every wire the wrapper declares: ``(name, width)``.
    wires: tuple[tuple[str, int], ...] = ()
    #: Kernel connections: ``(kernel port, expression)``.  An empty expression is an unconnected
    #: output — legal, and what a tied-off B half's outputs are.
    kernel_conns: tuple[tuple[str, str], ...] = ()
    #: Constant drivers: ``(wire, literal)`` — the B-half ``Dout`` inputs the kernel never uses.
    tieoffs: tuple[tuple[str, str], ...] = ()

    @property
    def files(self) -> tuple[str, ...]:
        """The Verilog file names this wrapper needs beside it, wrapper first."""
        return (f"{self.name}.v",)


def _axis_ports(spec) -> list[tuple[str, str, int]]:
    """The wrapper's own ports: the clock, the reset, and every AXI-Stream pin of the kernel.

    Derived from the kernel's :class:`~waveflow.build.composite_gen.TopSpec`, which is the same spec
    the kernel's interface pragmas come from — so the wrapper cannot present a port set the kernel
    does not have.
    """
    ports: list[tuple[str, str, int]] = [("ap_clk", "input", 1), ("ap_rst_n", "input", 1)]
    for p in spec.pin_ports:
        sigs = _AXIS_SIGS.get(p.kind)
        if sigs is None:
            raise LoweringError(
                f"wrapper_spec: boundary port {p.name!r} is {p.kind!r}, which has no wrapper port "
                f"mapping. Only AXI-Stream ports can cross a wrapper today; an m_axi port in a "
                f"wrapped design needs its pass-through written and gated, not guessed."
            )
        for sig, direction, wide in sigs:
            ports.append((f"{p.name}_{sig}", direction, p.width if wide else 1))
    return ports


def wrapper_spec(comp, spec) -> WrapperSpec:
    """Derive the wrapper for *comp* from its RTL registries and the kernel's *spec*.

    *comp* is the elaborated composite; *spec* its :func:`~waveflow.build.composite_gen.composite_top_spec`.
    Every name here is already decided somewhere else, which is what makes this a derivation rather
    than a design:

    * the wrapper's ports are the kernel's non-``bram`` ports (:attr:`TopSpec.pin_ports`);
    * the kernel's memory pins are :func:`bram_port_signals` of the port name;
    * the memory's pins are its own declared port map (``rtl_module().ports``);
    * which kernel port meets which memory port is the ``add_rtl_if`` registry.

    Raises :class:`~waveflow.build.hwcodegen.LoweringError` when a ``bram`` boundary port is not
    joined to a memory — a wrapper with a dangling memory port would elaborate and then read zeros
    forever, which is the failure mode this whole path exists to make impossible.
    """
    if not getattr(comp, "rtl_mods", None):
        raise LoweringError(
            f"{type(comp).__name__} declares no RTL sub-modules (add_rtl_mod), so it has no wrapper: "
            f"the generated kernel IS the design. A wrapper exists only to join the kernel to "
            f"hand-written RTL beside it.")

    # kernel-side endpoint identity -> (memory module, its port map for that endpoint)
    joined: dict[int, tuple[object, dict]] = {}
    for iface in comp.rtl_ifs.values():
        master = iface.endpoints.get("master")
        slave = iface.endpoints.get("slave")
        if master is None or slave is None:
            raise LoweringError(
                f"{type(comp).__name__}: rtl interface {iface.name!r} is not bound on both sides, "
                f"so the wrapper cannot join anything to it.")
        mem = slave.comp
        rtl = resolve_rtl_module(mem)
        attr = next((a for a, v in vars(mem).items() if v is slave), None)
        if attr is None or attr not in rtl.ports:
            raise LoweringError(
                f"{type(mem).__name__}.rtl_module() has no port map for the endpoint bound to "
                f"{iface.name!r}; the wrapper cannot name the Verilog ports to connect.")
        joined[id(master)] = (mem, rtl.ports[attr])

    ports = _axis_ports(spec)
    wires: list[tuple[str, int]] = []
    kconns: list[tuple[str, str]] = [("ap_clk", "ap_clk"), ("ap_rst_n", "ap_rst_n")]
    tieoffs: list[tuple[str, str]] = []
    mem_conns: dict[int, list[tuple[str, str]]] = {}

    for p in spec.pin_ports:                      # AXIS: straight through, same names
        for sig, _dir, _wide in _AXIS_SIGS[p.kind]:
            kconns.append((f"{p.name}_{sig}", f"{p.name}_{sig}"))

    bram_ports = [p for p in spec.ports if p.kind == "bram"]
    ep_of_port = {name: ep for name, ep in (_unpack(e) for e in comp.boundary)}
    for p in bram_ports:
        ep = ep_of_port[p.name]
        if id(ep) not in joined:
            raise LoweringError(
                f"{type(comp).__name__}: boundary port {p.name!r} is a bram port that no rtl "
                f"interface joins to a memory. The wrapper would leave it dangling — the kernel "
                f"would read zeros forever and nothing would say so. Wire it with add_rtl_if.")
        mem, pmap = joined[id(ep)]
        # Which halves this port HAS, from the same declaration its pragma comes from.  A read-write
        # port carries `storage_type=ram_1p`, which does not declare a `_B` half at all -- and a
        # wrapper naming pins that are not there does not elaborate.
        b_half = bram_emits_b_half(ep.storage_type)
        sigs = bram_port_signals(p.name, ("A", "B") if b_half else ("A",))
        shift = _bram_addr_shift(p.width)
        for role, mem_port in sorted(pmap.items()):
            vitis = _ROLE_TO_VITIS.get(role)
            if vitis is None:
                raise LoweringError(
                    f"{type(mem).__name__}.rtl_module() maps the role {role!r}, which has no "
                    f"kernel-side signal. Known roles: {sorted(_ROLE_TO_VITIS)}.")
            wire = f"{p.name}_{role}_a"
            # The WEN wire is as wide as Vitis drives it -- one bit per BYTE of the word.  It was
            # hard-coded at 2, which is right only at 16 bits; at 64 the kernel's 8-bit WEN was
            # being truncated into it, which xelab reported as a bit-length warning and nothing
            # else read.
            width = p.width if role in ("din", "dout") else (
                32 if role == "addr" else max(1, p.width // 8) if role == "we" else 1)
            wires.append((wire, width))
            kconns.append((sigs[f"{vitis}_A"], wire))
            # The two conventions, reconciled at the join.  See the module docstring: the kernel's
            # address is in BYTES and the memory's is in WORDS, and a byte-lane mask is not a write
            # enable.  Expressions rather than extra wires, so the wrapper stays readable and every
            # net still carries the name of the kernel signal it comes from.
            expr = wire
            if role == "addr" and shift:
                expr = f"{wire} >> {shift}"
            elif role == "we":
                expr = f"|{wire}"
            mem_conns.setdefault(id(mem), []).append((mem_port, expr))
        # The B half, WHEN THERE IS ONE.  Under `ram_1wnr` Vitis emits it whether or not the kernel
        # uses it, so it must be connected or left explicitly dangling -- and its `Dout` is an INPUT
        # to the kernel, which must be driven or the elaboration carries an undriven net into the
        # design.  Under `ram_1p` it does not exist, and naming it is an xvlog error.
        #
        # That difference is the invariant working: the wrapper wires ONE physical memory port per
        # declared bram port, and a read-write port's pragma is what stops Vitis from wanting two.
        if b_half:
            for sig in ("Addr", "EN", "Din", "WEN"):
                kconns.append((sigs[f"{sig}_B"], ""))      # unused kernel outputs: left open
            tie = f"{p.name}_dout_b"
            wires.append((tie, p.width))
            tieoffs.append((tie, f"{p.width}'d0"))
            kconns.append((sigs["Dout_B"], tie))
        # Emitted here, after the B block, so a port that HAS a B half produces byte-identical
        # Verilog to before this branch existed -- the existing designs' wrappers must not move.
        for sig in ("Clk", "Rst"):
            kconns.append((sigs[f"{sig}_A"], ""))
            if b_half:
                kconns.append((sigs[f"{sig}_B"], ""))

    mems: list[MemInst] = []
    for name, mem in comp.rtl_mods.items():
        rtl = resolve_rtl_module(mem)
        conns = mem_conns.get(id(mem), [])
        if not conns:
            raise LoweringError(
                f"{type(comp).__name__}: RTL module {name!r} is declared with add_rtl_mod but no "
                f"rtl interface reaches it, so the wrapper would instantiate a memory nothing talks "
                f"to.")
        mems.append(MemInst(inst=_inst_name(comp, mem), module=rtl.module, params=rtl.params,
                            clock=rtl.clock, conns=tuple(conns)))

    return WrapperSpec(name=wrapper_name(spec.top_name), kernel=spec.top_name, ports=tuple(ports),
                       mems=tuple(mems), wires=tuple(wires), kernel_conns=tuple(kconns),
                       tieoffs=tuple(tieoffs))


def bram_hazard_manifest(comp, spec) -> dict:
    """The nets a read-during-write collision is **visible on**, named by the emitter that made them.

    ``bram_t2p.v`` asserts the invariant itself::

        if (a_en && |a_we && b_en && (a_addr[AW-1:0] == b_addr[AW-1:0]))
            $error("bram_t2p: read-during-write collision at addr %0d", a_addr[AW-1:0]);

    and in the XSI flow **nothing can read that** — RTL text output reaches neither stdout nor a log
    (``plans/bram_access.md``, *DECIDED 2026-08-25*).  So the condition is checked from the waveform
    instead, and this is the half that must not be guessed: *which net* carries each term.  Same
    argument as :meth:`~waveflow.build.composite_gen.TopSpec.trace_manifest` — codegen chose these
    names, so binding is exact, and a name that has moved fails loudly rather than matching nothing.

    The wires are the **wrapper's**, not the memory's, because a level-1 ``$dumpvars`` of the
    elaborated top sees the wrapper's own scope.  That is also why ``addr_shift`` is here: the
    wrapper hands the memory ``buf_w_addr_a >> 3``, so the byte→word conversion is part of reading
    the waveform correctly, exactly as it is part of driving the memory correctly.

    Returns ``{"top": <wrapper module>, "clock": "ap_clk", "memories": [...]}`` with one entry per
    memory that has **both** a write accessor and a read accessor — a memory only one side touches
    cannot have the hazard, and saying so is better than emitting an entry whose condition can never
    be true.

    A ``"readwrite"`` accessor fills the **write** role: it is the side that can drive ``we``, which
    is the term the scan tests.  That is exact rather than approximate here because
    :class:`~waveflow.hw.bram.T2pBram` refuses a writing port B, so the writer is always port A —
    the same asymmetry ``bram_t2p.v``'s own ``$error`` is written with.  If that ever changes, this
    mapping and that assertion have to change together.

    Raises
    ------
    LoweringError
        If a connection expression is not one this reader understands.  The emitter writes exactly
        three shapes (a bare wire, ``wire >> N``, ``|wire``); anything else means the vocabulary grew
        and the scan would silently bind to the wrong thing.
    """
    from waveflow.build.composite_gen import wrapper_name

    ep_of_port = {name: ep for name, ep in (_unpack(e) for e in comp.boundary)}
    # memory identity -> {access: {role: wire}}, plus the geometry the memory itself indexes with.
    per_mem: dict[int, dict] = {}
    for iface in comp.rtl_ifs.values():
        master, slave = iface.endpoints.get("master"), iface.endpoints.get("slave")
        if master is None or slave is None:
            continue
        port = next((n for n, ep in ep_of_port.items() if ep is master), None)
        if port is None:
            raise LoweringError(
                f"{type(comp).__name__}: the accessor end of rtl interface {iface.name!r} is not a "
                f"boundary port of the kernel, so no wrapper wire carries it and the hazard cannot "
                f"be read off a waveform.")
        mem = slave.comp
        entry = per_mem.setdefault(id(mem), {"inst": _inst_name(comp, mem),
                                             "module": resolve_rtl_module(mem).module,
                                             "addr_bits": int(mem.addr_bits)})
        role = "write" if str(slave.access) == "readwrite" else str(slave.access)
        if role in entry:
            raise LoweringError(
                f"{type(comp).__name__}: memory {entry['inst']!r} has two accessors that both act "
                f"as the {role!r} side, so the read-during-write scan cannot say which pair to "
                f"compare. The waveform scan names exactly two roles.")
        entry[role] = {
            "en": f"{port}_en_a",
            "we": f"{port}_we_a",
            "addr": f"{port}_addr_a",
            "dout": f"{port}_dout_a",
            "addr_shift": _bram_addr_shift(int(master.bitwidth)),
        }

    memories = [m for m in per_mem.values() if "write" in m and "read" in m]
    return {"version": 1, "top": wrapper_name(spec.top_name), "clock": "ap_clk",
            "memories": memories}


def _unpack(entry) -> tuple[str, object]:
    from waveflow.build.composite_gen import _unpack_boundary
    return _unpack_boundary(entry)


#: Verilog keywords and gate primitives a Python attribute name could innocently collide with.  Not
#: the whole language — the ones a memory or a datapath actually gets called.  ``buf`` is the reason
#: this exists: it is a *primitive gate*, so ``bram_t2p #(...) buf (...)`` is a syntax error, and the
#: attribute name that produced it (``self.buf``) is the most natural name for a buffer there is.
_VERILOG_RESERVED = frozenset({
    "buf", "bufif0", "bufif1", "not", "and", "or", "nand", "nor", "xor", "xnor", "notif0", "notif1",
    "module", "endmodule", "input", "output", "inout", "wire", "reg", "assign", "always", "initial",
    "begin", "end", "parameter", "localparam", "generate", "endgenerate", "function", "task",
    "posedge", "negedge", "case", "endcase", "if", "else", "for", "while", "signed", "table", "time",
})


def _inst_name(comp, mem) -> str:
    """The wrapper's instance name for *mem*: the attribute it is bound to on the composite.

    The attribute name is what the design calls it, so the RTL calls it that too — one name, and a
    waveform is readable against the Python without a translation table.

    Refused when that name is a Verilog keyword, by name and with the fix.  This is not a
    hypothetical: the first design written against this emitter called its memory ``self.buf``, which
    is a *primitive gate* in Verilog — the file elaborates as far as the instantiation and then dies
    on a syntax error that says nothing about Python.  Catching it here costs one lookup and turns a
    confusing xvlog message into a sentence.
    """
    name = next((attr for attr, val in vars(comp).items() if val is mem),
                getattr(mem, "name", "mem"))
    if name in _VERILOG_RESERVED:
        raise LoweringError(
            f"{type(comp).__name__} holds its RTL module in an attribute named {name!r}, which is a "
            f"Verilog keyword — the wrapper would emit `{name} (` and xvlog would fail on a syntax "
            f"error that mentions no Python. Rename the attribute (e.g. {name}_mem)."
        )
    return name


def render_wrapper(spec: WrapperSpec) -> str:
    """Emit the wrapper Verilog for *spec* — the equivalent of the witness's ``rx_top.v``."""
    lines = [
        f"// {spec.name}.v — GENERATED by waveflow (build/wrapper_gen.py::render_wrapper).",
        "// DO NOT EDIT: regenerate instead.",
        "//",
        f"// The design scope: the generated kernel `{spec.kernel}` plus the hand-written memories it",
        "// reaches through `mode=bram` ports.  The kernel cannot contain them (Vitis turns an array",
        "// shared between two tasks into a synchronizing PIPO channel, and refuses one port used both",
        "// ways), so they live beside it and this module is what joins them.",
        "//",
        "// From outside this looks like a kernel with only its AXI-Stream ports — the memories are",
        "// internal and invisible to any testbench.  It is also what a resource estimate should",
        "// count: csynth of the kernel alone never sees them.",
        "`timescale 1ns/1ps",
        f"module {spec.name} (",
    ]
    decls = [f"    {d:<7}{'' if w == 1 else f'[{w-1}:0] '}{n}" for n, d, w in spec.ports]
    lines.append(",\n".join(decls))
    lines.append(");")

    if spec.wires:
        lines.append("")
        for n, w in spec.wires:
            lines.append(f"    wire{'' if w == 1 else f' [{w-1}:0]'} {n};")

    lines += ["", f"    {spec.kernel} kernel ("]
    conns = [f"        .{p}({e})" for p, e in spec.kernel_conns]
    lines.append(",\n".join(conns))
    lines.append("    );")

    for m in spec.mems:
        params = ", ".join(f".{k}({v})" for k, v in m.params)
        head = f"    {m.module} #({params}) {m.inst} (" if params else f"    {m.module} {m.inst} ("
        lines += ["", head]
        mc = [f"        .{m.clock}(ap_clk)"] + [f"        .{p}({w})" for p, w in m.conns]
        lines.append(",\n".join(mc))
        lines.append("    );")

    if spec.tieoffs:
        lines.append("")
        lines.append("    // The B half, for each bram interface that HAS one: `storage_type=ram_1wnr`")
        lines.append("    // emits a full A/B pair whether or not the kernel uses both, so its Dout INPUT")
        lines.append("    // must be driven.  A `ram_1p` port (a read-write one) declares no B half at all.")
        for n, v in spec.tieoffs:
            lines.append(f"    assign {n} = {v};")

    lines += ["endmodule", ""]
    return "\n".join(lines)
