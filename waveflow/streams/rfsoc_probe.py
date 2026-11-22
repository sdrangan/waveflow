"""rfsoc_probe.py -- the shared-kernel blocks on the RFSoC part at 250 MHz: a timing probe.

The stream blocks, the ring stub and ``SsrFft(direction="runtime")`` against its fixed builds.

    python -m waveflow.streams.rfsoc_probe <root>                      # generate and csynth every top
    python -m waveflow.streams.rfsoc_probe <root> --steps impl         # place and route those csynths
    python -m waveflow.streams.rfsoc_probe <root> --steps              # re-read; start no tool
    (--only NAME ... picks probes; --jobs 4 tool runs at once)

``plans/shared_kernel_rfsoc_timing.md``.  The gated builds (``tests/streams/test_arbiter_demux.py``,
``tests/examples/test_ring_stub.py``) synthesize for ``composite_gen.DEFAULT_PART`` at 100 MHz and pin
cycle counts recorded there; this probe leaves them alone.  It generates the same tops in a directory of
its own, with ``render_tcl(..., part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS)`` (xczu48dr, 4 ns),
runs csynth, and reads, per top:

* the estimated clock (``csynth.xml``) against the budget, the period less Vitis's default uncertainty;
* every module's loops with their achieved II, and its resources (``<module>_csynth.xml``);
* every module's critical path, state by state (``.autopilot/db/<module>.verbose.sched.rpt``, the
  *Timing violations* section, which lists each FSM state's chain whether or not it violates);
* with ``--steps impl``: Vivado out-of-context synthesis, place and route of the csynth RTL through
  ``export_design -flow impl`` (the flow of ``examples/mimo_cg/hw/impl_check.py``; ``rtl_ooc`` and
  ``system_ooc`` stop at synthesis), the achieved clock and the worst routed path.

Each probe is its own directory ``<root>/<name>``, and its result ``<root>/<name>/probe.json``; the run
prints a table of the probes it ran (all of them in ``<root>/probe_summary.json``).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from waveflow.build.composite_gen import RFSOC4X2_PART, RFSOC4X2_PERIOD_NS, tcl_path


@dataclass(frozen=True)
class Probe:
    """One top: a stream block as its RTL gate builds it (:func:`waveflow.streams.rtl.make`), the ring
    stub's composite (``kind="ring_stub"``), or ``SsrFft`` with the ping-pong reorder as
    :func:`waveflow.dsp.ssr_fft.rtl.generate` builds it (``kind="ssr_fft"``: *length*, *inverse*,
    *direction*)."""

    kind: str
    n: int = 3
    frame_bursts: int = 1
    policy: str = "priority"
    length: int = 0
    inverse: bool = False
    direction: str = "fixed"


#: The probes: the pair under both policies at 1- and 2-burst frames, the demux both ways as gated
#: (``test_rtl_outputs_match_pysim``), and the ring stub's composite, all 32-bit; ``SsrFft``'s direction
#: chosen per frame (``plans/shared_kernel.md`` Stage 3) against the two fixed directions, at the
#: length its gates build (64) and at 1024.
PROBES: dict[str, Probe] = {
    "pair_p_f1": Probe("pair", 3, 1, "priority"),
    "pair_p_f2": Probe("pair", 3, 2, "priority"),
    "pair_rr_f1": Probe("pair", 3, 1, "round_robin"),
    "pair_rr_f2": Probe("pair", 3, 2, "round_robin"),
    "demux_sel": Probe("demux", 3, 2),
    "demux_hdr": Probe("demux_hdr", 3, 1),
    "ring_stub": Probe("ring_stub"),
    **{f"fft{L}_{tag}": Probe("ssr_fft", length=L, inverse=inv, direction=d)
       for L in (64, 1024)
       for tag, inv, d in (("fwd", False, "fixed"), ("inv", True, "fixed"), ("rt", False, "runtime"))},
}


# --- generate and synthesize -------------------------------------------------------------------------

def generate(work: Path, probe: Probe) -> str:
    """Everything before csynth in *work*, for the RFSoC target; returns the top's name."""
    work.mkdir(parents=True, exist_ok=True)
    if probe.kind == "ring_stub":
        from examples.ring_stub import ring_stub_build as rb
        from examples.ring_stub.ring_stub import RingStub
        shutil.copytree(rb.HERE / "src", work / "src", dirs_exist_ok=True)   # the hand-written bodies
        rb.gen_headers(work)
        return rb.gen_top(RingStub, work, part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS)
    if probe.kind == "ssr_fft":
        from waveflow.dsp.ssr_fft import rtl as fft_rtl
        return fft_rtl.generate(work, probe.length, reorder="pingpong", inverse=probe.inverse,
                                direction=probe.direction, part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS)
    from waveflow.streams import rtl
    mod, _gate_top = rtl.make(probe.kind, probe.n, 32, probe.frame_bursts, probe.policy)
    return rtl.generate_for(work, mod, part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS)


def _run_hls(tcl: Path, work: Path, log: Path) -> tuple[str, float]:
    from waveflow.toolchain import toolchain
    t0 = time.perf_counter()
    try:
        r = toolchain.run_vitis_hls(tcl, work_dir=work, capture_output=True)
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.CalledProcessError as exc:        # a failed run is a result to record
        out = f"FAILED: {exc}\n" + (exc.stdout or "") + (exc.stderr or "")
    log.write_text(out, encoding="utf-8")
    return out, round(time.perf_counter() - t0, 1)


def csynth(work: Path, top: str) -> dict:
    """csynth *top*'s generated ``.tcl`` in *work* (a fresh project: the script opens it with ``-reset``)."""
    out, secs = _run_hls(tcl_path(work, top), work, work / "csynth.log")
    return {"ok": "WAVEFLOW_CSYNTH_OK" in out, "seconds": secs}


_IMPL_TCL = """\
open_project {top}_proj
open_solution solution1
if {{[catch {{export_design -flow impl -rtl verilog}} res]}} {{ puts "IMPL_ERROR: $res"; exit 2 }}
puts "IMPL_OK"
exit 0
"""


def implement(work: Path, top: str) -> dict:
    """Vivado out-of-context synthesis, place and route of *top*'s csynth RTL at the solution's clock."""
    tcl = work / "impl.tcl"
    tcl.write_text(_IMPL_TCL.format(top=top), encoding="utf-8")
    out, secs = _run_hls(tcl, work, work / "impl.log")
    return {"ok": "IMPL_OK" in out, "seconds": secs}


# --- read the reports --------------------------------------------------------------------------------

def _sol(work: Path, top: str) -> Path:
    return work / f"{top}_proj" / "solution1"


def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _resources(root) -> dict:
    res = root.find("AreaEstimates/Resources")
    return {} if res is None else {c.tag: int(c.text) for c in res if (c.text or "").isdigit()}


def module_report(xml: Path) -> dict:
    """One module's estimated clock, resources and loops (achieved II, depth, trip count)."""
    root = ET.parse(xml).getroot()
    ta = root.find("PerformanceEstimates/SummaryOfTimingAnalysis")
    loops = {}
    sl = root.find("PerformanceEstimates/SummaryOfLoopLatency")
    for node in (sl if sl is not None else []):
        loops[node.tag] = {"ii": node.findtext("PipelineII"), "depth": node.findtext("PipelineDepth"),
                           "trip": node.findtext("TripCount")}
    return {"estimated_ns": _num(ta.findtext("EstimatedClockPeriod")) if ta is not None else None,
            "resources": _resources(root), "loops": loops}


_STATE = re.compile(r"^ <State (\d+)>: ([\d.]+)ns")
_TARGET = re.compile(r"Target clock period: ([\d.]+)ns, clock uncertainty: ([\d.]+)ns")


def sched_paths(rpt: Path) -> dict:
    """The *Timing violations* section of a ``.verbose.sched.rpt``: the target, the uncertainty, and
    each FSM state's critical path (its delay and its chain of operations, as Vitis prints them)."""
    text = rpt.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"\+ Verbose Summary: Timing violations\n=+\n(.*?)\n=+\n", text, re.DOTALL)
    sec = m.group(1) if m else ""
    t = _TARGET.search(sec)
    states, cur = [], None
    for line in sec.splitlines():
        s = _STATE.match(line)
        if s:
            cur = {"state": int(s.group(1)), "delay_ns": float(s.group(2)), "ops": []}
            states.append(cur)
        elif cur is not None and line.startswith("\t"):
            cur["ops"].append(line.strip())
    return {"target_ns": float(t.group(1)) if t else None, "uncertainty_ns": float(t.group(2)) if t else None,
            "states": states}


def _routed_path(sol: Path) -> dict:
    """The worst setup path of the routed design, from Vivado's ``report_timing_summary``."""
    for rpt in sorted(sol.glob("impl/**/*timing*routed*.rpt")):
        text = rpt.read_text(encoding="utf-8", errors="replace")
        i = text.find("Max Delay Paths")
        if i < 0:
            continue
        blk = text[i:]
        out = {"report": rpt.name}
        for key, pat in (("slack", r"Slack \((?:MET|VIOLATED)\)\s*:\s*(-?[\d.]+)ns"),
                         ("source", r"Source:\s+(\S+)"), ("destination", r"Destination:\s+(\S+)"),
                         ("data_path", r"Data Path Delay:\s+(.+)"), ("levels", r"Logic Levels:\s+(.+)")):
            mm = re.search(pat, blk)
            out[key] = mm.group(1).strip() if mm else None
        return out
    return {}


def impl_report(work: Path, top: str) -> dict | None:
    """The export report's utilization and clock (``examples/mimo_cg/hw/impl_check.py``'s fields), and
    the worst routed path; ``None`` when no implementation finished."""
    sol = _sol(work, top)
    rpt = sol / "impl" / "report" / "verilog" / f"{top}_export.rpt"
    if not rpt.is_file():
        return None
    text = rpt.read_text(encoding="utf-8", errors="replace")
    out: dict = {}
    for name, pat in (("LUT", r"^LUT:\s+(\d+)"), ("FF", r"^FF:\s+(\d+)"), ("DSP", r"^DSP:\s+(\d+)"),
                      ("BRAM", r"^BRAM:\s+(\d+)"), ("SRL", r"^SRL:\s+(\d+)"),
                      ("cp_required", r"^CP required:\s+([\d.]+)"),
                      ("cp_post_synth", r"^CP achieved post-synthesis:\s+([\d.]+)"),
                      ("cp_post_impl", r"^CP achieved post-implementation:\s+([\d.]+)"),
                      ("tool", r"^Implementation tool:\s+(.+)$")):
        m = re.search(pat, text, re.MULTILINE)
        out[name] = None if m is None else (m.group(1).strip() if name == "tool" else _num(m.group(1)))
    out["timing_met"] = re.search(r"^Timing met", text, re.MULTILINE) is not None
    out["worst_path"] = _routed_path(sol)
    return out


def report(work: Path, top: str) -> dict:
    """Everything the probe records of one finished csynth (and implementation, if run)."""
    from waveflow.utils.csynthparse import synth_target
    sol = _sol(work, top)
    rdir = sol / "syn" / "report"
    top_xml = ET.parse(rdir / "csynth.xml").getroot()
    out = {"top": top, "target": synth_target(rdir),
           "vitis": top_xml.findtext("ReportVersion/Version"),
           "resources": _resources(top_xml), "modules": {}}
    for xml in sorted(rdir.glob("*_csynth.xml")):
        mod = xml.name[: -len("_csynth.xml")]
        m = module_report(xml)
        rpt = sol / ".autopilot" / "db" / f"{mod}.verbose.sched.rpt"
        if rpt.is_file():
            sp = sched_paths(rpt)
            m["uncertainty_ns"] = sp["uncertainty_ns"]
            m["states"] = sp["states"]
            m["worst_state"] = max(sp["states"], key=lambda s: s["delay_ns"]) if sp["states"] else None
        out["modules"][mod] = m
    unc = next((m.get("uncertainty_ns") for m in out["modules"].values() if m.get("uncertainty_ns")), None)
    tgt = out["target"] or {}
    out["budget_ns"] = (tgt["target_period_ns"] - unc) if (unc is not None and tgt.get("target_period_ns")) else None
    out["impl"] = impl_report(work, top)
    return out


# --- the run -----------------------------------------------------------------------------------------

def run_one(root: Path, name: str, steps: tuple[str, ...]) -> dict:
    """*steps* of one probe (``csynth`` regenerates and synthesizes; ``impl`` places and routes the
    csynth already in its directory), then its report; a step not run keeps its earlier record."""
    work = root / name
    probe = PROBES[name]
    top_file, prev = work / "top.txt", work / "probe.json"
    res = ({k: v for k, v in json.loads(prev.read_text(encoding="utf-8")).items() if k in ("csynth", "impl_run")}
           if prev.is_file() else {})
    if "csynth" in steps:
        top = generate(work, probe)
        top_file.write_text(top + "\n", encoding="utf-8")
        res = {"csynth": csynth(work, top)}
    top = top_file.read_text(encoding="utf-8").strip()
    if "impl" in steps and res.get("csynth", {}).get("ok"):
        res["impl_run"] = implement(work, top)
    out = {"name": name, "probe": probe.__dict__, **res, **report(work, top)}
    prev.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def table(results: list[dict]) -> str:
    """One row a probe: the estimate against the budget, the slowest module's worst state, the loops'
    II, the resources, and the routed clock when implemented."""
    rows = [("| probe | top | estimated | budget | slowest module (state: delay) | loop II | FF | LUT | "
             "BRAM | routed CP | worst routed slack |"),
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        mods = {k: v for k, v in r["modules"].items() if v.get("worst_state")}
        slow = max(mods.items(), key=lambda kv: kv[1]["worst_state"]["delay_ns"], default=None)
        slow_s = (f"`{slow[0]}` (S{slow[1]['worst_state']['state']}: {slow[1]['worst_state']['delay_ns']:.3f})"
                  if slow else "")
        iis = sorted({str(lp["ii"]) for m in r["modules"].values() for lp in m["loops"].values()})
        imp = r.get("impl") or {}
        est = (r["target"] or {}).get("estimated_period_ns")
        res = r["resources"]
        rows.append(f"| {r['name']} | `{r['top']}` | {est} | {r['budget_ns']} | {slow_s} | {','.join(iis)} | "
                    f"{res.get('FF')} | {res.get('LUT')} | {res.get('BRAM_18K')} | "
                    f"{imp.get('cp_post_impl')} | {(imp.get('worst_path') or {}).get('slack')} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--only", nargs="+", choices=tuple(PROBES), default=list(PROBES))
    ap.add_argument("--jobs", type=int, default=4, help="tool runs at once")
    ap.add_argument("--steps", nargs="*", choices=("csynth", "impl"), default=["csynth"],
                    help="csynth: generate and synthesize; impl: place and route the csynth in place "
                         "(export_design -flow impl); none: re-read finished runs, start no tool")
    a = ap.parse_args(argv)
    root = a.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    steps = tuple(a.steps)
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        results = list(pool.map(lambda n: run_one(root, n, steps), a.only))
    summary = root / "probe_summary.json"
    old = {r["name"]: r for r in json.loads(summary.read_text(encoding="utf-8"))} if summary.is_file() else {}
    old.update({r["name"]: r for r in results})
    summary.write_text(json.dumps([old[n] for n in PROBES if n in old], indent=2), encoding="utf-8")
    print(table(results))
    failed = [r["name"] for r in results if ("csynth" in steps and not r.get("csynth", {}).get("ok"))
              or ("impl" in steps and not r.get("impl_run", {}).get("ok"))]
    if failed:
        print(f"FAILED: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
