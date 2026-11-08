"""csynth.py — the SIMD core's generated top through Vitis HLS csynth alone (``plans/simdcore.md``
Stage 5), and its report: resources, the estimated clock, every loop's II and depth.

    r = run_csynth(cfg, "C:/wfsc/scs/mf_l2")     # a SHORT root: Windows MAX_PATH fails csynth silently
    r["lut"], r["loops"]["VLOOP"]                # (II, depth)

The top is :func:`waveflow.simdcore.hls.render_top` (AXI-Stream ports, two ``hls::task``\\ s); the
system flow (``waveflow.simdcore.build``) builds its own.
"""

from __future__ import annotations

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from waveflow.simdcore.config import SimdCoreConfig
from waveflow.simdcore.hls import D_DEFAULT, header_name, render_header, top_name

PART, PERIOD_NS = "xczu48dr-ffvg1517-2-e", 4
BUILD = Path(__file__).resolve().parents[1] / "build"


def _vitis_run() -> str:
    env = os.environ.get("WAVEFLOW_VITIS_RUN")
    if env:
        return env
    from waveflow.toolchain import toolchain

    exe = toolchain.find_vitis_path()
    if not exe:
        raise RuntimeError("Vitis HLS not found")
    return exe


def write_project(cfg: SimdCoreConfig, out: Path, d: int = D_DEFAULT) -> str:
    out.mkdir(parents=True, exist_ok=True)
    (out / header_name(cfg, d)).write_text(render_header(cfg, d))
    top = top_name(cfg, d)
    (out / "top.cpp").write_text(f'#include "{header_name(cfg, d)}"\n')
    o, b = out.as_posix(), BUILD.as_posix()
    (out / "run.tcl").write_text(
        "open_project -reset p\n"
        f"set_top {top}\n"
        f'add_files {{{o}/top.cpp}} -cflags "-I{o} -I{b}"\n'
        "open_solution -reset s -flow_target vivado\n"
        f"set_part {PART}\n"
        f"create_clock -period {PERIOD_NS}\n"
        "csynth_design\nexit\n")
    return top


def run_csynth(cfg: SimdCoreConfig, out: str | Path, d: int = D_DEFAULT) -> dict:
    out = Path(out)
    top = write_project(cfg, out, d)
    with open(out / "run.log", "w") as log:
        subprocess.call([_vitis_run(), "--mode", "hls", "--tcl", "run.tcl"], cwd=out, stdout=log,
                        stderr=subprocess.STDOUT)
    return report(out, top)


def _loops(xml: Path) -> dict:
    out = {}
    root = ET.parse(xml).getroot()
    lat = root.find("PerformanceEstimates/SummaryOfLoopLatency")
    if lat is None:
        return out

    def walk(node):
        for c in node:
            ii = c.findtext("PipelineII")
            if ii is not None:
                out[c.tag] = (ii.strip(), (c.findtext("PipelineDepth") or "?").strip())
            walk(c)

    walk(lat)
    return out


def report(out: str | Path, top: str) -> dict:
    """Resources, the clock estimate and loops (``{name: (II, depth)}``) from ``out``'s csynth."""
    rep = Path(out) / "p" / "s" / "syn" / "report"
    r: dict = {"top": top}
    x = rep / f"{top}_csynth.xml"
    if not x.is_file():
        log = Path(out) / "run.log"
        r["error"] = log.read_text(errors="replace")[-4000:] if log.is_file() else "no report"
        return r
    root = ET.parse(x).getroot()
    res = root.find("AreaEstimates/Resources")
    for k, tag in (("lut", "LUT"), ("ff", "FF"), ("dsp", "DSP"), ("bram", "BRAM_18K"), ("uram", "URAM")):
        v = res.findtext(tag) if res is not None else None
        r[k] = int(v) if v and v.isdigit() else v
    r["clock_ns"] = float(root.findtext("PerformanceEstimates/SummaryOfTimingAnalysis/EstimatedClockPeriod") or 0)
    loops = {}
    for f in sorted(rep.glob("*_csynth.xml")):
        loops.update(_loops(f))
    r["loops"] = loops
    log = Path(out) / "run.log"
    if log.is_file():
        txt = log.read_text(errors="replace")
        r["warnings"] = [ln for ln in txt.splitlines() if re.search(r"WARNING: \[HLS 200-(880|885|871)\]", ln)][:10]
    return r


def loop_depth_ok(rep: dict, d: int) -> bool:
    """Whether the vector loop's depth (``VLOOP``) is at most ``d``, the read-to-write distance the
    hazard countdown assumes (:data:`waveflow.simdcore.hls.D_DEFAULT`): the RTL is exact only then."""
    ii_depth = rep.get("loops", {}).get("VLOOP")
    return ii_depth is not None and int(ii_depth[1]) <= d


__all__ = ["PART", "PERIOD_NS", "loop_depth_ok", "report", "run_csynth", "write_project"]
