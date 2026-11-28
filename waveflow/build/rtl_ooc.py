"""rtl_ooc.py — the out-of-context library: one hand-written leaf or IP configuration, synthesized once.

``plans/system_resources.md`` Stage 6.  :func:`synth_rtl_ooc` synthesizes a Verilog module with its
parameters (``synth_design -generic``) out of context, so the numbers are the leaf's alone, and caches
the parsed result by the leaf's identity (module + parameters + source digest,
:func:`~waveflow.calib.module_key.identify_rtl`) and the part and clock.  :func:`measure_instances`
is the batch over a system's inventory: every leaf and IP configuration the store lacks, each measured
once (instances sharing a configuration share the run) and filed as a ``resource`` record with
``context: "ooc"`` -- what an estimate of a design that was never synthesized reads.

The crossbar goes through :func:`~waveflow.build.axi_xbar.synth_axi_xbar` (the census's flow), on an
IP generated for the synthesis part.

The batch is deliberately not a :class:`~waveflow.build.sweep.SweepRunner`: that sweeps a parameter
*grid* through a build DAG, and a system's leaves are a heterogeneous list of (module, parameters)
configurations.  What the runner would add -- failure isolation, a re-run that costs nothing -- is
here directly: a failed configuration is reported and the rest still run, and each result is cached
by identity, so a re-run starts no Vivado.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from waveflow.toolchain.toolchain import _tool_env


@dataclass
class OocSynth:
    """One out-of-context synthesis of a leaf configuration."""

    module: str
    key: str
    params: dict
    part: str
    period_ns: float
    counters: dict = field(default_factory=dict)
    wns_ns: float | None = None
    tool: str = ""
    seconds: float = 0.0
    #: The synthesized netlist (``design.dcp``) when the run was asked to keep one -- what a link
    #: step reads into the cell this module fills (``system_ooc``).  Empty otherwise.
    checkpoint: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def clock_port(source: str | Path, module: str) -> str:
    """The clock input of *module* in *source*: ``ap_clk`` or ``clk``, read off its port list."""
    text = Path(source).read_text(encoding="utf-8")
    # The module's whole body: a port-list match would stop at a ");" in the parameter block.
    m = re.search(rf"\bmodule\s+{re.escape(module)}\b(.*?)\bendmodule\b", text, re.S)
    ports = m.group(1) if m else text
    for name in ("ap_clk", "clk", "aclk"):
        if re.search(rf"\binput\s+(?:wire\s+)?{name}\b", ports):
            return name
    raise ValueError(f"{module}: no ap_clk / clk / aclk input in {source}")


def module_header(source: str | Path, module: str) -> tuple[dict[str, str], list[tuple]]:
    """*module*'s ANSI header in *source*: ``({parameter: default}, [(direction, kind, msb, lsb,
    name)])`` -- the bounds as written (expressions over the parameters), ``None`` for a scalar."""
    text = re.sub(r"//[^\n]*", "", Path(source).read_text(encoding="utf-8"))
    m = re.search(rf"\bmodule\s+{re.escape(module)}\s*(?:#\s*\((?P<p>.*?)\))?\s*\((?P<q>.*?)\);",
                  text, re.S)
    if m is None:
        raise ValueError(f"no ANSI header for module {module} in {source}")
    params = {d.group(1): d.group(2).strip() for d in
              re.finditer(r"parameter\s+(?:integer\s+)?(\w+)\s*=\s*([^,]+)", m.group("p") or "")}
    ports, prev = [], ("input", "wire", None, None)
    for piece in m.group("q").split(","):
        d = re.match(r"\s*(?:(input|output|inout)\s+)?(?:(wire|reg)\s+)?"
                     r"(?:\[([^:\]]+):([^\]]+)\]\s*)?(\w+)\s*$", piece)
        if d is None:
            raise ValueError(f"{module}: cannot read port declaration {piece.strip()!r}")
        if d.group(1):
            prev = (d.group(1), d.group(2) or "wire", d.group(3), d.group(4))
        ports.append((*prev, d.group(5)))
    return params, ports


def _const(expr: str, params: dict) -> int:
    """A port bound over the parameters, evaluated: ``DW-1`` with ``DW=64`` is 63."""
    e = re.sub(r"\b[A-Za-z_]\w*\b", lambda m: str(params[m.group(0)]), expr)
    if not re.fullmatch(r"[0-9+\-*/()<> ]+", e):
        raise ValueError(f"port bound {expr!r} is not constant arithmetic")
    return int(eval(e, {"__builtins__": {}}))        # noqa: S307 -- checked arithmetic only


def tied_wrapper(module: str, params: dict, ties, source: str | Path) -> str:
    """A top ``<module>_tied`` around *module* with *params*, its *ties* applied: a tied input
    driven by its value, a tied output left open, every other port passed through -- the leaf as a
    system that ties it off holds it, so out of context it synthesizes to what it does there."""
    defaults, ports = module_header(source, module)
    values = {**defaults, **{k: str(v) for k, v in params.items()}}
    values = {k: _const(v, {}) if re.fullmatch(r"[0-9+\-*/()<> ]+", v) else v
              for k, v in values.items()}
    tied = dict(ties)
    unknown = sorted(set(tied) - {p[4] for p in ports})
    if unknown:
        raise ValueError(f"{module}: no ports {unknown} to tie")
    decls, conns = [], []
    for direction, _kind, msb, lsb, name in ports:
        rng = "" if msb is None else f"[{_const(msb, values)}:{_const(lsb, values)}] "
        if name in tied:
            conns.append(f".{name}({tied[name]})")
            continue
        decls.append(f"{direction} wire {rng}{name}")
        conns.append(f".{name}({name})")
    gen = ", ".join(f".{k}({v})" for k, v in params.items())
    return (f"module {module}_tied (\n  " + ",\n  ".join(decls) + "\n);\n"
            f"  {module} #({gen}) u (\n    " + ",\n    ".join(conns) + "\n  );\nendmodule\n")


def ooc_tcl(module: str, params: dict, sources: list[str], *, part: str,
            checkpoint: bool = False) -> str:
    gen = " ".join(f"-generic {k}={v}" for k, v in params.items())
    lines = [
        f"create_project -in_memory -part {part}",
        *[f"read_verilog {{{s}}}" for s in sources],
        "read_xdc clocks.xdc",
        "set t0 [clock milliseconds]",
        f"synth_design -top {module} -part {part} -mode out_of_context {gen}".rstrip(),
        "set t1 [clock milliseconds]",
        "report_utilization -file util_synth.rpt",
        "set s [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup]",
        'if {[llength $s]} { puts "WF_WNS_synth [get_property SLACK $s]" }',
        *(["write_checkpoint -force design.dcp"] if checkpoint else []),
        'puts "WF_SECONDS [expr {($t1 - $t0) / 1000.0}]"',
        'puts "WF_VERSION [version -short]"',
        'puts "WF_DONE 1"',
    ]
    return "\n".join(lines) + "\n"


def synth_rtl_ooc(module: str, params: dict, sources, *, part: str, period_ns: float,
                  out_dir: str | Path, identity=None, vivado: str | None = None,
                  force: bool = False, checkpoint: bool = False, ties=()) -> OocSynth:
    """Synthesize Verilog *module* with *params* from *sources* out of context on *part*, cached under
    ``out_dir/<key>_<part>_<period>ns/result.json`` (*identity* defaults to
    :func:`~waveflow.calib.module_key.identify_rtl` of the three).  *checkpoint* also keeps the
    netlist (``design.dcp`` beside the result; :attr:`OocSynth.checkpoint`); a cached result without
    one is re-run when one is asked for.  *ties* (``RtlInstance.ties``) synthesize the leaf inside
    :func:`tied_wrapper`, as a system that ties those ports off holds it."""
    from waveflow import events
    from waveflow.calib.module_key import identify_rtl
    from waveflow.utils.vivado_report import parse_markers, parse_utilization

    sources = [Path(s) for s in sources]
    ident = identity or identify_rtl(module, params, sources, ties)
    work = Path(out_dir) / f"{ident.key}_{part}_{period_ns:g}ns"
    stamp = work / "result.json"
    dcp = work / "design.dcp"
    if stamp.exists() and not force and (dcp.exists() or not checkpoint):
        return OocSynth(**json.loads(stamp.read_text(encoding="utf-8")))
    if vivado is None:
        from waveflow.toolchain.toolchain import find_vivado_path
        vivado = find_vivado_path()
    if not vivado:
        raise RuntimeError("synth_rtl_ooc: Vivado not found (set WAVEFLOW_VIVADO_PATH or PATH)")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    clk = clock_port(sources[0], module)
    (work / "clocks.xdc").write_text(f"create_clock -period {period_ns:g} -name {clk} "
                                     f"[get_ports {clk}]\n", encoding="utf-8")
    tcl = work / "synth_ooc.tcl"
    files, top, generics = [s.resolve().as_posix() for s in sources], module, params
    if ties:
        (work / "tied.v").write_text(tied_wrapper(module, params, ties, sources[0]),
                                     encoding="utf-8")
        files.append((work / "tied.v").resolve().as_posix())
        top, generics = f"{module}_tied", {}
    tcl.write_text(ooc_tcl(top, generics, files, part=part, checkpoint=checkpoint),
                   encoding="utf-8")
    with events.span("tool", "vivado", script=tcl.name):
        r = subprocess.run([vivado, "-mode", "batch", "-nojournal", "-log", "vivado.log",
                            "-source", tcl.name], cwd=str(work), capture_output=True, text=True,
                           timeout=3600, env=_tool_env(None))
    out = (r.stdout or "") + (r.stderr or "")
    (work / "vivado_stdout.txt").write_text(out, encoding="utf-8")
    mk = parse_markers(out)
    if r.returncode != 0 or "DONE" not in mk:
        raise RuntimeError(f"out-of-context synthesis of {module} {params} failed:\n{out[-3000:]}")
    res = OocSynth(module=module, key=ident.key, params=dict(params), part=part,
                   period_ns=float(period_ns),
                   counters=parse_utilization((work / "util_synth.rpt").read_text(encoding="utf-8")),
                   wns_ns=float(mk["WNS_synth"]) if mk.get("WNS_synth") else None,
                   tool=f"vivado {mk.get('VERSION', '')}".strip(),
                   seconds=float(mk.get("SECONDS") or 0.0),
                   checkpoint=dcp.resolve().as_posix() if checkpoint else "")
    stamp.write_text(json.dumps(res.to_json(), indent=2), encoding="utf-8")
    return res


def ooc_record(i, res: OocSynth):
    """The ``resource`` record of an out-of-context measurement of inventory instance *i*."""
    from waveflow.calib.record_store import resource_record

    return resource_record(i.identity, res.counters, source="vivado_synth", part=res.part,
                           period_ns=res.period_ns, tool=res.tool, cost_seconds=res.seconds,
                           extra={"context": "ooc", "module": res.module, "rtl_module": i.module})


def measure_instances(spec, store, *, part: str, period_ns: float, out_dir: str | Path,
                      ip_dir: str | Path | None = None, measure: bool = False) -> dict[str, dict]:
    """For every leaf and IP instance of *spec*: is its configuration in *store* (a ``resource``
    record under its identity)?  With *measure*, synthesize each missing configuration once and file
    it.  Returns ``{inst: {"key", "status", ...}}``, status one of ``stored`` / ``measured`` /
    ``missing`` / ``failed`` (with the error) -- a failure is a row, not an abort."""
    import dataclasses

    out: dict[str, dict] = {}
    done: dict[str, dict] = {}                # identity key -> its outcome, one run per configuration
    for i in spec.instances():
        if i.kind == "kernel" or i.identity is None:
            continue
        key = i.identity.key
        entry: dict[str, Any] = {"key": key, "module": i.module, "kind": i.kind}
        if key in done:
            entry.update(done[key])
        elif store is not None and store.best(key, "resource") is not None:
            entry["status"] = "stored"
        elif not measure:
            entry["status"] = "missing"
        else:
            try:
                if i.kind == "ip":
                    from waveflow.build.axi_xbar import (generate_axi_xbar, synth_axi_xbar,
                                                         xbar_synth_records)
                    cfg = dataclasses.replace(spec.xbar, part=part)
                    ip = generate_axi_xbar(cfg, ip_dir or Path(out_dir) / "ip")
                    res = synth_axi_xbar(cfg, ip, out_dir, period_ns, impl=False)
                    for rec in xbar_synth_records(cfg, res):
                        if store is not None:
                            store.append(rec, identity=i.identity)
                else:
                    res = synth_rtl_ooc(i.module, i.params, i.sources, part=part,
                                        period_ns=period_ns, out_dir=out_dir, identity=i.identity,
                                        ties=i.ties)
                    if store is not None:
                        store.append(ooc_record(i, res), identity=i.identity)
                entry["status"] = "measured"
            except Exception as exc:          # one bad configuration must not stop the batch
                entry["status"] = "failed"
                entry["error"] = str(exc)[-2000:]
        done.setdefault(key, {k: entry[k] for k in ("status", "error") if k in entry})
        out[i.inst] = entry
    return out


# ---------------------------------------------------------------------------------------------
# Placed and routed behind a ring of registers (plans/bram_fullrate_port.md, Stage 3)
# ---------------------------------------------------------------------------------------------

#: The instance name of the module under test inside :func:`registered_wrapper`'s top.
RING_DUT = "u_dut"


def registered_wrapper(dut: str, source: str | Path, *, clock: str | None = None) -> str:
    """A top ``<dut>_ring`` around module *dut* (in *source*, an ANSI header with constant widths)
    with every port but the clock registered once: an input through a flop ``i_<port>`` into the DUT,
    an output from it through a flop ``o_<port>``.

    Out of context, a path from or to a bare port is not timed at all, so a module's own port paths --
    a BRAM's output straight to the AXI read data, say -- would close by not being looked at.  Inside
    the ring every such path is a flop-to-flop path place and route must close, which is what a module
    whose neighbours register their own ports sees.  The DUT keeps its hierarchy (``KEEP_HIERARCHY``),
    so no ring flop is absorbed into it (into a BRAM's output register, for one) and its utilization
    is reported apart from the ring's."""
    clock = clock or clock_port(source, dut)
    _params, ports = module_header(source, dut)
    decls, regs, conns = [f"input wire {clock}"], [], [f".{clock}({clock})"]
    for direction, _kind, msb, lsb, name in ports:
        if name == clock:
            continue
        rng = "" if msb is None else f"[{_const(msb, {})}:{_const(lsb, {})}] "
        decls.append(f"{direction} wire {rng}{name}")
        if direction == "input":
            regs += [f"  reg  {rng}i_{name};", f"  always @(posedge {clock}) i_{name} <= {name};"]
            conns.append(f".{name}(i_{name})")
        else:
            regs += [f"  wire {rng}d_{name};", f"  reg  {rng}o_{name};",
                     f"  always @(posedge {clock}) o_{name} <= d_{name};",
                     f"  assign {name} = o_{name};"]
            conns.append(f".{name}(d_{name})")
    return (f"// {dut}_ring -- every port of {dut} registered once (rtl_ooc.registered_wrapper).\n"
            f"module {dut}_ring (\n  " + ",\n  ".join(decls) + "\n);\n" + "\n".join(regs) + "\n"
            f"  (* keep_hierarchy = \"yes\" *) {dut} {RING_DUT} (\n    " + ",\n    ".join(conns)
            + "\n  );\nendmodule\n")


@dataclass
class OocImpl:
    """One out-of-context synthesis, placement and route of a module inside its register ring."""

    dut: str
    key: str
    part: str
    period_ns: float
    #: The DUT's own utilization (the ring excluded), after synthesis and after route, and each of
    #: its child instances' after route: ``{instance: counters}``.
    synth: dict = field(default_factory=dict)
    impl: dict = field(default_factory=dict)
    children: dict = field(default_factory=dict)
    wns_ns: float | None = None
    whs_ns: float | None = None
    wns_synth_ns: float | None = None
    logic_levels: int | None = None
    critical_path: str = ""
    #: The worst setup slack of each named path group (:func:`impl_registered_ooc`'s *paths*).
    paths: dict = field(default_factory=dict)
    #: The block RAMs after route and how many use their optional output register on each port:
    #: ``{"cells", "doa_reg", "dob_reg"}`` -- a fabric register synthesis moved into the RAM shows here.
    bram_out_regs: dict = field(default_factory=dict)
    tool: str = ""
    seconds: float = 0.0

    @property
    def met(self) -> bool:
        return self.wns_ns is not None and self.wns_ns >= 0 and (self.whs_ns is None or self.whs_ns >= 0)

    def to_json(self) -> dict:
        return asdict(self)


#: Path groups every ring run reports (``get_timing_paths`` filters), besides the worst overall: into
#: the ring's output flops (the DUT's outputs), out of its input flops, and out of any block RAM.
RING_PATHS: dict[str, str] = {
    "to_outputs": "-to [get_cells -quiet {o_*}]",
    "from_inputs": "-from [get_cells -quiet {i_*}]",
    "from_bram": "-from [get_cells -quiet -hierarchical -filter {PRIMITIVE_GROUP == BLOCKRAM}]",
}


def ring_tcl(top: str, sources: list[str], *, part: str, paths: dict[str, str]) -> str:
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
        f"create_project -in_memory -part {part}",
        *[f"read_verilog {{{s}}}" for s in sources],
        "read_xdc clocks.xdc",
        "set t0 [clock milliseconds]",
        f"synth_design -top {top} -part {part} -mode out_of_context",
        "report_utilization -hierarchical -hierarchical_depth 3 -file util_synth.rpt",
        "wf_slack synth",
        "opt_design",
        "place_design",
        "phys_opt_design",
        "route_design",
        "set t1 [clock milliseconds]",
        "report_utilization -hierarchical -hierarchical_depth 3 -file util_impl.rpt",
        "report_timing_summary -max_paths 10 -file timing_impl.rpt",
        "wf_slack impl",
    ]
    for name, flt in paths.items():
        lines += [f"set p [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup {flt}]",
                  f'if {{[llength $p]}} {{ puts "WF_SLACKOF_{name} [get_property SLACK $p]" }}']
    lines += [
        "set nt 0; set na 0; set nb 0",
        "foreach c [get_cells -quiet -hierarchical -filter {PRIMITIVE_GROUP == BLOCKRAM}] {",
        "  incr nt",
        "  if {[get_property DOA_REG $c]} { incr na }",
        "  if {[get_property DOB_REG $c]} { incr nb }",
        "}",
        'puts "WF_BRAMREGS $nt $na $nb"',
    ]
    lines += ['puts "WF_SECONDS [expr {($t1 - $t0) / 1000.0}]"',
              'puts "WF_VERSION [version -short]"',
              'puts "WF_DONE 1"']
    return "\n".join(lines) + "\n"


def impl_registered_ooc(dut: str, sources, *, part: str, period_ns: float, out_dir: str | Path,
                        paths: dict[str, str] | None = None, vivado: str | None = None,
                        force: bool = False) -> OocImpl:
    """Synthesize, place and route module *dut* (from *sources*, its own file first) inside
    :func:`registered_wrapper`'s ring, out of context on *part* at *period_ns*.  Cached under
    ``out_dir/<key>_<part>_<period>ns_ring/result.json``, the key the DUT's
    :func:`~waveflow.calib.module_key.identify_rtl` (its sources' content).  *paths*: further named
    path groups to report the worst slack of, beside :data:`RING_PATHS`.  A few minutes a run."""
    from waveflow import events
    from waveflow.calib.module_key import identify_rtl
    from waveflow.utils.vivado_report import parse_markers, parse_utilization_hierarchical

    sources = [Path(s) for s in sources]
    ident = identify_rtl(dut, {}, sources)
    work = Path(out_dir) / f"{ident.key}_{part}_{period_ns:g}ns_ring"
    stamp = work / "result.json"
    groups = {**RING_PATHS, **(paths or {})}
    if stamp.exists() and not force:
        cached = OocImpl(**json.loads(stamp.read_text(encoding="utf-8")))
        if set(groups) <= set(cached.paths) and cached.bram_out_regs:
            return cached                       # else: it reported less than is asked -- run again
    if vivado is None:
        from waveflow.toolchain.toolchain import find_vivado_path
        vivado = find_vivado_path()
    if not vivado:
        raise RuntimeError("impl_registered_ooc: Vivado not found (set WAVEFLOW_VIVADO_PATH or PATH)")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    clk = clock_port(sources[0], dut)
    (work / "clocks.xdc").write_text(f"create_clock -period {period_ns:g} -name {clk} "
                                     f"[get_ports {clk}]\n", encoding="utf-8")
    ring = work / "ring.v"
    ring.write_text(registered_wrapper(dut, sources[0], clock=clk), encoding="utf-8")
    tcl = work / "impl_ring.tcl"
    tcl.write_text(ring_tcl(f"{dut}_ring", [s.resolve().as_posix() for s in [*sources, ring]],
                            part=part, paths=groups), encoding="utf-8")
    with events.span("tool", "vivado", script=tcl.name):
        r = subprocess.run([vivado, "-mode", "batch", "-nojournal", "-log", "vivado.log",
                            "-source", tcl.name], cwd=str(work), capture_output=True, text=True,
                           timeout=7200, env=_tool_env(None))
    out = (r.stdout or "") + (r.stderr or "")
    (work / "vivado_stdout.txt").write_text(out, encoding="utf-8")
    mk = parse_markers(out)
    if r.returncode != 0 or "DONE" not in mk:
        raise RuntimeError(f"ring implementation of {dut} failed:\n{out[-3000:]}")

    def dut_rows(report: str) -> tuple[dict, dict]:
        rows = parse_utilization_hierarchical((work / report).read_text(encoding="utf-8"))
        own = next(r for r in rows if r["instance"] == RING_DUT)
        kids = {r["instance"]: r["counters"] for r in rows if r["depth"] == own["depth"] + 1}
        return own["counters"], kids

    def num(k, cast=float):
        v = mk.get(k)
        return cast(float(v)) if v not in (None, "") else None

    synth, _ = dut_rows("util_synth.rpt")
    impl, kids = dut_rows("util_impl.rpt")
    res = OocImpl(dut=dut, key=ident.key, part=part, period_ns=float(period_ns), synth=synth,
                  impl=impl, children=kids, wns_ns=num("WNS_impl"), whs_ns=num("WHS_impl"),
                  wns_synth_ns=num("WNS_synth"), logic_levels=num("LEVELS_impl", int),
                  critical_path=mk.get("PATH_impl", ""),
                  paths={n: num(f"SLACKOF_{n}") for n in groups},
                  bram_out_regs=dict(zip(("cells", "doa_reg", "dob_reg"),
                                         (int(x) for x in mk.get("BRAMREGS", "0 0 0").split()))),
                  tool=f"vivado {mk.get('VERSION', '')}".strip(), seconds=num("SECONDS") or 0.0)
    stamp.write_text(json.dumps(res.to_json(), indent=2), encoding="utf-8")
    return res


__all__ = ["OocImpl", "OocSynth", "RING_DUT", "RING_PATHS", "clock_port", "impl_registered_ooc",
           "measure_instances", "module_header", "ooc_record", "ooc_tcl", "registered_wrapper",
           "ring_tcl", "synth_rtl_ooc", "tied_wrapper"]
