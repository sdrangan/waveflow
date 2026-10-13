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
                           timeout=3600)
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


__all__ = ["OocSynth", "clock_port", "measure_instances", "module_header", "ooc_record",
           "ooc_tcl", "synth_rtl_ooc", "tied_wrapper"]
