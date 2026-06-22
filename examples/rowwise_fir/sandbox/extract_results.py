"""extract_results.py — parse the csynth reports into committed JSON artifacts.

Reads the two solutions produced by run.tcl and emits, under results/:
  * csynth_singleport.json  (X+Y share bundle=gmem)
  * csynth_split.json       (separate gmemX / gmemY bundles)

Each captures the headline Phase-1 evidence: the compute inner-loop II (=1),
the per-loop pipeline IIs (load / compute / store all =1), the inferred m_axi
bursts (proving the row load/store coalesce), and the top-level latency/interval.

The single-port vs split top latency/interval come out *identical* — that is the
finding (a single m_axi bundle is full-duplex, so the X-read + Y-write pair does
not contend); see results/fir_sandbox_results.md.

Loop IIs and resources are parsed with waveflow.utils.csynthparse.CsynthParser;
the burst table and top overall-latency are parsed from the report directly.

Run with the project venv (after gen_data.py + run.tcl)::

    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe extract_results.py
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from waveflow.utils.csynthparse import CsynthParser

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

# (output name, project dir, solution name)
SOLUTIONS = [
    ("singleport", "waveflow_rowwise_fir_proj", "solution_singleport"),
    ("split", "waveflow_rowwise_fir_split_proj", "solution_split"),
]

TOP = "fir_accel"


def _loop_iis(parser: CsynthParser) -> dict:
    """Per-loop pipeline II keyed by loop label (LOAD / COMPUTE / STORE / ...)."""
    parser.get_loop_pipeline_info()
    out = {}
    for key, row in parser.loop_df.iterrows():
        # key is "<module>:<loop>"; keep the loop label.
        label = str(key).split(":")[-1].strip()
        ii = row.get("PipelineII")
        out[label] = None if ii is None or (isinstance(ii, float) and ii != ii) else int(ii)
    return out


def _top_latency(sol_path: Path) -> dict:
    """Top-module overall latency/interval from csynth.xml."""
    xml = sol_path / "syn" / "report" / "csynth.xml"
    root = ET.parse(xml).getroot()
    perf = root.find("PerformanceEstimates")
    out = {}
    if perf is not None:
        lat = perf.find("SummaryOfOverallLatency")
        if lat is not None:
            for tag in (
                "Best-caseLatency",
                "Average-caseLatency",
                "Worst-caseLatency",
                "Interval-min",
                "Interval-max",
            ):
                el = lat.find(tag)
                if el is not None and el.text is not None:
                    try:
                        out[tag] = int(el.text)
                    except ValueError:
                        out[tag] = el.text
    return out


def _inferred_bursts(sol_path: Path) -> list[dict]:
    """Parse the 'Inferred Burst Summary' table out of csynth.rpt."""
    rpt = (sol_path / "syn" / "report" / "csynth.rpt").read_text(encoding="utf-8")
    lines = rpt.splitlines()
    bursts: list[dict] = []
    in_summary = False
    for line in lines:
        if "Inferred Burst Summary" in line:
            in_summary = True
            continue
        if in_summary:
            if line.startswith("* ") or "All M_AXI Variable Accesses" in line:
                break
            # data rows look like: | iface | dir | length | width | loop | loc |
            if line.strip().startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) >= 5 and cells[0] not in ("HW Interface", ""):
                    bursts.append(
                        {
                            "interface": cells[0],
                            "direction": cells[1],
                            "length": cells[2],
                            "width": cells[3],
                            "loop": cells[4],
                        }
                    )
    return bursts


def extract_one(name: str, proj: str, sol: str) -> dict:
    sol_path = HERE / proj / sol
    parser = CsynthParser(sol_path=str(sol_path))
    loop_iis = _loop_iis(parser)
    compute_ii = next((v for k, v in loop_iis.items() if "COMPUTE" in k.upper()), None)

    parser.get_resources()
    total = parser.res_df.loc["Total"].to_dict() if "Total" in parser.res_df.index else {}

    rec = {
        "solution": name,
        "top": TOP,
        "part": "xc7z020clg484-1",
        "clk_period_ns": 10.0,
        "compute_loop_ii": compute_ii,
        "loop_ii": loop_iis,
        "top_latency": _top_latency(sol_path),
        "inferred_bursts": _inferred_bursts(sol_path),
        "resources_total": {k: (int(v) if str(v).lstrip("-").isdigit() else v) for k, v in total.items()},
    }
    return rec


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    recs = {}
    for name, proj, sol in SOLUTIONS:
        rec = extract_one(name, proj, sol)
        path = RESULTS / f"csynth_{name}.json"
        path.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        recs[name] = rec
        print(f"wrote {path}")
        print(f"  compute_loop_ii = {rec['compute_loop_ii']}  loop_ii = {rec['loop_ii']}")
        print(f"  bursts = {[(b['direction'], b['loop'], b['length']) for b in rec['inferred_bursts']]}")
        print(f"  top_latency = {rec['top_latency']}")
    # Sanity: single-port and split should match (the full-duplex finding).
    if "singleport" in recs and "split" in recs:
        a, b = recs["singleport"]["top_latency"], recs["split"]["top_latency"]
        print(f"single-port vs split top latency identical: {a == b}")


if __name__ == "__main__":
    main()
