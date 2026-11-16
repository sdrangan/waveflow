#!/usr/bin/env python3
"""Stage 1 witnesses of plans/simdcore.md: run each variant through Vitis HLS and print the table.

Stdlib only, no Waveflow import (a parser bug and a synthesis result must not look alike).

    python run.py [--out C:/wfsc/sw] [--jobs 2] [--report-only] [variant ...]

Builds under a SHORT root (Windows MAX_PATH: a long path fails csynth silently).  Tool: Vitis HLS
2025.1, ``WITNESS_VITIS_RUN`` or ``C:/Xilinx/2025.1/Vitis/bin/vitis-run.bat``.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
VITIS_RUN = os.environ.get("WITNESS_VITIS_RUN", "C:/Xilinx/2025.1/Vitis/bin/vitis-run.bat")
PART = "xczu48dr-ffvg1517-2-e"

# name: (top, source, testbench or None, cflags, steps, what)
V = {
    "w6_l2": ("w6_top", "w6.cpp", "tb6.cpp", "-DSC_L=2", "csim csynth cosim",
              "flattened loop, L 2, dependence false"),
    "w6_l2_nodep": ("w6_top", "w6.cpp", "tb6.cpp", "-DSC_L=2 -DSC_NODEP", "csynth cosim",
                    "same, no dependence pragma (HLS default)"),
    "w6_l2_short": ("w6_top", "w6.cpp", "tb6.cpp", "-DSC_L=2 -DSC_NG=2", "csim csynth cosim",
                    "dependence false, VL/L = 2 < D (hazard case)"),
    "w6_l2_e32mul": ("w6_top", "w6.cpp", "tb6.cpp", "-DSC_L=2 -DSC_E32MUL", "csim csynth",
                     "L 2 with the e32 real multiply"),
    "w2_l4": ("w6_top", "w6.cpp", "tb6.cpp", "-DSC_L=4", "csynth", "L 4"),
    "w2_l8": ("w6_top", "w6.cpp", "tb6.cpp", "-DSC_L=8", "csynth", "L 8"),
    "w2_l16": ("w6_top", "w6.cpp", "tb6.cpp", "-DSC_L=16", "csynth", "L 16"),
    "w1_lane": ("w1_top", "w1.cpp", None, "", "csynth", "one lane, II 1"),
    "w1_lane_fixsh": ("w1_top", "w1.cpp", None, "-DSC_FIXSH", "csynth",
                      "one lane, the wf. shifts fixed (csh 15, rsh 0)"),
    "w1_lane_e32mul":("w1_top", "w1.cpp", None, "-DSC_E32MUL", "csynth", "one lane + e32 multiply"),
    "w3_l2": ("w3_top", "w3.cpp", None, "-DSC_L=2", "csynth", "reduction, L 2"),
    "w3_l4": ("w3_top", "w3.cpp", None, "-DSC_L=4", "csynth", "reduction, L 4"),
    "w3_l8": ("w3_top", "w3.cpp", None, "-DSC_L=8", "csynth", "reduction, L 8"),
    "w3_l16": ("w3_top", "w3.cpp", None, "-DSC_L=16", "csynth", "reduction, L 16"),
    "w5_scalar": ("w5_top", "w5.cpp", "tb5.cpp", "", "csim csynth", "scalar + vector hls::tasks"),
    "w5_scalar_ii2": ("w5_top", "w5.cpp", "tb5.cpp", "-DSC_SII2", "csim csynth",
                      "scalar issue loop at II 2"),
    "w4_dm":("w4_top", "w4.cpp", None, "-DSC_L=2", "csynth", "DM banks + window ping-pong, L 2"),
}


def tcl(name: str) -> str:
    top, src, tb, cf, steps, _ = V[name]
    h = HERE.as_posix()
    lines = [
        "open_project -reset p",
        f"set_top {top}",
        f'add_files {{{h}/{src}}} -cflags "-I{h} {cf}"',
    ]
    if tb:
        lines.append(f'add_files -tb {{{h}/{tb}}} -cflags "-I{h} {cf}"')
    lines += [
        "open_solution -reset s -flow_target vivado",
        f"set_part {PART}",
        "create_clock -period 4",
    ]
    for s in steps.split():
        lines.append({"csim": "csim_design", "csynth": "csynth_design",
                      "cosim": "cosim_design -rtl verilog"}[s])
    lines.append("exit")
    return "\n".join(lines) + "\n"


def run_one(out: Path, name: str) -> str:
    d = out / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.tcl").write_text(tcl(name))
    with open(d / "run.log", "w") as log:
        rc = subprocess.call([VITIS_RUN, "--mode", "hls", "--tcl", "run.tcl"], cwd=d, stdout=log,
                             stderr=subprocess.STDOUT, shell=False)
    return f"done {name} rc={rc}"


def loops(xml: Path) -> dict:
    out = {}
    root = ET.parse(xml).getroot()
    lat = root.find("PerformanceEstimates/SummaryOfLoopLatency")
    if lat is None:
        return out

    def walk(node, prefix=""):
        for c in node:
            ii = c.findtext("PipelineII")
            nm = prefix + c.tag
            if ii is not None:
                out[nm] = (ii.strip(), (c.findtext("PipelineDepth") or "?").strip(),
                           (c.findtext("TripCount") or "?").strip())
            walk(c, nm + "/")

    walk(lat)
    return out


def summary(out: Path, name: str) -> dict:
    top = V[name][0]
    rep = out / name / "p" / "s" / "syn" / "report"
    r: dict = {"name": name}
    x = rep / f"{top}_csynth.xml"
    if x.is_file():
        root = ET.parse(x).getroot()
        res = root.find("AreaEstimates/Resources")
        for k, tag in (("lut", "LUT"), ("ff", "FF"), ("dsp", "DSP"), ("bram", "BRAM_18K")):
            r[k] = res.findtext(tag) if res is not None else "?"
        r["clock"] = root.findtext("PerformanceEstimates/SummaryOfTimingAnalysis/EstimatedClockPeriod")
        r["latency"] = root.findtext("PerformanceEstimates/SummaryOfOverallLatency/Worst-caseLatency")
        lp = {}
        for f in sorted(rep.glob("*_csynth.xml")):
            for k, v in loops(f).items():
                lp[f"{f.stem.replace('_csynth', '')}:{k}"] = v
        r["loops"] = lp
    log = out / name / "run.log"
    if log.is_file():
        txt = log.read_text(errors="replace")
        r["tb"] = [ln.strip() for ln in txt.splitlines() if re.match(r"\s*(w\d|MISMATCH)", ln)][:6]
        m = re.search(r"\*\*\* C/RTL co-simulation finished: (\w+)", txt)
        r["cosim"] = m.group(1) if m else None
    crpt = out / name / "p" / "s" / "sim" / "report" / f"{top}_cosim.rpt"
    if crpt.is_file():
        for ln in crpt.read_text(errors="replace").splitlines():
            if "Verilog" in ln and "|" in ln:
                r["cosim_rpt"] = re.sub(r"\s+", " ", ln.strip())
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="C:/wfsc/sw")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("variants", nargs="*")
    a = ap.parse_args()
    out = Path(a.out)
    names = a.variants or list(V)
    if not a.report_only:
        with ThreadPoolExecutor(a.jobs) as ex:
            for msg in ex.map(lambda n: run_one(out, n), names):
                print(msg, flush=True)
    for n in names:
        s = summary(out, n)
        print(f"\n== {n}: {V[n][5]}  [Vitis HLS 2025.1, {PART}, 4 ns]")
        for k in ("lut", "ff", "dsp", "bram", "clock", "latency", "cosim", "cosim_rpt"):
            if s.get(k) is not None:
                print(f"   {k}: {s[k]}")
        for k, v in s.get("loops", {}).items():
            print(f"   loop {k}: II {v[0]} depth {v[1]} trip {v[2]}")
        for ln in s.get("tb", []):
            print(f"   tb: {ln}")


if __name__ == "__main__":
    sys.exit(main())
