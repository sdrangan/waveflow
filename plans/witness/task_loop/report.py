#!/usr/bin/env python3
"""Read the eight csynth reports and print the witness table.

Standalone on purpose — stdlib only, no Waveflow import.  A witness that used our own report parser
could not settle a question about Vitis, because a parser bug and a synthesis result would be
indistinguishable.

THE FIELD THAT MATTERS, AND THE ONE THAT HAS BEEN MISREAD TWICE:

    PerformanceEstimates/SummaryOfLoopLatency/<loop>/PipelineII      <-- ACHIEVED.  Use this.
    PerformanceEstimates/SummaryOfOverallLatency/Interval-max        <-- the module's interval

They are different numbers.  When Vitis misses an `II=1` target it still reports the *target* in the
human-readable summary's "Target II" column while the schedule runs at the achieved value; taking
the target would flatter the design by exactly the factor that hides starvation at a converter.
Everything below reads the XML, and every II printed is the achieved one.

`undef` is preserved as None rather than coerced.  Vitis writes `undef` when it cannot bound a body
— a `while (1)` firing has no trip count — and substituting a number there would invent a
cycles-per-firing that nothing measured.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent

VARIANTS = ["ing_1", "ing_n8", "ing_n64", "ing_w",
            "ply_1", "ply_n8", "ply_n64", "ply_w"]

#: Words per firing, by construction.  Not read from the report — it is what the source says, and the
#: whole point is to compare it against what the report says the firing costs.
NWORDS = {"ing_1": 1, "ing_n8": 8, "ing_n64": 64, "ing_w": None,
          "ply_1": 1, "ply_n8": 8, "ply_n64": 64, "ply_w": None}


def _num(text: str | None):
    """`undef` -> None, everything else -> int.  See the module docstring."""
    if text is None:
        return None
    t = text.strip()
    if t in ("undef", "-", ""):
        return None
    try:
        return int(t)
    except ValueError:
        try:
            return float(t)
        except ValueError:
            return None


def read_module(report_dir: Path, module: str) -> dict | None:
    """Overall latency/interval and every pipelined loop, from one `<module>_csynth.xml`."""
    p = report_dir / f"{module}_csynth.xml"
    if not p.is_file():
        return None
    root = ET.parse(p).getroot()
    perf = root.find("PerformanceEstimates")
    out: dict = {"module": module, "loops": []}
    overall = perf.find("SummaryOfOverallLatency") if perf is not None else None
    if overall is not None:
        out["latency_max"] = _num(overall.findtext("Worst-caseLatency"))
        out["interval_max"] = _num(overall.findtext("Interval-max"))
    loops = perf.find("SummaryOfLoopLatency") if perf is not None else None
    if loops is not None:
        for loop in loops:
            out["loops"].append({
                "name": loop.tag,
                "trip": _num(loop.findtext("TripCount")),
                "latency": _num(loop.findtext("Latency")),
                # ACHIEVED II.  See the module docstring.
                "ii": _num(loop.findtext("PipelineII")),
                "depth": _num(loop.findtext("PipelineDepth")),
            })
    return out


def collect(variant: str) -> dict:
    """Everything the reports say about one variant: its top, and the task body beneath it."""
    rd = HERE / f"proj_{variant}" / "sol1" / "syn" / "report"
    row: dict = {"variant": variant, "built": rd.is_dir(), "nwords": NWORDS[variant],
                 "top": None, "body": None}
    if not rd.is_dir():
        return row
    row["top"] = read_module(rd, variant)
    # The task body is synthesized as its own module; its name is mangled from the C++ symbol, so it
    # is FOUND rather than spelled out -- the mangling is not a thing this witness should assert.
    others = sorted(x.name[: -len("_csynth.xml")] for x in rd.glob("*_csynth.xml")
                    if x.name not in (f"{variant}_csynth.xml", "csynth.xml",
                                      "csynth_design_size.xml"))
    bodies = [m for m in others if "Pipeline" not in m]
    row["body"] = read_module(rd, bodies[0]) if bodies else None
    row["pipelines"] = [read_module(rd, m) for m in others if "Pipeline" in m]
    return row


def fmt(v) -> str:
    return "-" if v is None else str(v)


def main() -> int:
    rows = [collect(v) for v in VARIANTS]

    print("## Achieved II and firing cost, from syn/report/*_csynth.xml\n")
    print("| variant | words/firing | body latency | body interval | loop | trip | achieved II | "
          "pipeline depth |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        if not r["built"]:
            print(f"| `{r['variant']}` | {fmt(r['nwords'])} | **REFUSED — no report** | | | | | |")
            continue
        body = r["body"] or {}
        loops = list(body.get("loops", []))
        for pl in r.get("pipelines") or []:
            if pl:
                loops += pl.get("loops", [])
        if not loops:
            print(f"| `{r['variant']}` | {fmt(r['nwords'])} | {fmt(body.get('latency_max'))} | "
                  f"{fmt(body.get('interval_max'))} | *(no loop)* | - | - | - |")
            continue
        for i, lp in enumerate(loops):
            head = (f"| `{r['variant']}` | {fmt(r['nwords'])} | {fmt(body.get('latency_max'))} | "
                    f"{fmt(body.get('interval_max'))} " if i == 0 else "| | | | ")
            print(f"{head}| `{lp['name']}` | {fmt(lp['trip'])} | **{fmt(lp['ii'])}** | "
                  f"{fmt(lp['depth'])} |")

    print("\n## Cycles per word, derived from csynth as `latency + 1`\n")
    # ASCII only in printed output: this runs on a plain Windows console, where an em-dash arrives
    # as a replacement character and makes the tool look broken.
    print("**These are PESSIMISTIC BY 2 for the looped variants -- use the RTL run.**  `latency + 1`")
    print("is right for a one-word body (confirmed by both `_1` rows, which match RTL exactly) and")
    print("wrong by 2 cycles for a loop body: it gives 13/69 at N=8/64 where RTL measures 11/67.")
    print("The column is kept because it is what a reader would otherwise compute by hand, and")
    print("finding it already computed and already flagged is better than finding it absent.")
    print("See README section 3.\n")
    print("| variant | words/firing | firing latency | cycles/word | vs. 1-per-firing |")
    print("|---|---|---|---|---|")
    base = {}
    for r in rows:
        body = r["body"] or {}
        lat, n = body.get("latency_max"), r["nwords"]
        if r["variant"].endswith("_1") and lat is not None:
            base[r["variant"][:3]] = lat + 1
    for r in rows:
        body = r["body"] or {}
        lat, n = body.get("latency_max"), r["nwords"]
        if not r["built"]:
            print(f"| `{r['variant']}` | {fmt(n)} | **REFUSED** | - | - |")
            continue
        if lat is None or n is None:
            # An unbounded body has no cycles-per-firing.  Saying so is the measurement.
            print(f"| `{r['variant']}` | {fmt(n)} | **unbounded** | *(see the RTL run)* | - |")
            continue
        # A firing occupies latency + 1 FSM states -- the calibration the RF arc uses throughout.
        per_firing = lat + 1
        cpw = per_firing / n
        b = base.get(r["variant"][:3])
        speedup = f"{b / cpw:.2f}x" if b else "-"
        print(f"| `{r['variant']}` | {n} | {per_firing} | **{cpw:.3f}** | {speedup} |")

    missing = [r["variant"] for r in rows if not r["built"]]
    if missing:
        print(f"\n**Not built:** {', '.join(missing)} — see the csynth log for the refusal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
