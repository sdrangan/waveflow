"""system_ip.py — a system packaged as IP, one per device: route B's packaging.

``plans/ip_packaging_routes.md`` Stage 3.  A system's DUT is a tree of **sub-tops** -- each
device (its kernel, views, adaptor and the bus writers acting for it) and the on-chip memory --
joined by the crossbar (``SystemTopSpec.subs``).  This module packages each sub-top as a Vivado IP
with ``ipx::package_project``:

* the sources are what the sub-top compiles: its own module, rendered alone
  (:meth:`~waveflow.build.system_top.SystemTopSpec.render_sub_verilog`), its leaves' hand-written
  Verilog, and its kernels' csynth Verilog
  (:meth:`~waveflow.build.system_top.SystemTopSpec.sources_of`)
  -- the same files the per-device synthesis (:mod:`~waveflow.build.system_ooc`) reads;
* the interfaces are **inferred** from the port names: an AXI4 port group (``mi<k>_axi_*`` facing
  the crossbar's master side, ``si<k>_axi_*`` its slave side) is an AXI4 slave / master,
  ``ap_clk`` the clock, ``ap_rst_n`` the reset.  An interrupt line (``SystemTopSpec.irqs``) is
  declared one explicitly.  The clock is then associated with
  every AXI interface and the reset, which a block design needs to validate;
* the crossbar is AMD's IP already, so it is not packaged: its ``.xci`` is used as is.

Every IP is packaged in **one** Vivado session (a session costs 20-40 s to start), each cached by
the content of everything it reads, so an unchanged device is never re-packaged.  What comes back is
read from each IP's own ``component.xml`` -- its VLNV, its ports, its bus interfaces -- not from
what the script asked for (:class:`PackagedIp`), so the gate checks what Vivado produced.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: The vendor every packaged IP carries; the library is the system's top.
VENDOR = "waveflow.org"
VERSION = "1.0"
#: The packaging recipe's revision: part of every IP's cache key, so a change to what
#: :func:`package_tcl` does re-packages rather than reusing an IP packaged the old way.
RECIPE = "2"

_SPIRIT = "{http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009}"


@dataclass
class BusInterface:
    """One bus interface of a packaged IP, as its ``component.xml`` declares it."""

    name: str
    #: The bus type's name: ``aximm``, ``axis``, ``clock``, ``reset``, ``interrupt``, ...
    bus: str
    mode: str                          # "master" | "slave"
    #: Bus parameters worth reading back (``ASSOCIATED_BUSIF``, ``ASSOCIATED_RESET``, ...).
    params: dict = field(default_factory=dict)


@dataclass
class PackagedIp:
    """One sub-top packaged as IP: where it is, and what Vivado says it is."""

    sub: str                           # the sub-top (device or memory) name
    module: str                        # its Verilog module, the IP's top
    vlnv: str                          # vendor:library:name:version
    root: Path                         # the IP repository directory (holds component.xml)
    ports: dict = field(default_factory=dict)        # name -> (direction, width)
    interfaces: list = field(default_factory=list)   # [BusInterface]
    cached: bool = False

    def interface(self, name: str) -> BusInterface | None:
        return next((b for b in self.interfaces if b.name == name), None)

    def to_json(self) -> dict:
        d = asdict(self)
        d["root"] = Path(self.root).as_posix()
        return d


@dataclass
class SystemIp:
    """A system packaged per device: each sub-top's IP, and the crossbar's."""

    top: str
    part: str
    ips: dict = field(default_factory=dict)          # sub-top name -> PackagedIp
    #: The crossbar: ``{"xci": path, "vlnv": "xilinx.com:ip:axi_crossbar:2.1", "name": module}``,
    #: or ``None`` for a system with no bus inside its DUT.
    xbar: dict | None = None
    #: The Vivado session's seconds (0 when every IP was cached).
    seconds: float = 0.0

    def to_json(self) -> dict:
        return {"top": self.top, "part": self.part, "seconds": self.seconds, "xbar": self.xbar,
                "ips": {k: v.to_json() for k, v in self.ips.items()}}

    @classmethod
    def from_json(cls, d: dict) -> "SystemIp":
        """A manifest (``manifest.json``) read back."""
        ips = {}
        for k, v in d["ips"].items():
            v = dict(v)
            v["root"] = Path(v["root"])
            v["ports"] = {p: tuple(x) for p, x in v["ports"].items()}
            v["interfaces"] = [BusInterface(**b) for b in v["interfaces"]]
            ips[k] = PackagedIp(**v)
        return cls(top=d["top"], part=d["part"], ips=ips, xbar=d.get("xbar"),
                   seconds=d.get("seconds", 0.0))


def module_ports(verilog: str, module: str) -> dict[str, tuple[str, int]]:
    """``{port: (direction, width)}`` of *module*'s ANSI header in *verilog*."""
    m = re.search(rf"^module {re.escape(module)} \((.*?)\);", verilog, re.M | re.S)
    if m is None:
        raise ValueError(f"no module {module} in the text")
    out: dict[str, tuple[str, int]] = {}
    for decl in m.group(1).split(","):
        d = re.match(r"\s*(input|output|inout)\s+(?:wire\s+)?(?:\[(\d+):(\d+)\]\s*)?(\w+)\s*$",
                     decl)
        if d:
            hi, lo = d.group(2), d.group(3)
            out[d.group(4)] = (d.group(1), (int(hi) - int(lo) + 1) if hi is not None else 1)
    return out


def read_component(xml_path: Path) -> tuple[str, dict, list]:
    """``(vlnv, ports, interfaces)`` from an IP's ``component.xml``."""
    root = ET.parse(xml_path).getroot()

    def text(el, tag: str) -> str:
        e = el.find(_SPIRIT + tag)
        return e.text if e is not None and e.text is not None else ""

    vlnv = ":".join(text(root, t) for t in ("vendor", "library", "name", "version"))
    ports: dict[str, tuple[str, int]] = {}
    for p in root.iter(_SPIRIT + "port"):
        wire = p.find(_SPIRIT + "wire")
        if wire is None:
            continue
        direction = {"in": "input", "out": "output", "inout": "inout"}.get(
            text(wire, "direction"), text(wire, "direction"))
        vec = wire.find(_SPIRIT + "vector")
        width = 1
        if vec is not None:
            width = abs(int(text(vec, "left")) - int(text(vec, "right"))) + 1
        ports[text(p, "name")] = (direction, width)
    interfaces: list[BusInterface] = []
    for b in root.iter(_SPIRIT + "busInterface"):
        bt = b.find(_SPIRIT + "busType")
        bus = bt.get(_SPIRIT + "name", "") if bt is not None else ""
        mode = "master" if b.find(_SPIRIT + "master") is not None else (
            "slave" if b.find(_SPIRIT + "slave") is not None else "")
        params = {}
        for prm in b.iter(_SPIRIT + "parameter"):
            params[text(prm, "name")] = text(prm, "value")
        interfaces.append(BusInterface(text(b, "name"), bus, mode, params))
    return vlnv, ports, interfaces


def package_tcl(entries: list[dict], *, part: str) -> str:
    """One Vivado session packaging every entry ``{module, sources, root, library, description,
    interrupts}``.

    Each IP is packaged from an in-memory project over its sources (``-import_files`` copies them
    into the IP, so it is self-contained); its interfaces are inferred from the port names, and the
    clock is then associated with every AXI interface and the reset."""
    lines = ["set t0 [clock seconds]"]
    for e in entries:
        files = " ".join("{" + Path(s).resolve().as_posix() + "}" for s in e["sources"])
        root = Path(e["root"]).resolve().as_posix()
        lines += [
            f"create_project -in_memory -part {part}",
            f"add_files [list {files}]",
            f"set_property top {e['module']} [current_fileset]",
            "update_compile_order -fileset sources_1",
            f"ipx::package_project -root_dir {{{root}}} -vendor {VENDOR} -library {e['library']} "
            f"-taxonomy /UserIP -import_files -set_current true -force",
            "set core [ipx::current_core]",
            # An interrupt line is a plain output to the inference; say what it is.
            *[f"ipx::infer_bus_interface {irq} xilinx.com:signal:interrupt_rtl:1.0 $core"
              for irq in e.get("interrupts", ())],
            f"set_property name {e['module']} $core",
            f"set_property version {VERSION} $core",
            f"set_property display_name {{{e['module']}}} $core",
            f"set_property description {{{e['description']}}} $core",
            # The clock drives every AXI interface, and the reset goes with it: what a block
            # design's validation reads to put each interface in a clock domain.
            "set busifs {}",
            "foreach bi [ipx::get_bus_interfaces -of_objects $core] {",
            "  set bt [get_property bus_type_name $bi]",
            "  if {$bt eq \"aximm\" || $bt eq \"axis\"} { lappend busifs [get_property name $bi] }",
            "}",
            "set clk [ipx::get_bus_interfaces ap_clk -of_objects $core]",
            "if {$clk ne \"\" && [llength $busifs] > 0} {",
            "  set p [ipx::get_bus_parameters ASSOCIATED_BUSIF -of_objects $clk]",
            "  if {$p eq \"\"} { set p [ipx::add_bus_parameter ASSOCIATED_BUSIF $clk] }",
            "  set_property value [join $busifs :] $p",
            "}",
            "if {$clk ne \"\" && [ipx::get_bus_interfaces ap_rst_n -of_objects $core] ne \"\"} {",
            "  set p [ipx::get_bus_parameters ASSOCIATED_RESET -of_objects $clk]",
            "  if {$p eq \"\"} { set p [ipx::add_bus_parameter ASSOCIATED_RESET $clk] }",
            "  set_property value ap_rst_n $p",
            "}",
            "ipx::create_xgui_files $core",
            "ipx::update_checksums $core",
            "ipx::check_integrity $core",
            "ipx::save_core $core",
            "close_project",
            f"puts \"WF_PACKAGED {e['module']}\"",
        ]
    lines += ["puts \"WF_SECONDS [expr {[clock seconds] - $t0}]\"",
              "puts \"WF_VERSION [version -short]\"", "puts \"WF_DONE\""]
    return "\n".join(lines) + "\n"


def _digest(sources: list[Path], *extra: str) -> str:
    h = hashlib.sha256()
    for s in sources:
        h.update(Path(s).name.encode())
        h.update(Path(s).read_bytes().replace(b"\r\n", b"\n"))
    for x in extra:
        h.update(x.encode())
    return h.hexdigest()[:12]


def package_system(spec, *, kernel_dirs: dict[str, Path], part: str, out_dir: str | Path,
                   xbar_ip=None, vivado: str | None = None, force: bool = False) -> SystemIp:
    """Package every sub-top of *spec* as an IP under ``out_dir/<module>_<digest>/`` (module doc).

    *kernel_dirs* maps each csynth'd module to its ``syn/verilog``; *xbar_ip* is the crossbar the
    system top instantiates (an :class:`~waveflow.build.axi_xbar.AxiXbarIp`), recorded by its
    ``.xci``.  An IP whose inputs are unchanged is reused without starting Vivado."""
    from waveflow.utils.vivado_report import parse_markers

    out_dir = Path(out_dir)
    src_dir = out_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    res = SystemIp(top=spec.top, part=part)
    todo: list[dict] = []
    for sub in spec.subs:
        sub_v = src_dir / f"{sub.module}.v"
        text = spec.render_sub_verilog(sub.name)
        sub_v.write_text(text, encoding="utf-8")
        leaves, kernels = spec.sources_of(sub.name)
        sources = [sub_v, *leaves] + [f for m in kernels
                                      for f in sorted(Path(kernel_dirs[m]).glob("*.v"))]
        ports = module_ports(text, sub.module)
        irqs = [o for o, _view in spec.irqs if o in ports]
        digest = _digest(sources, part, VENDOR, VERSION, RECIPE, spec.top, *irqs)
        root = out_dir / f"{sub.module}_{digest}"
        ip = PackagedIp(sub=sub.name, module=sub.module, vlnv="", root=root, ports=ports)
        res.ips[sub.name] = ip
        if (root / "component.xml").is_file() and not force:
            ip.cached = True
            continue
        if root.exists():
            shutil.rmtree(root)
        todo.append({"module": sub.module, "sources": sources, "root": root, "library": spec.top,
                     "interrupts": irqs,
                     "description": f"{sub.kind} '{sub.name}' of the {spec.top} system "
                                    f"(waveflow, plans/ip_packaging_routes.md)"})

    if todo:
        if vivado is None:
            from waveflow.toolchain.toolchain import find_vivado_path
            vivado = find_vivado_path()
        if not vivado:
            raise RuntimeError("package_system: Vivado not found "
                               "(set WAVEFLOW_VIVADO_PATH or PATH)")
        work = out_dir / "session"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        (work / "package.tcl").write_text(package_tcl(todo, part=part), encoding="utf-8")
        r = subprocess.run([vivado, "-mode", "batch", "-nojournal", "-log", "vivado.log",
                            "-source", "package.tcl"], cwd=str(work), capture_output=True,
                           text=True, timeout=3600)
        out = (r.stdout or "") + (r.stderr or "")
        (work / "vivado_stdout.txt").write_text(out, encoding="utf-8")
        mk = parse_markers(out)
        if r.returncode != 0 or "DONE" not in mk:
            raise RuntimeError(f"packaging {spec.top}'s sub-tops failed:\n{out[-4000:]}")
        res.seconds = float(mk.get("SECONDS") or 0.0)

    for ip in res.ips.values():
        vlnv, ports, interfaces = read_component(ip.root / "component.xml")
        ip.vlnv, ip.interfaces = vlnv, interfaces
        if set(ports) != set(ip.ports):
            diff = sorted(set(ports) ^ set(ip.ports))
            raise RuntimeError(f"{ip.module}: the packaged IP's ports {diff} differ from the "
                               f"sub-top's")
        ip.ports = ports

    if xbar_ip is not None:
        inst = json.loads(Path(xbar_ip.xci).read_text(encoding="utf-8"))["ip_inst"]
        res.xbar = {"name": xbar_ip.module, "xci": Path(xbar_ip.xci).as_posix(),
                    "vlnv": inst["component_reference"]}
    (out_dir / "manifest.json").write_text(json.dumps(res.to_json(), indent=2) + "\n",
                                           encoding="utf-8")
    if not todo:
        res.seconds = 0.0
    return res


__all__ = ["BusInterface", "PackagedIp", "SystemIp", "VENDOR", "VERSION", "module_ports",
           "package_system", "package_tcl", "read_component"]
