"""axi_xbar.py — AMD's ``axi_crossbar`` IP, generated standalone for XSI and synthesis.

``plans/mm_slave_adaptor.md`` decision D1: the crossbar the board runs is the crossbar XSI runs.
The crossbar is a contended resource, so a cycle count measured through a stand-in would not transfer
to the hardware.  This module produces the **real** IP from a Python description:

    cfg = AxiXbarConfig(name="xbar_2x2", n_si=2,
                        mi=[AxiXbarRange(0x0000_0000, 16), AxiXbarRange(0x0001_0000, 16)])
    ip = generate_axi_xbar(cfg, out_dir)       # one Vivado batch run, cached by config hash
    ip.sim_files, ip.include_dirs, ip.module   # what xvlog needs, and what to instantiate

**Why ``create_ip`` and not a hand-parameterized instantiation.**  The crossbar is plain, unencrypted
Verilog (``data/ip/xilinx/axi_crossbar_v2_1/hdl``), so instantiating its top directly would work —
but the top's name carries the IP's patch version (``axi_crossbar_v2_1_37_axi_crossbar`` in 2025.1),
and the packed ``C_*`` parameters (base IDs, connectivity masks, issuing/acceptance limits, the family
string) are what the IP's own configuration logic computes.  Restating that logic here would be a
second copy of it, free to drift from the one the board's block design runs.  ``create_ip`` takes
about 30 s in an in-memory project and is cached, so the cost is paid once per configuration.

**Address map.**  Each master-side (MI) slot decodes one range: ``base`` plus ``addr_width`` low bits
(a ``2**addr_width``-byte window, so the base must be aligned to it).  The crossbar forwards the
**full** address; a slave that wants a local offset masks it (the adaptor's front end does).

**IDs.**  The crossbar's MI-side ID carries the issuing SI's index (``C_S_AXI_BASE_ID``), so every
slave behind it must echo ``AWID``/``ARID`` on ``BID``/``RID``.  With ``id_width = 1`` and two SIs,
SI *k* is ID *k*.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AxiXbarRange:
    """One MI slot's decoded window: ``[base, base + 2**addr_width)`` (bytes)."""

    base: int
    addr_width: int

    def __post_init__(self) -> None:
        if not 12 <= self.addr_width <= 64:
            # The IP refuses windows below 4 KB (an AXI burst may not cross a 4 KB boundary, so a
            # smaller window could split one).  Saying so here beats a Vivado error 30 s later.
            raise ValueError(f"axi_crossbar windows are at least 4 KB (addr_width >= 12), "
                             f"got addr_width={self.addr_width}")
        if self.base % (1 << self.addr_width):
            raise ValueError(f"MI base 0x{self.base:x} is not aligned to its 2**{self.addr_width}-byte "
                             f"window")

    @property
    def size(self) -> int:
        return 1 << self.addr_width


@dataclass(frozen=True)
class AxiXbarConfig:
    """The crossbar's configuration — everything ``create_ip`` is told, and nothing else."""

    name: str
    n_si: int
    mi: tuple[AxiXbarRange, ...]
    data_width: int = 64
    addr_width: int = 32
    id_width: int = 1
    part: str = "xczu48dr-ffvg1517-2-e"

    def __post_init__(self) -> None:
        object.__setattr__(self, "mi", tuple(self.mi))
        if not 1 <= self.n_si <= 16 or not 1 <= len(self.mi) <= 16:
            raise ValueError("axi_crossbar supports 1..16 SI and 1..16 MI slots")
        if self.n_si == 1 and len(self.mi) == 1:
            # Measured 2026-10-02 (Vivado 2025.1): create_ip accepts NUM_SI=1/NUM_MI=1 and silently
            # generates C_NUM_MASTER_SLOTS=2 with address parameters for ONE slot -- 2-bit MI ports, an
            # inconsistent IP, and an XSI run that crashes.  A 1x1 crossbar routes nothing; connect the
            # master and slave directly, or give the crossbar a second slot.
            raise ValueError("a 1x1 axi_crossbar is degenerate (create_ip generates an inconsistent "
                             "2-MI IP for it); connect directly or add a second SI/MI slot")
        if (1 << self.id_width) < self.n_si:
            # The response route back to an SI is its ID; fewer ID values than SIs cannot name them.
            raise ValueError(f"id_width={self.id_width} cannot distinguish {self.n_si} SIs")
        spans = sorted((r.base, r.base + r.size) for r in self.mi)
        for (a0, a1), (b0, _b1) in zip(spans, spans[1:]):
            if b0 < a1:
                raise ValueError(f"MI windows overlap at 0x{b0:x}")

    @classmethod
    def from_crossbar(cls, xbar, name: str, *, addr_width: int = 32, id_width: int = 1,
                      part: str | None = None) -> "AxiXbarConfig":
        """The RTL crossbar for a pysim ``AXIMMCrossBarIF``: one master-side (MI) slot per bound
        slave, at the base and size ``assign_address_ranges`` gave it -- so a slave's address is
        written once, in Python, and both backends decode it (``plans/bus_address_map.md`` D4).

        Each range must be a power of two of at least 4 KB, aligned to its size, as the IP requires.
        A crossbar with one slave gets a second, unused slot (a 1 x 1 ``axi_crossbar`` is refused --
        see the class): 4 KB at the first 64 KB boundary past the last range.
        """
        mi: list[AxiXbarRange] = []
        for k in range(int(xbar.nports_slave)):
            ep = xbar.endpoints.get(f"slave_{k}")
            if ep is None or ep.addr_range is None:
                raise ValueError(f"{xbar.name}: slave_{k} is unbound or has no address range; call "
                                 f"assign_address_ranges first")
            base, size = int(ep.addr_range.base_addr), int(ep.addr_range.size)
            aw = size.bit_length() - 1
            if size != 1 << aw:
                raise ValueError(f"{xbar.name}: slave_{k}'s range 0x{size:x} is not a power of two")
            mi.append(AxiXbarRange(base, aw))
        if len(mi) == 1:
            end = mi[0].base + mi[0].size
            mi.append(AxiXbarRange(-(-end // 0x1_0000) * 0x1_0000, 12))
        kw = {} if part is None else {"part": part}
        return cls(name=name, n_si=int(xbar.nports_master), mi=mi, data_width=int(xbar.bitwidth),
                   addr_width=addr_width, id_width=id_width, **kw)

    def digest(self) -> str:
        """A short hash of the configuration: the cache key for the generated IP."""
        blob = json.dumps({**asdict(self), "mi": [asdict(r) for r in self.mi]}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def tcl(self, ip_dir: str) -> str:
        """The batch script: an in-memory project, ``create_ip``, the config, ``generate_target``."""
        props = [f"CONFIG.NUM_SI {{{self.n_si}}}", f"CONFIG.NUM_MI {{{len(self.mi)}}}",
                 f"CONFIG.DATA_WIDTH {{{self.data_width}}}", f"CONFIG.ADDR_WIDTH {{{self.addr_width}}}",
                 "CONFIG.PROTOCOL {AXI4}", f"CONFIG.ID_WIDTH {{{self.id_width}}}"]
        for k, r in enumerate(self.mi):
            props.append(f"CONFIG.M{k:02d}_A00_BASE_ADDR {{0x{r.base:016x}}}")
            props.append(f"CONFIG.M{k:02d}_A00_ADDR_WIDTH {{{r.addr_width}}}")
        body = " \\\n  ".join(props)
        return (
            f"create_project -in_memory -part {self.part}\n"
            "set_property target_language Verilog [current_project]\n"
            f"file mkdir {{{ip_dir}}}\n"
            f"create_ip -name axi_crossbar -vendor xilinx.com -library ip -module_name {self.name} "
            f"-dir {{{ip_dir}}}\n"
            f"set_property -dict [list \\\n  {body} \\\n] [get_ips {self.name}]\n"
            f"generate_target {{instantiation_template simulation synthesis}} [get_ips {self.name}]\n"
            f"foreach f [get_files -of_objects [get_ips {self.name}]] {{ puts \"XBAR_FILE $f\" }}\n"
        )


@dataclass
class AxiXbarIp:
    """A generated crossbar: the files xvlog compiles, in dependency order, and the include dirs."""

    module: str
    root: Path
    sim_files: list[Path] = field(default_factory=list)
    include_dirs: list[Path] = field(default_factory=list)

    def xvlog_lines(self) -> list[str]:
        """Lines for an ``xvlog -f`` file: ``--include`` dirs, then every source."""
        lines = [f"--include {d.as_posix()}" for d in self.include_dirs]
        lines += [p.as_posix() for p in self.sim_files]
        return lines


#: Compile order matters to xvlog only through `include; listing the shared libraries before the
#: crossbar that instantiates them keeps the order the IP's own simulation scripts use.
_LIB_ORDER = ("generic_baseblocks", "fifo_generator_vlog_beh", "axi_infrastructure",
              "axi_register_slice", "axi_data_fifo", "axi_crossbar")


def _collect(root: Path, name: str) -> AxiXbarIp:
    hdl = sorted((root / "hdl").glob("*.v")) + sorted((root / "simulation").glob("*.v"))

    def rank(p: Path) -> int:
        for i, key in enumerate(_LIB_ORDER):
            if p.name.startswith(key):
                return i
        return len(_LIB_ORDER)

    files = sorted(hdl, key=rank) + [root / "sim" / f"{name}.v"]
    missing = [p for p in files if not p.exists()]
    if missing:
        raise FileNotFoundError(f"generated crossbar {name} is missing {missing}")
    return AxiXbarIp(module=name, root=root, sim_files=files, include_dirs=[root / "hdl"])


def generate_axi_xbar(cfg: AxiXbarConfig, out_dir: str | Path, vivado: str | None = None,
                      force: bool = False) -> AxiXbarIp:
    """Generate (or reuse) the crossbar IP for *cfg* under ``out_dir/<name>_<digest>/``.

    The digest is the cache: an unchanged configuration reuses the generated files without starting
    Vivado, and a changed one lands in a new directory rather than half-overwriting the old one.
    Raises ``RuntimeError`` with Vivado's output tail if generation fails.
    """
    out_dir = Path(out_dir)
    work = out_dir / f"{cfg.name}_{cfg.digest()}"
    root = work / "ip" / cfg.name
    stamp = work / "done.json"
    if stamp.exists() and not force:
        return _collect(root, cfg.name)

    if vivado is None:
        from waveflow.toolchain.toolchain import find_vivado_path
        vivado = find_vivado_path()
    if not vivado:
        raise RuntimeError("generate_axi_xbar: Vivado not found (set WAVEFLOW_VIVADO_PATH or PATH)")

    work.mkdir(parents=True, exist_ok=True)
    tcl = work / "gen_xbar.tcl"
    tcl.write_text(cfg.tcl((work / "ip").as_posix()), encoding="utf-8")
    from waveflow import events

    with events.span("tool", "vivado", script=tcl.name):
        r = subprocess.run([vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", tcl.name],
                           cwd=str(work), capture_output=True, text=True, timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or "XBAR_FILE" not in out:
        raise RuntimeError(f"axi_crossbar generation failed for {cfg.name}:\n{out[-3000:]}")
    ip = _collect(root, cfg.name)
    stamp.write_text(json.dumps({"module": cfg.name, "digest": cfg.digest()}), encoding="utf-8")
    return ip


# ---------------------------------------------------------------------------
# Verilog rendering — the crossbar instance and the AXI4 port groups around it
# ---------------------------------------------------------------------------

#: AXI4 signals as ``(NAME, width-key, master-to-slave?)``.  The width key is resolved against a
#: config by :func:`axi_signals`; ``"region"`` exists on the crossbar's MI side only.
_AXI4 = (
    ("AWID", "id", True), ("AWADDR", "addr", True), ("AWLEN", 8, True), ("AWSIZE", 3, True),
    ("AWBURST", 2, True), ("AWLOCK", 1, True), ("AWCACHE", 4, True), ("AWPROT", 3, True),
    ("AWREGION", "region", True), ("AWQOS", 4, True), ("AWVALID", 1, True), ("AWREADY", 1, False),
    ("WDATA", "data", True), ("WSTRB", "strb", True), ("WLAST", 1, True), ("WVALID", 1, True),
    ("WREADY", 1, False),
    ("BID", "id", False), ("BRESP", 2, False), ("BVALID", 1, False), ("BREADY", 1, True),
    ("ARID", "id", True), ("ARADDR", "addr", True), ("ARLEN", 8, True), ("ARSIZE", 3, True),
    ("ARBURST", 2, True), ("ARLOCK", 1, True), ("ARCACHE", 4, True), ("ARPROT", 3, True),
    ("ARREGION", "region", True), ("ARQOS", 4, True), ("ARVALID", 1, True), ("ARREADY", 1, False),
    ("RID", "id", False), ("RDATA", "data", False), ("RRESP", 2, False), ("RLAST", 1, False),
    ("RVALID", 1, False), ("RREADY", 1, True),
)


def axi_signals(data_width: int, addr_width: int, id_width: int,
                region: bool = False) -> list[tuple[str, int, bool]]:
    """The AXI4 port group as ``(NAME, width, master_to_slave)``.  ``region`` adds AW/ARREGION."""
    widths = {"id": id_width, "addr": addr_width, "data": data_width, "strb": data_width // 8,
              "region": 4}
    out = []
    for name, w, m2s in _AXI4:
        if w == "region" and not region:
            continue
        out.append((name, widths[w] if isinstance(w, str) else w, m2s))
    return out


def _rng(w: int) -> str:
    return f"[{w - 1}:0] " if w > 1 else ""


def axi_port_decls(prefix: str, sigs, facing: str) -> list[str]:
    """Port declarations for one AXI4 group named ``<prefix>_<SIG>``.

    ``facing="slave"``: this module is the slave on the group (master-to-slave signals are inputs) —
    the port a host BFM drives.  ``facing="master"``: the reverse — the port a memory BFM serves.
    """
    if facing not in ("slave", "master"):
        raise ValueError(f"facing must be 'slave' or 'master', got {facing!r}")
    decls = []
    for name, w, m2s in sigs:
        is_in = m2s if facing == "slave" else not m2s
        decls.append(f"{'input ' if is_in else 'output'} wire {_rng(w)}{prefix}_{name}")
    return decls


def render_xbar_instance(cfg: AxiXbarConfig, inst: str, si_prefixes, mi_prefixes) -> str:
    """Instantiate the generated crossbar with each slot's signals on named nets.

    SI slot *k* connects to nets ``<si_prefixes[k]>_<SIG>`` and MI slot *k* to
    ``<mi_prefixes[k]>_<SIG>`` (including ``AW/ARREGION``).  The nets must already exist — as ports of
    the enclosing module or as wires — which is what lets one renderer serve both a test harness
    (where they are top-level ports) and a real top (where they are internal wires).  The crossbar's
    packed per-slot vectors are built by concatenation, highest slot first, which is legal on both
    input and output ports.
    """
    si_prefixes, mi_prefixes = list(si_prefixes), list(mi_prefixes)
    if len(si_prefixes) != cfg.n_si or len(mi_prefixes) != len(cfg.mi):
        raise ValueError(f"{cfg.name}: {cfg.n_si} SI / {len(cfg.mi)} MI slots, got "
                         f"{len(si_prefixes)} / {len(mi_prefixes)} prefixes")
    conns = ["    .aclk(ap_clk)", "    .aresetn(ap_rst_n)"]
    for side, prefixes, region in (("s", si_prefixes, False), ("m", mi_prefixes, True)):
        for name, _w, _m2s in axi_signals(cfg.data_width, cfg.addr_width, cfg.id_width, region):
            cat = ", ".join(f"{p}_{name}" for p in reversed(prefixes))
            conns.append(f"    .{side}_axi_{name.lower()}({{{cat}}})")
    return f"  {cfg.name} {inst} (\n" + ",\n".join(conns) + "\n  );\n"


def axi_wire_decls(prefix: str, sigs) -> list[str]:
    """``wire`` declarations for one AXI4 group (for nets internal to a top)."""
    return [f"wire {_rng(w)}{prefix}_{name};" for name, w, _m2s in sigs]
