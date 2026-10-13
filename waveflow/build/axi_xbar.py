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

**Connectivity mode.**  ``mode="SAMD"`` (shared address, multiple data -- the IP's default) is a true
crossbar: bursts between different SI/MI pairs move at once.  ``mode="SASD"`` (shared address, shared
data) is a shared bus: one transaction at a time across the whole interconnect, for the least area.

**What it costs.**  :func:`synth_axi_xbar` synthesizes, places and routes the generated IP out of
context at a clock period and parses its utilization and slack; :func:`render_xbar_bfm_top` and
:func:`render_xbar_latency_tb` measure its latencies under XSI.  :meth:`AxiXbarConfig.identity` is the
key both are filed under in a platform's module record store (``waveflow.calib.record_store``).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: ``CONFIG.CONNECTIVITY_MODE`` values: a crossbar (the IP's default) or a shared bus.
XBAR_MODES = ("SAMD", "SASD")


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
    mode: str = "SAMD"

    def __post_init__(self) -> None:
        object.__setattr__(self, "mi", tuple(self.mi))
        if self.mode not in XBAR_MODES:
            raise ValueError(f"mode must be one of {XBAR_MODES}, got {self.mode!r}")
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
                      part: str | None = None, mode: str = "SAMD") -> "AxiXbarConfig":
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
                   addr_width=addr_width, id_width=id_width, mode=mode, **kw)

    def digest(self) -> str:
        """A short hash of the configuration: the cache key for the generated IP.

        ``mode`` enters only when it is not the default, so every crossbar generated before the field
        existed (all SAMD) keeps its cache directory.
        """
        d = {**asdict(self), "mi": [asdict(r) for r in self.mi]}
        if self.mode == "SAMD":
            del d["mode"]
        blob = json.dumps(d, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def hw_params(self) -> dict:
        """The fields that decide the hardware -- everything but ``name`` (a label) and ``part``
        (where it is built, which a record's provenance carries instead)."""
        return {"mode": self.mode, "n_si": self.n_si, "n_mi": len(self.mi),
                "data_width": self.data_width, "addr_width": self.addr_width,
                "id_width": self.id_width, "mi": [[r.base, r.addr_width] for r in self.mi]}

    def identity(self):
        """The :class:`~waveflow.calib.module_key.ModuleIdentity` this crossbar's measurements are
        filed under in a platform's module store.

        The crossbar is not an ``HwModule``, so there is no structure signature to elaborate; its
        configuration *is* its structure.  The digest covers :meth:`hw_params`, so two designs that
        build the same crossbar under different names share one key -- and one synthesis.
        """
        from waveflow.calib.module_key import KEY_DIGEST_CHARS, ModuleIdentity

        params = self.hw_params()
        sig = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        return ModuleIdentity(key=f"axi_crossbar-{sig[:KEY_DIGEST_CHARS]}", cls_name="AxiCrossbar",
                              cls_module=__name__, params=params, signature=sig)

    def config_props(self) -> dict[str, str]:
        """``{CONFIG.<name>: value}``: what the IP is told, by ``create_ip`` here or by a block
        design's ``create_bd_cell`` (:mod:`~waveflow.build.system_bd`)."""
        props = {"CONFIG.NUM_SI": str(self.n_si), "CONFIG.NUM_MI": str(len(self.mi)),
                 "CONFIG.DATA_WIDTH": str(self.data_width),
                 "CONFIG.ADDR_WIDTH": str(self.addr_width), "CONFIG.PROTOCOL": "AXI4",
                 "CONFIG.ID_WIDTH": str(self.id_width), "CONFIG.CONNECTIVITY_MODE": self.mode}
        for k, r in enumerate(self.mi):
            props[f"CONFIG.M{k:02d}_A00_BASE_ADDR"] = f"0x{r.base:016x}"
            props[f"CONFIG.M{k:02d}_A00_ADDR_WIDTH"] = str(r.addr_width)
        return props

    def tcl(self, ip_dir: str) -> str:
        """The batch script: an in-memory project, ``create_ip``, the config, ``generate_target``."""
        body = " \\\n  ".join(f"{k} {{{v}}}" for k, v in self.config_props().items())
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

    @property
    def xci(self) -> Path:
        """The IP's ``.xci`` -- what ``read_ip`` takes for synthesis."""
        return self.root / f"{self.module}.xci"

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


#: The ID signals.  A SASD crossbar's MI side has none of them: one transaction is in flight across
#: the whole interconnect, so a response never needs routing back by ID (measured: the generated
#: wrapper lacks exactly these four ``m_axi_*`` ports).
ID_SIGS = ("AWID", "BID", "ARID", "RID")


def mi_signals(cfg: AxiXbarConfig) -> list[tuple[str, int, bool]]:
    """The crossbar's master-side (MI) port group: AXI4 with ``AW/ARREGION``, without IDs for SASD."""
    sigs = axi_signals(cfg.data_width, cfg.addr_width, cfg.id_width, region=True)
    return [s for s in sigs if not (cfg.mode == "SASD" and s[0] in ID_SIGS)]


def render_xbar_instance(cfg: AxiXbarConfig, inst: str, si_prefixes, mi_prefixes) -> str:
    """Instantiate the generated crossbar with each slot's signals on named nets.

    SI slot *k* connects to nets ``<si_prefixes[k]>_<SIG>`` and MI slot *k* to
    ``<mi_prefixes[k]>_<SIG>`` (including ``AW/ARREGION``).  The nets must already exist — as ports of
    the enclosing module or as wires — which is what lets one renderer serve both a test harness
    (where they are top-level ports) and a real top (where they are internal wires).  The crossbar's
    packed per-slot vectors are built by concatenation, highest slot first, which is legal on both
    input and output ports.  A SASD crossbar has no MI-side ID ports (:func:`mi_signals`).
    """
    si_prefixes, mi_prefixes = list(si_prefixes), list(mi_prefixes)
    if len(si_prefixes) != cfg.n_si or len(mi_prefixes) != len(cfg.mi):
        raise ValueError(f"{cfg.name}: {cfg.n_si} SI / {len(cfg.mi)} MI slots, got "
                         f"{len(si_prefixes)} / {len(mi_prefixes)} prefixes")
    conns = ["    .aclk(ap_clk)", "    .aresetn(ap_rst_n)"]
    sides = (("s", si_prefixes, axi_signals(cfg.data_width, cfg.addr_width, cfg.id_width)),
             ("m", mi_prefixes, mi_signals(cfg)))
    for side, prefixes, sigs in sides:
        for name, _w, _m2s in sigs:
            cat = ", ".join(f"{p}_{name}" for p in reversed(prefixes))
            conns.append(f"    .{side}_axi_{name.lower()}({{{cat}}})")
    return f"  {cfg.name} {inst} (\n" + ",\n".join(conns) + "\n  );\n"


def axi_wire_decls(prefix: str, sigs) -> list[str]:
    """``wire`` declarations for one AXI4 group (for nets internal to a top)."""
    return [f"wire {_rng(w)}{prefix}_{name};" for name, w, _m2s in sigs]


# ---------------------------------------------------------------------------
# Measuring it: area and slack (Vivado), latency (XSI)
# ---------------------------------------------------------------------------

@dataclass
class XbarSynth:
    """One out-of-context synthesis (and, with ``impl``, place and route) of a generated crossbar.

    ``synth`` / ``impl`` are the utilization counters after each (BRAM in 18K halves -- see
    :mod:`waveflow.utils.vivado_report`).  ``wns_ns`` / ``whs_ns`` are the worst setup / hold slack
    **after routing** at ``period_ns`` -- the closure verdict -- and ``wns_synth_ns`` the
    post-synthesis estimate, which leaves out routing and is optimistic.  ``clock_ns`` is the period
    Vivado actually applied, read back so a constraint that silently failed to bind cannot pass for a
    met one.
    """

    digest: str
    part: str
    period_ns: float
    synth: dict = field(default_factory=dict)
    impl: dict | None = None
    wns_ns: float | None = None
    whs_ns: float | None = None
    wns_synth_ns: float | None = None
    logic_levels: int | None = None
    critical_path: str = ""
    clock_ns: float | None = None
    tool: str = ""
    synth_seconds: float = 0.0
    impl_seconds: float = 0.0

    @property
    def met(self) -> bool | None:
        """Timing closed after routing (``None`` when only synthesized)."""
        return None if self.wns_ns is None else self.wns_ns >= 0.0

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "XbarSynth":
        return cls(**data)


def synth_tcl(cfg: AxiXbarConfig, xci: str, period_ns: float, impl: bool = True) -> str:
    """Out-of-context synthesis (+ place and route) of the IP at *xci*, printing ``WF_*`` markers.

    ``generate_synth_checkpoint false`` makes ``synth_design`` take the IP's RTL as the design itself
    instead of looking for a separately synthesized checkpoint.  The clock is created **after**
    ``synth_design``, overriding whatever the IP's own OOC constraints set, and read back.
    """
    lines = [
        "proc wf_slack {tag} {",
        "  set s [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup]",
        "  if {[llength $s]} {",
        '    puts "WF_WNS_$tag [get_property SLACK $s]"',
        '    puts "WF_LEVELS_$tag [get_property LOGIC_LEVELS $s]"',
        '    puts "WF_PATH_$tag [get_property STARTPOINT_PIN $s] -> [get_property ENDPOINT_PIN $s]"',
        "  }",
        "  set h [get_timing_paths -quiet -max_paths 1 -nworst 1 -hold]",
        '  if {[llength $h]} { puts "WF_WHS_$tag [get_property SLACK $h]" }',
        "}",
        f"create_project -in_memory -part {cfg.part}",
        "set_property target_language Verilog [current_project]",
        f"read_ip {{{xci}}}",
        f"set_property generate_synth_checkpoint false [get_files {{{xci}}}]",
        f"generate_target all [get_files {{{xci}}}]",
        "set t0 [clock milliseconds]",
        f"synth_design -top {cfg.name} -part {cfg.part} -mode out_of_context",
        f"create_clock -period {period_ns:g} -name aclk [get_ports aclk]",
        'puts "WF_CLOCK [get_property PERIOD [get_clocks -of_objects [get_ports aclk]]]"',
        "set t1 [clock milliseconds]",
        "report_utilization -file util_synth.rpt",
        "wf_slack synth",
    ]
    if impl:
        lines += [
            "opt_design",
            "place_design",
            "phys_opt_design",
            "route_design",
            "set t2 [clock milliseconds]",
            "report_utilization -file util_impl.rpt",
            "report_timing_summary -file timing_impl.rpt",
            "wf_slack impl",
            'puts "WF_SECONDS_IMPL [expr {($t2 - $t1) / 1000.0}]"',
        ]
    lines += ['puts "WF_SECONDS_SYNTH [expr {($t1 - $t0) / 1000.0}]"',
              'puts "WF_VERSION [version -short]"',
              'puts "WF_DONE 1"']
    return "\n".join(lines) + "\n"


def synth_axi_xbar(cfg: AxiXbarConfig, ip: AxiXbarIp, out_dir: str | Path, period_ns: float,
                   impl: bool = True, vivado: str | None = None,
                   force: bool = False) -> XbarSynth:
    """Synthesize -- and with *impl*, place and route -- the generated crossbar out of context.

    Out of context, so the numbers are the crossbar's alone: no I/O buffers, no neighbours to share
    logic with.  Runs on a **copy** of the IP directory (``generate_target`` rewrites files under it,
    and the original is what XSI compiles), and caches the parsed result under
    ``out_dir/<name>_<digest>/synth_<period>ns[_impl]/result.json``, so a sweep pays each
    configuration once.  A few minutes per configuration with *impl*.
    """
    import shutil

    from waveflow.utils.vivado_report import parse_markers, parse_utilization

    tag = f"synth_{period_ns:g}ns{'_impl' if impl else ''}"
    work = Path(out_dir) / f"{cfg.name}_{cfg.digest()}" / tag
    stamp = work / "result.json"
    if stamp.exists() and not force:
        return XbarSynth.from_json(json.loads(stamp.read_text(encoding="utf-8")))

    if vivado is None:
        from waveflow.toolchain.toolchain import find_vivado_path
        vivado = find_vivado_path()
    if not vivado:
        raise RuntimeError("synth_axi_xbar: Vivado not found (set WAVEFLOW_VIVADO_PATH or PATH)")

    if work.exists():
        shutil.rmtree(work)
    ip_copy = work / "ip" / cfg.name
    shutil.copytree(ip.root, ip_copy)
    tcl = work / "synth_xbar.tcl"
    tcl.write_text(synth_tcl(cfg, (ip_copy / f"{cfg.name}.xci").as_posix(), period_ns, impl),
                   encoding="utf-8")
    from waveflow import events

    with events.span("tool", "vivado", script=tcl.name):
        r = subprocess.run([vivado, "-mode", "batch", "-nojournal", "-log", "vivado.log",
                            "-source", tcl.name], cwd=str(work), capture_output=True, text=True,
                           timeout=7200)
    out = (r.stdout or "") + (r.stderr or "")
    (work / "vivado_stdout.txt").write_text(out, encoding="utf-8")
    mk = parse_markers(out)
    if r.returncode != 0 or "DONE" not in mk:
        raise RuntimeError(f"axi_crossbar synthesis failed for {cfg.name}:\n{out[-3000:]}")

    def num(name: str, cast=float):
        v = mk.get(name)
        return None if v in (None, "") else cast(float(v))

    res = XbarSynth(
        digest=cfg.digest(), part=cfg.part, period_ns=float(period_ns),
        synth=parse_utilization((work / "util_synth.rpt").read_text(encoding="utf-8")),
        impl=(parse_utilization((work / "util_impl.rpt").read_text(encoding="utf-8"))
              if impl else None),
        wns_ns=num("WNS_impl") if impl else None, whs_ns=num("WHS_impl") if impl else None,
        wns_synth_ns=num("WNS_synth"),
        logic_levels=num("LEVELS_impl" if impl else "LEVELS_synth", int),
        critical_path=mk.get("PATH_impl" if impl else "PATH_synth", ""),
        clock_ns=num("CLOCK"), tool=f"vivado {mk.get('VERSION', '')}".strip(),
        synth_seconds=num("SECONDS_SYNTH") or 0.0, impl_seconds=num("SECONDS_IMPL") or 0.0,
    )
    if res.clock_ns is None or abs(res.clock_ns - period_ns) > 1e-6:
        raise RuntimeError(f"{cfg.name}: the clock on aclk is {res.clock_ns} ns, not the requested "
                           f"{period_ns} ns -- the slack would describe a different constraint")
    stamp.write_text(json.dumps(res.to_json(), indent=2), encoding="utf-8")
    return res


def xbar_synth_records(cfg: AxiXbarConfig, res: XbarSynth) -> list:
    """The resource records a synthesis yields: ``vivado_synth``, and ``vivado_impl`` when routed.

    The routed record carries the closure verdict (``wns_ns``, ``whs_ns``, ``met``, ``logic_levels``)
    in its payload beside the counters: area that does not close at the platform clock is not area
    the platform can have, and a reader of the record should not need a second file to know which.
    """
    from waveflow.calib.record_store import resource_record

    ident = cfg.identity()
    common = dict(part=res.part, period_ns=res.period_ns, tool=res.tool)
    extra = {"rtl_module": cfg.name, "mode": cfg.mode}
    recs = [resource_record(ident, res.synth, source="vivado_synth",
                            cost_seconds=res.synth_seconds,
                            extra={**extra, "wns_synth_ns": res.wns_synth_ns}, **common)]
    if res.impl is not None:
        recs.append(resource_record(
            ident, res.impl, source="vivado_impl", cost_seconds=res.impl_seconds,
            extra={**extra, "wns_ns": res.wns_ns, "whs_ns": res.whs_ns, "met": res.met,
                   "logic_levels": res.logic_levels, "critical_path": res.critical_path},
            **common))
    return recs


#: What the memory BFMs read/drive (AxiMmReadSlave / AxiMmWriteSlave).  Everything else on the MI
#: side is terminated inside :func:`render_xbar_bfm_top`.
BFM_SLAVE_SIGS = ("AWADDR", "AWLEN", "AWVALID", "AWREADY", "WDATA", "WSTRB", "WLAST", "WVALID",
                  "WREADY", "BVALID", "BREADY", "ARADDR", "ARLEN", "ARVALID", "ARREADY", "RDATA",
                  "RLAST", "RVALID", "RREADY")


def render_xbar_bfm_top(cfg: AxiXbarConfig, top: str) -> str:
    """A Verilog top that puts the crossbar between host BFMs and memory BFMs.

    SI *k* is the top-level port group ``s<k>_axi`` (driven by a ``wfbfm::AxiMmMaster``); MI *k* is
    ``m<k>_axi`` (served by ``AxiMmReadSlave`` / ``AxiMmWriteSlave``).  The memory BFMs serve one
    transaction at a time and do not drive ``BID``/``RID``, so the top latches ``AWID``/``ARID`` per MI
    and echoes it -- correct exactly because a memory BFM holds ``AWREADY`` low until its ``B`` is
    taken.  It also masks each address to its MI's window, so each memory sees a local offset.  A SASD
    crossbar has no MI-side IDs, so there is nothing to echo.
    """
    si = [f"s{k}_axi" for k in range(cfg.n_si)]
    mi_int = [f"mx{k}_axi" for k in range(len(cfg.mi))]
    mi_ext = [f"m{k}_axi" for k in range(len(cfg.mi))]
    sig_si = axi_signals(cfg.data_width, cfg.addr_width, cfg.id_width)
    sig_mi = mi_signals(cfg)
    sig_bfm = [s for s in sig_mi if s[0] in BFM_SLAVE_SIGS]
    has_id = any(s[0] == "AWID" for s in sig_mi)

    ports = ["input wire ap_clk", "input wire ap_rst_n"]
    for p in si:
        ports += axi_port_decls(p, sig_si, facing="slave")
    for p in mi_ext:
        ports += axi_port_decls(p, sig_bfm, facing="master")
    body = []
    for p in mi_int:
        body += axi_wire_decls(p, sig_mi)
    body.append(render_xbar_instance(cfg, "u_xbar", si, mi_int))
    for k, (pi, pe) in enumerate(zip(mi_int, mi_ext)):
        mask = cfg.mi[k].size - 1
        for name, _w, m2s in sig_bfm:
            if name in ("AWADDR", "ARADDR"):
                body.append(f"assign {pe}_{name} = {pi}_{name} & {cfg.addr_width}'h{mask:x};")
            elif m2s:
                body.append(f"assign {pe}_{name} = {pi}_{name};")
            else:
                body.append(f"assign {pi}_{name} = {pe}_{name};")
        body += [f"assign {pi}_BRESP = 2'b00;", f"assign {pi}_RRESP = 2'b00;"]
        if not has_id:
            continue
        idw = cfg.id_width
        body += [
            f"reg [{idw - 1}:0] {pi}_bid_q, {pi}_rid_q;",
            "always @(posedge ap_clk) begin",
            f"  if (!ap_rst_n) begin {pi}_bid_q <= 0; {pi}_rid_q <= 0; end",
            "  else begin",
            f"    if ({pi}_AWVALID && {pi}_AWREADY) {pi}_bid_q <= {pi}_AWID;",
            f"    if ({pi}_ARVALID && {pi}_ARREADY) {pi}_rid_q <= {pi}_ARID;",
            "  end",
            "end",
            f"assign {pi}_BID = {pi}_bid_q;",
            f"assign {pi}_RID = {pi}_rid_q;",
        ]
    return (f"// {top}.v -- GENERATED by waveflow.build.axi_xbar.render_xbar_bfm_top.\n"
            f"`timescale 1ns/1ps\nmodule {top} (\n  " + ",\n  ".join(ports) + "\n);\n"
            + "\n".join(body) + "\nendmodule\n")


@dataclass(frozen=True)
class XbarProbe:
    """One latency measurement: every op starts at the same cycle, and the span runs from the first
    address phase to the last response, inclusive.

    *ops* are ``(si, "W"|"R", mi, beats)``.  A probe with one op is an uncontended latency; with
    several, the span says how the crossbar overlapped them -- side by side (SAMD, different MIs) or
    one after another (one MI, or SASD).
    """

    name: str
    ops: tuple
    what: str = ""


def xbar_latency_probes(cfg: AxiXbarConfig, beats: int = 16) -> list[XbarProbe]:
    """The standard probe set for *cfg*.

    * ``w1`` / ``r1`` / ``w<B>`` / ``r<B>``: SI 0 to MI 0, alone -- the per-transaction and per-beat
      costs.
    * ``par_w<B>`` / ``par_r<B>`` (two or more SIs **and** MIs): SI *k* to MI *k* for every *k* that
      has both, at once.  A crossbar runs them side by side; a shared bus serializes them.
    * ``same_w<B>`` / ``same_r<B>`` (two or more SIs): every SI to MI 0 at once.  Serialized in either
      mode -- AXI4 bursts to one slave cannot interleave -- so the span measures the arbitration
      overhead per extra master.

    Writes come before the reads of the same family, which read back what they wrote.
    """
    b = int(beats)
    probes = [XbarProbe("w1", ((0, "W", 0, 1),), "one-beat write, alone"),
              XbarProbe("r1", ((0, "R", 0, 1),), "one-beat read, alone"),
              XbarProbe(f"w{b}", ((0, "W", 0, b),), f"{b}-beat write, alone"),
              XbarProbe(f"r{b}", ((0, "R", 0, b),), f"{b}-beat read, alone")]
    k_par = min(cfg.n_si, len(cfg.mi))
    if k_par >= 2:
        probes += [XbarProbe(f"par_w{b}", tuple((k, "W", k, b) for k in range(k_par)),
                             f"{k_par} masters write {b} beats to {k_par} different slaves at once"),
                   XbarProbe(f"par_r{b}", tuple((k, "R", k, b) for k in range(k_par)),
                             f"{k_par} masters read {b} beats from {k_par} different slaves at once")]
    if cfg.n_si >= 2:
        probes += [XbarProbe(f"same_w{b}", tuple((k, "W", 0, b) for k in range(cfg.n_si)),
                             f"{cfg.n_si} masters write {b} beats to one slave at once"),
                   XbarProbe(f"same_r{b}", tuple((k, "R", 0, b) for k in range(cfg.n_si)),
                             f"{cfg.n_si} masters read {b} beats from one slave at once")]
    return probes


#: Cycles between probe starts -- long enough that a probe has drained (its worst case is every SI
#: serialized on one MI) before the next begins, so no probe sees another's traffic.
PROBE_GAP = 400


def _probe_family(name: str) -> str:
    """``w16``/``r16`` -> ``x16``, ``par_w16``/``par_r16`` -> ``par_x16``: a write and the read that
    checks it share an address block."""
    return re.sub(r"(^|_)[wr](\d+)$", r"\1x\2", name)


def render_xbar_latency_tb(cfg: AxiXbarConfig, probes, dll: str, wdb: str = "xbar_lat.wdb") -> str:
    """The XSI testbench (C++) that runs *probes* through :func:`render_xbar_bfm_top`.

    Probe *p* starts at cycle ``(p + 1) * PROBE_GAP``.  Each SI's ops are queued in time order, so an
    ``AxiMmMaster`` (one transaction at a time) never holds one probe's op behind another's.  Every
    write stores a distinct ramp and every read checks it gets back what the matching write stored,
    so a probe that measures a wrong transfer fails rather than reporting a latency.

    Prints ``OP <probe> <si> <W|R> <start> <end>`` per op, then ``PASSED test``.
    """
    bpw = cfg.data_width // 8
    n_mi = len(cfg.mi)
    mem_words = [min(r.size // bpw, 1 << 16) for r in cfg.mi]
    L = ["// GENERATED by waveflow.build.axi_xbar.render_xbar_latency_tb -- do not edit.",
         '#include "xsi_bfm.h"', "using namespace wfbfm;", "",
         "static std::vector<uint64_t> ramp(uint64_t base, int n) {",
         "    std::vector<uint64_t> v;",
         "    for (int i = 0; i < n; ++i) v.push_back(base + (uint64_t)i);",
         "    return v;",
         "}", "", "int main() {",
         f'    XsiSim sim("{dll}", "{wdb}");']
    for k in range(n_mi):
        L.append(f"    FlatMemory mem{k}({mem_words[k]}, {bpw});")
    for k in range(cfg.n_si):
        L.append(f'    AxiMmMaster h{k}(sim.dut(), "s{k}_axi", {bpw}, {k});')
    for k in range(n_mi):
        L.append(f'    AxiMmReadSlave r{k}(sim.dut(), "m{k}_axi", mem{k});')
        L.append(f'    AxiMmWriteSlave w{k}(sim.dut(), "m{k}_axi", mem{k});')
    objs = ([f"&mem{k}" for k in range(n_mi)] + [f"&h{k}" for k in range(cfg.n_si)]
            + [f"&r{k}" for k in range(n_mi)] + [f"&w{k}" for k in range(n_mi)])
    L.append(f"    std::vector<XsiSimObj*> all = {{{', '.join(objs)}}};")
    L.append("    auto drive = [&] { for (auto* p : all) p->drive(); };")

    # Each probe family gets its own 1 KB block in each MI's window, each SI its own 256-byte slot.
    fam_ids: dict = {}
    ops = []          # (probe, si, kind, beats, addr, data_base)
    for p_idx, pr in enumerate(probes):
        t = (p_idx + 1) * PROBE_GAP
        fam = fam_ids.setdefault(_probe_family(pr.name), len(fam_ids))
        for si, kind, mi, beats in pr.ops:
            local = 0x400 * fam + 0x100 * si
            if beats * bpw > 0x100 or local + 0x100 > cfg.mi[mi].size:
                raise ValueError(f"probe {pr.name}: {beats} beats do not fit their address slot")
            addr = cfg.mi[mi].base + local
            base = (fam + 1) * 0x1000 + si * 0x100
            ops.append((pr.name, si, kind, beats, base))
            if kind == "W":
                L.append(f"    size_t op{len(ops) - 1} = h{si}.write(0x{addr:x}, "
                         f"ramp(0x{base:x}, {beats}), {t});")
            else:
                L.append(f"    size_t op{len(ops) - 1} = h{si}.read(0x{addr:x}, {beats}, {t});")
    idle = " && ".join(f"h{k}.idle()" for k in range(cfg.n_si))
    last = (len(probes) + 2) * PROBE_GAP + 2000
    L += ["    for (auto* p : all) p->pre_sim();",
          "    sim.reset(drive);",
          "    long cyc = 0;",
          f"    for (; cyc < {last} && !({idle}); ++cyc) {{",
          "        sim.clock_low();  for (auto* p : all) p->sample();",
          "        sim.clock_high(); for (auto* p : all) p->update(); drive();",
          "    }",
          "    for (auto* p : all) p->post_sim();",
          "    int fail = 0;",
          "    auto check = [&](const char* what, bool ok) {",
          '        if (!ok) { std::printf("FAIL %s\\n", what); ++fail; }',
          "    };",
          f'    check("hosts idle", {idle});']
    for i, (name, si, kind, beats, base) in enumerate(ops):
        o = f"h{si}.op(op{i})"
        L.append(f'    check("{name} si{si} done", {o}.done());')
        L.append(f'    check("{name} si{si} OKAY", {o}.resp == 0);')
        if kind == "R":
            L.append(f'    check("{name} si{si} readback", {o}.rdata == ramp(0x{base:x}, {beats}));')
        L.append(f'    std::printf("OP {name} {si} {kind} %ld %ld\\n", {o}.t_start, {o}.t_end);')
    L += ['    std::printf("cycles=%ld\\n", cyc);',
          '    std::printf(fail ? "FAILED test (%d)\\n" : "PASSED test\\n", fail);',
          "    sim.close();", "    return fail ? 1 : 0;", "}", ""]
    return "\n".join(L)


def parse_latency_output(out: str, probes) -> dict[str, int]:
    """``{probe: span}`` from :func:`render_xbar_latency_tb`'s ``OP`` lines.

    The span is first address phase to last response, inclusive -- for a one-op probe, exactly the
    ``t_end - t_start + 1`` the crossbar guide's latency table reports.
    """
    if "PASSED test" not in out:
        raise RuntimeError(f"crossbar latency testbench failed:\n{out[-3000:]}")
    spans: dict = {}
    for line in out.splitlines():
        m = re.match(r"^OP (\S+) (\d+) ([WR]) (-?\d+) (-?\d+)$", line.strip())
        if m:
            name, start, end = m.group(1), int(m.group(4)), int(m.group(5))
            lo, hi = spans.get(name, (start, end))
            spans[name] = (min(lo, start), max(hi, end))
    missing = [p.name for p in probes if p.name not in spans]
    if missing:
        raise RuntimeError(f"no OP lines for probe(s) {missing}")
    return {p.name: spans[p.name][1] - spans[p.name][0] + 1 for p in probes}


def xbar_timing_records(cfg: AxiXbarConfig, spans: dict, probes, *, part: str = "",
                        tool: str = "xsim", cost_seconds: float = 0.0) -> list:
    """One ``timing`` record per probe, ``source="xsi"``: ``{features, cycles}``.

    ``features`` names the probe's shape (op, beats, masters, distinct slaves), so a model can be fit
    across configurations without parsing probe names.  No ``period_ns``: these are cycle counts of
    the crossbar's own logic, which XSI simulates the same way at any clock.
    """
    from waveflow.calib.record_store import Provenance, Record, _now

    ident = cfg.identity()
    share = cost_seconds / max(len(probes), 1)
    recs = []
    for pr in probes:
        feats = {"probe": pr.name, "op": pr.ops[0][1], "beats": pr.ops[0][3],
                 "masters": len({o[0] for o in pr.ops}), "slaves": len({o[2] for o in pr.ops})}
        recs.append(Record(key=ident.key, target="timing", source="xsi",
                           payload={"features": feats, "cycles": int(spans[pr.name]),
                                    "what": pr.what},
                           provenance=Provenance(signature=ident.signature, part=part, tool=tool),
                           cost_seconds=share, measured_at=_now()))
    return recs
