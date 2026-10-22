#!/usr/bin/env python3
"""Print the witness table from the csynth reports: stdlib only, no Waveflow import (a parser bug and a
synthesis result must not look alike).

Reads, per variant ``OUT/<variant>/p/s/syn/report``: the achieved II of every pipelined loop
(``SummaryOfLoopLatency/<loop>/PipelineII`` -- the achieved value, not the target), the estimated clock,
and the resources of the top; and the peak memory of the run (``/usr/bin/time -v``, ``time.log``), with
the co-simulation verdict when ``cosim_design`` ran.

    python3 report.py OUT_DIR [variant ...]
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def loops(root) -> dict:
    out = {}
    lat = root.find("PerformanceEstimates/SummaryOfLoopLatency")
    if lat is None:
        return out

    def walk(node, prefix=""):
        for child in node:
            ii = child.findtext("PipelineII")
            name = prefix + child.tag
            if ii is not None:
                out[name] = ii.strip()
            walk(child, name + "/")

    walk(lat)
    return out


def module(rep: Path, name: str) -> dict | None:
    p = rep / f"{name}_csynth.xml"
    if not p.is_file():
        return None
    root = ET.parse(p).getroot()
    res = root.find("AreaEstimates/Resources")
    get = lambda tag: res.findtext(tag) if res is not None else None  # noqa: E731
    return {
        "clock": root.findtext("PerformanceEstimates/SummaryOfTimingAnalysis/EstimatedClockPeriod"),
        "dsp": get("DSP"), "lut": get("LUT"), "ff": get("FF"), "bram": get("BRAM_18K"),
        "loops": loops(root),
    }


def peak_gb(d: Path) -> str:
    t = d / "time.log"
    if not t.is_file():
        return "?"
    m = re.search(r"Maximum resident set size \(kbytes\): (\d+)", t.read_text(errors="replace"))
    return f"{int(m.group(1)) / 1e6:.2f}" if m else "?"


def cosim(d: Path) -> str:
    log = d / "run.log"
    if not log.is_file():
        return ""
    text = log.read_text(errors="replace")
    if "C/RTL co-simulation finished: PASS" in text:
        return "PASS"
    if "C/RTL co-simulation finished: FAIL" in text or "COSIM" in text and "FAIL" in text:
        return "FAIL"
    return ""


def main() -> int:
    out = Path(sys.argv[1])
    variants = sys.argv[2:] or sorted(p.name for p in out.iterdir() if p.is_dir())
    print(f"{'variant':14} {'clock':>6} {'DSP':>5} {'LUT':>7} {'FF':>7} {'BRAM':>5} {'GB':>5} {'cosim':>5}  pipelined loops (achieved II)")
    for v in variants:
        d = out / v
        rep = d / "p" / "s" / "syn" / "report"
        top = module(rep, "w5_top" if v.startswith("w5") else "vexec_top")
        if top is None:
            print(f"{v:14} NO REPORT (csynth failed: see {d / 'run.log'})")
            continue
        iis = {}
        for x in rep.glob("*_csynth.xml"):
            m = module(rep, x.name[: -len("_csynth.xml")])
            if m:
                iis.update({f"{x.name[:-11]}:{k}": ii for k, ii in m["loops"].items()})
        ii_text = " ".join(f"{k}={ii}" for k, ii in sorted(iis.items()))
        print(f"{v:14} {top['clock']:>6} {top['dsp']:>5} {top['lut']:>7} {top['ff']:>7} {top['bram']:>5} "
              f"{peak_gb(d):>5} {cosim(d):>5}  {ii_text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
