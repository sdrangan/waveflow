"""system_synth.py — a system top through Vivado, its resources attributed to every instance it holds.

``plans/system_resources.md`` Stage 4.  One out-of-context synthesis (optionally placed and routed) of
the generated system top, with every instance's sources: the crossbar by ``read_ip`` on its ``.xci``,
the hand-written leaves the inventory uses (:meth:`~waveflow.build.system_top.SystemTopSpec.instances`),
each kernel's csynth Verilog, and the top.  ``report_utilization -hierarchical`` then gives a row per
instance down to the sub-tops' children (``-hierarchical_depth 2``: the top holds its devices and the
memory as sub-tops, ``plans/ip_packaging_routes.md`` Stage 0), which is joined to the inventory **by
hierarchical path** (:attr:`~waveflow.build.system_top.RtlInstance.path`).

Three numbers come out, and they add up exactly:

* **per instance** -- each inventory instance's row, measured in the design it belongs to (Vivado's
  default ``-flatten_hierarchy rebuilt`` optimises across boundaries and rebuilds the hierarchy for
  reporting, so this is the design one would ship);
* **integration** -- ``total - Σ instances``: the logic the top and its sub-tops hold themselves
  (each multi-view adaptor's inline address decoder, the stream tie-offs) plus whatever optimisation
  moved across an instance boundary.  Negative is legal and not clamped;
* **the total**.

An instance whose hierarchy synthesis dissolved has no row; it is priced by the primitives under
its path prefix instead (:func:`attribute`), so it is still its own and not glue.  An instance that can
be priced neither way **raises** (:class:`UnmappedInstanceError`).  ``keep_hierarchy`` keeps every
instance's row at a QoR cost (markov: +25% LUT, +30% FF), so it is off by default.  The run is cached under the system's identity -- the inventory's
identities, the top's text, every kernel source's digest, the part, the clock and ``impl`` -- so a
re-run with nothing changed starts no Vivado.

**Records.**  Each instance's numbers file under its identity as target :data:`INSTANCE_TARGET`
(``"instance"``), source ``vivado_synth`` (``vivado_impl`` when routed), with ``context`` = the system
top: a kernel's whole instance is *not* its own cost when it has children (a composite's own cost is
its ``integration`` record), so these never land under ``resource``, where a per-module lookup reads.
The system's identity gets an ``integration`` record and an ``instance`` record for the total.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: The store target per-instance system-synthesis numbers file under (see the module doc).
INSTANCE_TARGET = "instance"

#: Files in a kernel's ``syn/verilog`` that the synthesis must find beside it (ROM initializers a
#: ``$readmemh`` names relative to the run directory).
INIT_SUFFIXES = (".dat", ".mem", ".mif", ".hex")


class UnmappedInstanceError(RuntimeError):
    """An inventory instance has no row in the hierarchical utilization report.

    Fatal for the reason :class:`~waveflow.calib.synth_report.UnmappedModuleError` is: a dropped
    instance's cost lands in the integration term, which then reads as expensive glue."""


@dataclass
class SystemSynth:
    """One synthesis of a system top, attributed."""

    top: str
    digest: str
    part: str
    period_ns: float
    impl: bool
    keep_hierarchy: bool = False
    total: dict = field(default_factory=dict)
    instances: dict = field(default_factory=dict)      # inst -> counters
    #: Rows no inventory instance claims: the top's children and the sub-tops' children, by path.
    unclaimed: dict = field(default_factory=dict)
    #: Instances with no row of their own (synthesis dissolved their hierarchy), priced instead by the
    #: primitives under their name prefix (``u_mem_mem/*``).
    recovered: list = field(default_factory=list)
    wns_ns: float | None = None
    met: bool | None = None
    tool: str = ""
    seconds: float = 0.0
    #: The total after ``opt_design`` (asked with ``opt``): the like-for-like number for the
    #: per-device route, whose linked total is after ``opt_design`` too (``system_ooc``).
    opt_total: dict = field(default_factory=dict)
    opt_seconds: float = 0.0

    @property
    def integration(self) -> dict:
        """``total - Σ instances`` per counter -- the top's own logic and cross-boundary effects."""
        keys = set(self.total) | {k for c in self.instances.values() for k in c}
        return {k: int(self.total.get(k, 0)) - sum(int(c.get(k, 0)) for c in self.instances.values())
                for k in sorted(keys)}

    def to_json(self) -> dict:
        d = asdict(self)
        d["integration"] = self.integration
        return d

    @classmethod
    def from_json(cls, data: dict) -> "SystemSynth":
        data = {k: v for k, v in data.items() if k != "integration"}
        return cls(**data)


def _sha(path: Path) -> str:
    from waveflow.build.rtl_digest import _sha256

    return _sha256(path)


def kernel_files(kernel_dir: Path) -> list[Path]:
    """Every file of a csynth'd module's ``syn/verilog`` the synthesis needs: the Verilog and any ROM
    initializer.  A module whose csynth also emitted IP-generation scripts (floating-point cores) is
    refused: its cores would be missing from the netlist."""
    files = sorted(p for p in Path(kernel_dir).iterdir() if p.is_file())
    tcls = [p.name for p in files if p.suffix == ".tcl"]
    if tcls:
        raise NotImplementedError(f"{kernel_dir}: csynth emitted IP scripts {tcls}; system_synth does "
                                  f"not generate a kernel's Vitis IP cores yet")
    return [p for p in files if p.suffix == ".v" or p.suffix in INIT_SUFFIXES]


def system_identity(spec, top_text: str, kernel_dirs: dict[str, Path], *, part: str,
                    period_ns: float, impl: bool, keep_hierarchy: bool = False):
    """The :class:`~waveflow.calib.module_key.ModuleIdentity` of a system top's synthesis: what its
    records file under, and the cache key of the run.  Covers every instance's identity, the top's
    text and every kernel source (a kernel's RTL can change with no change to its pysim structure).
    ``part`` / ``period_ns`` / ``impl`` key the cache but not the identity's params, as for any record
    (a record's provenance carries where it was measured)."""
    from waveflow.calib.module_key import KEY_DIGEST_CHARS, ModuleIdentity

    inv = spec.instances()
    blob = {
        "top": top_text.replace("\r\n", "\n"),
        "instances": [[i.inst, i.module, i.identity.signature if i.identity else ""] for i in inv],
        "kernels": {m: {p.name: _sha(p) for p in kernel_files(d)} for m, d in sorted(kernel_dirs.items())},
    }
    sig = hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()
    kinds = [i.kind for i in inv]
    ident = ModuleIdentity(key=f"{spec.top}-{sig[:KEY_DIGEST_CHARS]}", cls_name="SystemTop",
                           cls_module="verilog",
                           params={"top": spec.top, "kernels": kinds.count("kernel"),
                                   "leaves": kinds.count("leaf"), "ip": kinds.count("ip")},
                           signature=sig)
    run = hashlib.sha256(f"{sig}|{part}|{period_ns:g}|{int(impl)}|{int(keep_hierarchy)}".encode()
                         ).hexdigest()[:12]
    return ident, run


def synth_tcl(top: str, *, part: str, period_ns: float, xci: str, sources: list[str],
              impl: bool, insts=(), opt: bool = False) -> str:
    """The batch script: read everything, synthesize out of context, report hierarchically."""
    lines = [
        # Per instance, the primitives whose names it prefixes -- what prices an instance whose
        # hierarchy synthesis dissolved (no row in the hierarchical report).
        "proc wf_prims {tag insts} {",
        "  foreach inst $insts {",
        "    set counts [dict create]",
        '    foreach c [get_cells -quiet -hierarchical -filter "IS_PRIMITIVE && NAME =~ $inst/*"] {',
        "      dict incr counts [get_property REF_NAME $c]",
        "    }",
        '    dict for {ref n} $counts { puts "WF_PRIM_$tag $inst $ref $n" }',
        "  }",
        "}",
        "proc wf_slack {tag} {",
        "  set s [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup]",
        "  if {[llength $s]} {",
        '    puts "WF_WNS_$tag [get_property SLACK $s]"',
        '    puts "WF_PATH_$tag [get_property STARTPOINT_PIN $s] -> [get_property ENDPOINT_PIN $s]"',
        "  }",
        "}",
        f"create_project -in_memory -part {part}",
        "set_property target_language Verilog [current_project]",
        f"read_ip {{{xci}}}",
        f"set_property generate_synth_checkpoint false [get_files {{{xci}}}]",
        f"generate_target all [get_files {{{xci}}}]",
    ]
    lines += [f"read_verilog {{{s}}}" for s in sources]
    lines += [
        "read_xdc clocks.xdc",
        "set t0 [clock milliseconds]",
        f"synth_design -top {top} -part {part} -mode out_of_context",
        "set t1 [clock milliseconds]",
        'puts "WF_CLOCK [get_property PERIOD [get_clocks -of_objects [get_ports ap_clk]]]"',
        "report_utilization -file util_synth.rpt",
        "report_utilization -hierarchical -hierarchical_depth 2 -file util_hier_synth.rpt",
        "wf_slack synth",
        "wf_prims synth {" + " ".join(insts) + "}",
    ]
    if opt and not impl:
        lines += [
            "set t2 [clock milliseconds]", "opt_design",
            'puts "WF_OPT_SECONDS [expr {([clock milliseconds] - $t2) / 1000.0}]"',
            "report_utilization -file util_opt.rpt",
        ]
    if impl:
        lines += [
            "opt_design", "place_design", "phys_opt_design", "route_design",
            "set t1 [clock milliseconds]",
            "report_utilization -file util_impl.rpt",
            "report_utilization -hierarchical -hierarchical_depth 2 -file util_hier_impl.rpt",
            "wf_slack impl",
            "wf_prims impl {" + " ".join(insts) + "}",
        ]
    lines += ['puts "WF_SECONDS [expr {($t1 - $t0) / 1000.0}]"', 'puts "WF_VERSION [version -short]"',
              'puts "WF_DONE 1"']
    return "\n".join(lines) + "\n"


#: A primitive's REF_NAME prefix -> the counters it occupies, as ``report_utilization`` counts them
#: (Total LUTs = logic LUTs + LUTRAM + SRL; BRAM in 18K halves).  First match wins.
_PRIM_COUNTERS: tuple[tuple[str, dict], ...] = (
    ("LUT", {"lut": 1}),
    ("SRL", {"lut": 1, "srl": 1}),
    ("RAM32M16", {"lut": 8, "lutram": 8}), ("RAM64M8", {"lut": 8, "lutram": 8}),
    ("RAM32M", {"lut": 4, "lutram": 4}), ("RAM64M", {"lut": 4, "lutram": 4}),
    ("RAM256X1D", {"lut": 8, "lutram": 8}), ("RAM128X1D", {"lut": 4, "lutram": 4}),
    ("RAM64X1D", {"lut": 2, "lutram": 2}), ("RAM32X1D", {"lut": 2, "lutram": 2}),
    ("RAM256X1S", {"lut": 4, "lutram": 4}), ("RAM128X1S", {"lut": 2, "lutram": 2}),
    ("RAM64X1S", {"lut": 1, "lutram": 1}), ("RAM32X1S", {"lut": 1, "lutram": 1}),
    ("RAMB36", {"bram": 2}), ("FIFO36", {"bram": 2}), ("RAMB18", {"bram": 1}),
    ("FIFO18", {"bram": 1}),
    ("FD", {"ff": 1}), ("LD", {"ff": 1}),
    ("DSP", {"dsp": 1}), ("URAM", {"uram": 1}),
)


def prim_counters(prims: dict[str, int]) -> dict[str, int]:
    """Counters for a census of primitives ``{REF_NAME: count}`` (the ``wf_prims`` lines)."""
    out: dict[str, int] = {}
    for ref, n in prims.items():
        for prefix, weights in _PRIM_COUNTERS:
            if ref.startswith(prefix):
                for k, w in weights.items():
                    out[k] = out.get(k, 0) + w * int(n)
                break
    return out


def parse_prims(text: str, tag: str) -> dict[str, dict[str, int]]:
    """``{inst: {REF_NAME: count}}`` from the ``WF_PRIM_<tag> inst ref n`` lines."""
    out: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == f"WF_PRIM_{tag}":
            out.setdefault(parts[1], {})[parts[2]] = int(parts[3])
    return out


def row_paths(hier_rows: list[dict]) -> list[str]:
    """Each row's hierarchical path below the top (``""`` for the top, ``u_gen_sub/u_gen`` two levels
    down), from the report's depths."""
    stack: list[str] = []
    out = []
    for r in hier_rows:
        stack = stack[:r["depth"]] + [r["instance"]]
        out.append("/".join(stack[1:]))
    return out


def attribute(spec, hier_rows: list[dict],
              prims: "dict[str, dict[str, int]] | None" = None) -> tuple[dict, dict, dict, list]:
    """``(total, {inst: counters}, unclaimed, recovered)`` from a hierarchical report: the top row,
    one row per inventory instance (by path: the top's children and its sub-tops' children), the
    rows at those levels nobody claimed, and the instances that had no row and were priced from
    *prims* -- the primitives under their path prefix -- instead.

    Synthesis can dissolve an instance's hierarchy (markov's BRAM window and memory under
    ``-flatten_hierarchy rebuilt``); its cells keep their names, so they are still its own and
    ``total - Σ instances`` stays the top's own logic.  An instance with no row and no census raises
    :class:`UnmappedInstanceError` -- a dropped instance would make the glue look expensive.  One
    with a census but no cells cost nothing: synthesis removed it."""
    top_rows = [r for r in hier_rows if r["depth"] == 0]
    if not top_rows:
        raise ValueError("hierarchical report has no top row")
    subs = {s.inst for s in spec.subs}
    children = {}
    for path, r in zip(row_paths(hier_rows), hier_rows):
        parent = path.rpartition("/")[0]
        if r["depth"] == 1 or (r["depth"] == 2 and parent in subs):
            children[path] = r["counters"]
    inv = spec.instances()
    missing = [i.inst for i in inv if i.path not in children]
    if missing and prims is None:
        raise UnmappedInstanceError(
            f"{spec.top}: inventory instance(s) {missing} have no row in the hierarchical report "
            f"(rows: {sorted(children)}) and no primitive census to price them by name.  A "
            f"dropped instance would land in the integration term and read as expensive glue, "
            f"so this is fatal.")
    inst = {i.inst: (children[i.path] if i.path in children
                     else prim_counters((prims or {}).get(i.path, {})))
            for i in inv}
    claimed = {i.path for i in inv} | subs
    return (top_rows[0]["counters"], inst, {n: c for n, c in children.items() if n not in claimed},
            missing)


def keep_hierarchy_top(top_text: str, insts) -> str:
    """*top_text* with ``(* keep_hierarchy = "yes" *)`` on each instance named in *insts* -- the
    synthesis copy of the top only; the top XSI compiles is untouched."""
    import re

    names = set(insts)
    out = []
    for line in top_text.splitlines(keepends=True):
        m = re.match(r"^(  )(\w+ (?:#\(.*\) )?(u_\w+) \()", line)
        if m and m.group(3) in names:
            line = f'{m.group(1)}(* keep_hierarchy = "yes" *) {line[2:]}'
        out.append(line)
    return "".join(out)


def synth_system(spec, *, top_v: Path, kernel_dirs: dict[str, Path], ip, out_dir: Path, part: str,
                 period_ns: float, impl: bool = False, keep_hierarchy: bool = False,
                 vivado: str | None = None,
                 force: bool = False, opt: bool = False) -> SystemSynth:
    """Synthesize the system top *spec* describes (its Verilog at *top_v*) out of context on *part*
    at *period_ns*, attribute the hierarchical report to the inventory, and cache the result under
    ``out_dir/<run digest>/result.json``.  *ip* is the crossbar IP (``generate_axi_xbar``) generated
    for *part*; *kernel_dirs* maps each csynth'd module to its ``syn/verilog``.  *opt* also runs
    ``opt_design`` after the synthesis and reports :attr:`SystemSynth.opt_total`; a cached result
    without it is re-run when it is asked for."""
    from waveflow import events
    from waveflow.utils.vivado_report import parse_markers, parse_utilization_hierarchical

    top_text = Path(top_v).read_text(encoding="utf-8")
    _ident, run = system_identity(spec, top_text, kernel_dirs, part=part, period_ns=period_ns,
                                  impl=impl, keep_hierarchy=keep_hierarchy)
    work = Path(out_dir) / run
    stamp = work / "result.json"
    if stamp.exists() and not force:
        cached = SystemSynth.from_json(json.loads(stamp.read_text(encoding="utf-8")))
        if cached.opt_total or not opt or impl:
            return cached

    if vivado is None:
        from waveflow.toolchain.toolchain import find_vivado_path
        vivado = find_vivado_path()
    if not vivado:
        raise RuntimeError("synth_system: Vivado not found (set WAVEFLOW_VIVADO_PATH or PATH)")

    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    ip_copy = work / "ip" / ip.module                  # generate_target rewrites files under the IP
    shutil.copytree(ip.root, ip_copy)
    sources = [Path(p).resolve().as_posix() for p in spec.leaf_sources()]
    for module, d in kernel_dirs.items():
        for f in kernel_files(Path(d)):
            if f.suffix == ".v":
                sources.append(f.resolve().as_posix())
            else:
                shutil.copy2(f, work / f.name)         # $readmemh resolves it from the run dir
    synth_text = (keep_hierarchy_top(top_text, [i.inst for i in spec.instances()])
                  if keep_hierarchy else top_text)
    (work / Path(top_v).name).write_text(synth_text, encoding="utf-8")
    sources.append(Path(top_v).name)
    (work / "clocks.xdc").write_text(f"create_clock -period {period_ns:g} -name ap_clk "
                                     f"[get_ports ap_clk]\n", encoding="utf-8")
    tcl = work / "synth_system.tcl"
    tcl.write_text(synth_tcl(spec.top, part=part, period_ns=period_ns,
                             xci=(ip_copy / f"{ip.module}.xci").as_posix(), sources=sources,
                             impl=impl, insts=[i.path for i in spec.instances()], opt=opt),
                   encoding="utf-8")
    with events.span("tool", "vivado", script=tcl.name):
        r = subprocess.run([vivado, "-mode", "batch", "-nojournal", "-log", "vivado.log",
                            "-source", tcl.name], cwd=str(work), capture_output=True, text=True,
                           timeout=7200)
    out = (r.stdout or "") + (r.stderr or "")
    (work / "vivado_stdout.txt").write_text(out, encoding="utf-8")
    mk = parse_markers(out)
    if r.returncode != 0 or "DONE" not in mk:
        raise RuntimeError(f"system synthesis of {spec.top} failed:\n{out[-4000:]}")
    clock = float(mk["CLOCK"]) if mk.get("CLOCK") else None
    if clock is None or abs(clock - period_ns) > 1e-6:
        raise RuntimeError(f"{spec.top}: the clock on ap_clk is {clock} ns, not {period_ns} ns")
    tag = "impl" if impl else "synth"
    rows = parse_utilization_hierarchical(
        (work / f"util_hier_{tag}.rpt").read_text(encoding="utf-8"))
    total, inst, unclaimed, recovered = attribute(spec, rows, parse_prims(out, tag))
    wns = float(mk[f"WNS_{tag}"]) if mk.get(f"WNS_{tag}") else None
    res = SystemSynth(top=spec.top, digest=run, part=part, period_ns=float(period_ns), impl=impl,
                      keep_hierarchy=keep_hierarchy,
                      total=total, instances=inst, unclaimed=unclaimed, recovered=recovered,
                      wns_ns=wns,
                      met=(wns >= 0) if (impl and wns is not None) else None,
                      tool=f"vivado {mk.get('VERSION', '')}".strip(),
                      seconds=float(mk.get("SECONDS") or 0.0))
    if opt and not impl:
        from waveflow.utils.vivado_report import parse_utilization

        res.opt_total = parse_utilization((work / "util_opt.rpt").read_text(encoding="utf-8"))
        res.opt_seconds = float(mk.get("OPT_SECONDS") or 0.0)
    stamp.write_text(json.dumps(res.to_json(), indent=2), encoding="utf-8")
    return res


def system_synth_records(spec, res: SystemSynth, top_text: str, kernel_dirs: dict[str, Path],
                         owner_label=lambda o: "") -> list[tuple[Any, Any]]:
    """``[(record, identity), ...]`` for one system synthesis: an :data:`INSTANCE_TARGET` record per
    inventory instance under its identity, the system's integration term and its total under the
    system's identity.  The run's cost is divided evenly over the instance records (one indivisible
    run, as for a csynth), the total carrying none, so ``total_cost_seconds`` counts it once."""
    from waveflow.calib.record_store import Record, integration_record, normalize_resources
    from waveflow.calib.record_store import Provenance, _now

    source = "vivado_impl" if res.impl else "vivado_synth"
    ident, _run = system_identity(spec, top_text, kernel_dirs, part=res.part,
                                  period_ns=res.period_ns, impl=res.impl)
    inv = spec.instances()
    share = res.seconds / len(inv) if inv else 0.0
    out = []

    def rec(i_ident, counters: dict, extra: dict, cost: float):
        payload = dict(normalize_resources(counters))
        payload.update(extra)
        return Record(key=i_ident.key, target=INSTANCE_TARGET, source=source, payload=payload,
                      provenance=Provenance(signature=i_ident.signature, part=res.part,
                                            period_ns=res.period_ns, tool=res.tool),
                      cost_seconds=cost, measured_at=_now())

    for i in inv:
        if i.identity is None:
            continue
        out.append((rec(i.identity, res.instances[i.inst],
                        {"context": spec.top, "system": ident.key, "inst": i.inst,
                         "module": i.module, "kind": i.kind,
                         "owner": owner_label(i.owner)}, share), i.identity))
    closure = {"wns_ns": res.wns_ns, "met": res.met} if res.impl else {}
    out.append((rec(ident, res.total, {"context": spec.top, "system": ident.key, "inst": spec.top,
                                       "kind": "system", "keep_hierarchy": res.keep_hierarchy,
                                       **closure}, 0.0), ident))
    out.append((integration_record(ident, res.integration, source=source, part=res.part,
                                   period_ns=res.period_ns, tool=res.tool), ident))
    return out


__all__ = ["INSTANCE_TARGET", "SystemSynth", "UnmappedInstanceError", "attribute", "kernel_files",
           "row_paths", "synth_system", "synth_tcl", "system_identity", "system_synth_records"]
