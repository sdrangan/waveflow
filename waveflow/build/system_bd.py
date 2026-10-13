"""system_bd.py — a system's block design, built from its packaged IPs: route B's assembly.

``plans/ip_packaging_routes.md`` Stage 4.  :mod:`~waveflow.build.system_ip` packages each sub-top
of a system as an IP; this module instantiates them in a Vivado block design beside AMD's crossbar
and wires them, from the same :class:`~waveflow.build.system_top.SystemTopSpec` the XSI system top
is rendered from:

* **the links are found by name.**  In the system top a sub-top's port is named for the net it
  carries, so an IP interface's peer is the crossbar slot of that name (``si<k>_axi`` its slave
  side, ``mi<k>_axi`` its master side), another IP with an interface of the same name (a direct
  link), or else the outside: a port of the design (the host's bus, an interrupt line)
  (:func:`bd_links`);
* **the crossbar is configured as the XSI one is** (:meth:`AxiXbarConfig.config_props`), and
  every master's address map is assigned **explicitly**, each slave at its crossbar slot's base
  and size -- the address map is written once, in Python, and ``assign_bd_address`` left to itself
  invents its own windows;
* ``validate_bd_design`` is the gate (rung 4 of ``plans/board_packaging.md``), and what comes back
  is **read from Vivado** after it: the interface nets, the nets, each master's segments, the
  crossbar's configuration, and the ports of the wrapper ``make_wrapper`` writes
  (:class:`BlockDesign`) -- so a test compares the design Vivado holds with the system top.

The design is cached by its inputs (each IP's directory is named for its content), so an unchanged
system starts no Vivado.  ``write_bd_tcl`` exports the script that recreates the design in any
project that has the IP repository on its path.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: The block-design recipe's revision: part of the cache key (see :data:`system_ip.RECIPE`).
RECIPE = "2"
XBAR_VLNV = "xilinx.com:ip:axi_crossbar:2.1"
#: How the design is synthesized when a bitstream build runs it -> its ``synth_checkpoint_mode``.
#: ``global`` (one run over everything) is the default: per device costs 0.35% LUT on markov but
#: five Vivado runs' fixed overhead cold (``plans/ip_packaging_routes.md`` Stage 5).  ``per_ip`` is
#: Vivado's own default for a block design: each IP out of context, then linked.
SYNTH_MODES = {"global": "None", "per_ip": "Hierarchical"}


@dataclass(frozen=True)
class BdLink:
    """One connection of a block design.

    ``kind`` is ``"intf"`` (interface pin *a* to interface pin *b*), ``"port"`` (interface pin *a*
    made a port of the design named *port*) or ``"pin"`` (a single pin -- an interrupt -- made a
    port named *port*).  Pins are ``<cell>/<pin>``."""

    kind: str
    a: str
    b: str = ""
    port: str = ""


@dataclass
class BlockDesign:
    """A system's block design: where it is, and what Vivado holds after validating it."""

    name: str
    part: str
    period_ns: float
    bd: str = ""                     # the .bd file
    wrapper: str = ""                # the HDL wrapper make_wrapper wrote
    tcl: str = ""                    # write_bd_tcl's script, to recreate the design elsewhere
    links: list = field(default_factory=list)          # [BdLink], what was asked for
    #: Read back: each interface net's endpoints, sorted (``/cell/pin`` or ``/port``).
    intf_nets: list = field(default_factory=list)
    #: Read back: each net's endpoints (clock, reset, interrupts), by net name.
    nets: dict = field(default_factory=dict)
    #: Read back: master address space -> {slave segment: [offset, range]}.
    segments: dict = field(default_factory=dict)
    #: Read back: the crossbar cell's configuration, for the properties the system set.
    xbar_props: dict = field(default_factory=dict)
    #: The wrapper's ports: name -> (direction, width).
    ports: dict = field(default_factory=dict)
    #: ``CRITICAL WARNING`` lines of the session (a validated design has none).
    critical: list = field(default_factory=list)
    #: Read back: the design's ``synth_checkpoint_mode`` (:data:`SYNTH_MODES`).
    synth_checkpoint_mode: str = ""
    seconds: float = 0.0
    cached: bool = False
    key: str = ""

    def to_json(self) -> dict:
        d = asdict(self)
        d["links"] = [asdict(x) for x in self.links]
        return d

    @classmethod
    def from_json(cls, d: dict) -> "BlockDesign":
        d = dict(d)
        d["links"] = [BdLink(**x) for x in d.get("links", [])]
        d["ports"] = {k: tuple(v) for k, v in d.get("ports", {}).items()}
        return cls(**d)


def bd_links(spec, ips: dict) -> list[BdLink]:
    """Every connection of *spec*'s block design but the clock and the reset (module doc).

    *ips* maps each sub-top name to its :class:`~waveflow.build.system_ip.PackagedIp` (its
    interfaces read back from the IP)."""
    from waveflow.build.system_top import XBAR_INST

    inst = {s.name: s.inst for s in spec.subs}
    ends: dict[str, list[tuple[str, str]]] = {}         # interface name -> [(cell/pin, bus)]
    for name, ip in ips.items():
        for b in ip.interfaces:
            if b.bus not in ("clock", "reset"):
                ends.setdefault(b.name, []).append((f"{inst[name]}/{b.name}", b.bus))

    def one(name: str) -> str:
        e = ends.pop(name, [])
        if len(e) != 1:
            raise ValueError(f"{spec.top}: crossbar net {name} meets {len(e)} IP interfaces, not 1")
        return e[0][0]

    links: list[BdLink] = []
    for k, (p, outside) in enumerate(spec.si):
        xpin = f"{XBAR_INST}/S{k:02d}_AXI"
        links.append(BdLink("port", xpin, port=p) if outside else BdLink("intf", one(p), xpin))
    for k, slot in enumerate(spec.mi):
        if slot.group:                   # a stub slot is never addressed: left open
            links.append(BdLink("intf", f"{XBAR_INST}/M{k:02d}_AXI", one(f"mi{k}_axi")))
    for name, e in ends.items():
        if len(e) == 2:                  # a link between two devices
            links.append(BdLink("intf", e[0][0], e[1][0]))
        elif len(e) == 1:                # out of the design
            links.append(BdLink("pin" if e[0][1] == "interrupt" else "port", e[0][0], port=name))
        else:
            raise ValueError(f"{spec.top}: net {name} meets {len(e)} IP interfaces")
    return links


def _range(n: int) -> str:
    """A window size as ``assign_bd_address -range`` takes it: ``8K``, ``1M``, ``4G``."""
    for unit, suffix in ((1 << 30, "G"), (1 << 20, "M"), (1 << 10, "K")):
        if n % unit == 0:
            return f"{n // unit}{suffix}"
    raise ValueError(f"window of {n} bytes is not a whole number of KB")


def master_spaces(spec, links: list[BdLink]) -> list[str]:
    """The address spaces that issue on the crossbar: its slave-side (SI) slots, each a port of the
    design (``/<port>``) or an IP's master interface (``/<cell>/<pin>``)."""
    from waveflow.build.system_top import XBAR_INST

    out = []
    for k, _ in enumerate(spec.si):
        xpin = f"{XBAR_INST}/S{k:02d}_AXI"
        lk = next(x for x in links if xpin in (x.a, x.b))
        out.append(f"/{lk.port}" if lk.kind == "port" else f"/{lk.a if lk.b == xpin else lk.b}")
    return out


def bd_tcl(spec, ips: dict, *, name: str, part: str, period_ns: float, repo: Path, proj: Path,
           out: Path, links: list[BdLink], synth: str = "global") -> str:
    """One Vivado session: the project, the design, its validation, the read-back, the wrapper."""
    from waveflow.build.system_top import XBAR_INST

    inst = {s.name: s.inst for s in spec.subs}
    cells = [inst[n] for n in ips] + ([XBAR_INST] if spec.xbar is not None else [])
    clk_pins = [f"{c}/{'aclk' if c == XBAR_INST else 'ap_clk'}" for c in cells]
    rst_pins = [f"{c}/{'aresetn' if c == XBAR_INST else 'ap_rst_n'}" for c in cells]
    freq = round(1e9 / period_ns)
    lines = [
        "set t0 [clock seconds]",
        f"create_project -force {name}_proj {{{proj.resolve().as_posix()}}} -part {part}",
        f"set_property ip_repo_paths [list {{{repo.resolve().as_posix()}}}] [current_project]",
        "update_ip_catalog",
        f"create_bd_design {name}",
        *[f"create_bd_cell -type ip -vlnv {ip.vlnv} {inst[n]}" for n, ip in ips.items()],
    ]
    if spec.xbar is not None:
        props = " ".join(f"{k} {{{v}}}" for k, v in spec.xbar.config_props().items())
        lines += [f"create_bd_cell -type ip -vlnv {XBAR_VLNV} {XBAR_INST}",
                  f"set_property -dict [list {props}] [get_bd_cells {XBAR_INST}]"]
    pins = " ".join(f"[get_bd_pins {p}]" for p in clk_pins)
    rsts = " ".join(f"[get_bd_pins {p}]" for p in rst_pins)
    lines += [f"create_bd_port -dir I -type clk -freq_hz {freq} ap_clk",
              "create_bd_port -dir I -type rst ap_rst_n",
              "set_property CONFIG.POLARITY ACTIVE_LOW [get_bd_ports ap_rst_n]",
              f"connect_bd_net [get_bd_ports ap_clk] {pins}",
              f"connect_bd_net [get_bd_ports ap_rst_n] {rsts}"]
    busif = []
    for lk in links:
        if lk.kind == "intf":
            lines.append(f"connect_bd_intf_net [get_bd_intf_pins {lk.a}] [get_bd_intf_pins {lk.b}]")
        elif lk.kind == "port":
            lines.append(f"make_bd_intf_pins_external -name {lk.port} [get_bd_intf_pins {lk.a}]")
            busif.append(lk.port)
        else:
            lines.append(f"make_bd_pins_external -name {lk.port} [get_bd_pins {lk.a}]")
    if busif:
        lines.append(f"set_property CONFIG.ASSOCIATED_BUSIF {{{':'.join(busif)}}} "
                     f"[get_bd_ports ap_clk]")
    lines.append("set_property CONFIG.ASSOCIATED_RESET ap_rst_n [get_bd_ports ap_clk]")
    # The address map: every master reaches every slave slot at the crossbar's own window.
    if spec.xbar is not None:
        slots = [(k, r) for k, (r, slot) in enumerate(zip(spec.xbar.mi, spec.mi)) if slot.group]
        for sp in master_spaces(spec, links):
            for k, r in slots:
                pin = next(lk.b for lk in links if lk.a == f"{XBAR_INST}/M{k:02d}_AXI")
                lines.append(
                    f"assign_bd_address -offset 0x{r.base:08X} -range {_range(r.size)} "
                    f"-target_address_space [get_bd_addr_spaces {sp}] "
                    f"[get_bd_addr_segs -of_objects [get_bd_intf_pins {pin}]]")
    lines += [
        "validate_bd_design",
        "foreach n [get_bd_intf_nets] {",
        "  puts \"WF_INET [lsort [concat [get_bd_intf_pins -quiet -of_objects $n] "
        "[get_bd_intf_ports -quiet -of_objects $n]]]\"",
        "}",
        "foreach n [get_bd_nets] {",
        "  puts \"WF_NET [get_property NAME $n] [lsort [concat [get_bd_pins -quiet -of_objects $n] "
        "[get_bd_ports -quiet -of_objects $n]]]\"",
        "}",
        "foreach sp [get_bd_addr_spaces] {",
        "  foreach s [get_bd_addr_segs -quiet -of_objects $sp] {",
        "    puts \"WF_SEG $sp [get_bd_addr_segs -quiet -of_objects $s] [get_property OFFSET $s] "
        "[get_property RANGE $s]\"",
        "  }",
        "}",
    ]
    if spec.xbar is not None:
        lines += [f"puts \"WF_XPROP {k} [get_property {k} [get_bd_cells {XBAR_INST}]]\""
                  for k in spec.xbar.config_props()]
    lines += [
        "save_bd_design",
        f"set_property synth_checkpoint_mode {SYNTH_MODES[synth]} [get_files {name}.bd]",
        f"puts \"WF_SYNTHMODE [get_property synth_checkpoint_mode [get_files {name}.bd]]\"",
        f"set w [make_wrapper -files [get_files {name}.bd] -top]",
        "puts \"WF_WRAPPER $w\"",
        f"puts \"WF_BD [get_files {name}.bd]\"",
        f"write_bd_tcl -force {{{(out / (name + '.tcl')).resolve().as_posix()}}}",
        "close_project",
        "puts \"WF_SECONDS [expr {[clock seconds] - $t0}]\"",
        "puts \"WF_VERSION [version -short]\"",
        "puts \"WF_DONE\"",
    ]
    return "\n".join(lines) + "\n"


def wrapper_ports(verilog: str) -> dict[str, tuple[str, int]]:
    """``{port: (direction, width)}`` of a ``make_wrapper`` wrapper (non-ANSI declarations)."""
    out = {}
    for d in re.finditer(r"^\s*(input|output|inout)\s*(?:\[(\d+):(\d+)\])?\s*(\w+)\s*;", verilog,
                         re.M):
        hi, lo = d.group(2), d.group(3)
        out[d.group(4)] = (d.group(1), (int(hi) - int(lo) + 1) if hi is not None else 1)
    return out


def _num(s: str) -> int:
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def _readback(bd: BlockDesign, out: str) -> None:
    for line in out.splitlines():
        line = line.strip()
        tag, _sep, rest = line.partition(" ")
        parts = rest.split()
        if tag == "WF_INET":
            bd.intf_nets.append(sorted(parts))
        elif tag == "WF_NET" and parts:
            bd.nets[parts[0]] = sorted(parts[1:])
        elif tag == "WF_SEG" and len(parts) == 4:
            bd.segments.setdefault(parts[0], {})[parts[1]] = [_num(parts[2]), _num(parts[3])]
        elif tag == "WF_XPROP" and parts:
            bd.xbar_props[parts[0]] = parts[1] if len(parts) > 1 else ""
        elif line.startswith("CRITICAL WARNING"):
            bd.critical.append(line)


def build_block_design(spec, sysip, *, period_ns: float, out_dir: str | Path,
                       synth: str = "global", vivado: str | None = None,
                       force: bool = False) -> BlockDesign:
    """*spec*'s block design over its packaged IPs *sysip* (:class:`~waveflow.build.system_ip
    .SystemIp`), validated and read back (module doc).  *synth* is how a bitstream build will
    synthesize it (:data:`SYNTH_MODES`).  Written under *out_dir*: the project, the session,
    ``<name>.tcl`` and ``block_design.json``.  Unchanged inputs start no Vivado."""
    from waveflow.utils.vivado_report import parse_markers

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{spec.top}_bd"
    links = bd_links(spec, sysip.ips)
    if synth not in SYNTH_MODES:
        raise ValueError(f"synth must be one of {sorted(SYNTH_MODES)}, got {synth!r}")
    blob = json.dumps({"recipe": RECIPE, "part": sysip.part, "period": period_ns, "synth": synth,
                       "ips": {n: [ip.vlnv, Path(ip.root).name] for n, ip in sysip.ips.items()},
                       "xbar": spec.xbar.config_props() if spec.xbar else None,
                       "links": [asdict(x) for x in links]}, sort_keys=True)
    key = hashlib.sha256(blob.encode()).hexdigest()[:12]
    rec = out_dir / "block_design.json"
    if rec.is_file() and not force:
        old = BlockDesign.from_json(json.loads(rec.read_text(encoding="utf-8")))
        if old.key == key and Path(old.bd).is_file():
            old.cached, old.seconds = True, 0.0
            return old

    if vivado is None:
        from waveflow.toolchain.toolchain import find_vivado_path
        vivado = find_vivado_path()
    if not vivado:
        raise RuntimeError("build_block_design: Vivado not found "
                           "(set WAVEFLOW_VIVADO_PATH or PATH)")
    repo = Path(next(iter(sysip.ips.values())).root).parent
    work, proj = out_dir / "session", out_dir / "proj"
    for d in (work, proj):
        if d.exists():
            shutil.rmtree(d)
    work.mkdir(parents=True)
    (work / "bd.tcl").write_text(
        bd_tcl(spec, sysip.ips, name=name, part=sysip.part, period_ns=period_ns, repo=repo,
               proj=proj, out=out_dir, links=links, synth=synth), encoding="utf-8")
    r = subprocess.run([vivado, "-mode", "batch", "-nojournal", "-log", "vivado.log", "-source",
                        "bd.tcl"], cwd=str(work), capture_output=True, text=True, timeout=3600)
    out = (r.stdout or "") + (r.stderr or "")
    (work / "vivado_stdout.txt").write_text(out, encoding="utf-8")
    mk = parse_markers(out)
    if r.returncode != 0 or "DONE" not in mk:
        raise RuntimeError(f"{name}: the block design failed:\n{out[-4000:]}")
    bd = BlockDesign(name=name, part=sysip.part, period_ns=period_ns, bd=mk.get("BD", ""),
                     wrapper=mk.get("WRAPPER", ""), tcl=(out_dir / f"{name}.tcl").as_posix(),
                     links=links, seconds=float(mk.get("SECONDS") or 0.0), key=key,
                     synth_checkpoint_mode=mk.get("SYNTHMODE", ""))
    _readback(bd, out)
    bd.ports = wrapper_ports(Path(bd.wrapper).read_text(encoding="utf-8"))
    rec.write_text(json.dumps(bd.to_json(), indent=2) + "\n", encoding="utf-8")
    return bd


__all__ = ["BdLink", "BlockDesign", "RECIPE", "SYNTH_MODES", "bd_links", "bd_tcl",
           "build_block_design", "master_spaces", "wrapper_ports"]
