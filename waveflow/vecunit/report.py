"""report.py — what a vector unit's harness run measured: per-job cycles from the RTL run, and the
resources csynth estimated for the unit and each task.

    python -m waveflow.vecunit.report <build root>
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from waveflow.vecunit.build import TOP, WORK_DIR


def job_cycles(root: Path) -> list[int]:
    """The cycle each job's status left the unit at RTL (``m_status``, one word a job)."""
    d = Path(root) / WORK_DIR / TOP / "xsi_run" / "vectors" / "m_status"
    return [int(c) for c in np.fromfile(d / "cycles.bin", dtype="<u8")]


def output_cycles(root: Path, port: str = "s_out0") -> list[int]:
    d = Path(root) / WORK_DIR / TOP / "xsi_run" / "vectors" / port
    return [int(c) for c in np.fromfile(d / "cycles.bin", dtype="<u8")]


def _resources(xml: Path) -> dict:
    r = ET.parse(xml).getroot()
    res = r.find("AreaEstimates/Resources")
    if res is None:
        raise ValueError(f"{xml}: no AreaEstimates/Resources")
    out: dict[str, float] = {k: int(res.findtext(t) or 0) for k, t in (("lut", "LUT"), ("ff", "FF"), ("dsp", "DSP"),
                                                       ("bram", "BRAM_18K"))}
    out["clock_ns"] = float(r.findtext("PerformanceEstimates/SummaryOfTimingAnalysis/"
                                       "EstimatedClockPeriod") or 0)
    return out


def resources(root: Path) -> dict[str, dict]:
    """csynth's estimate for the top (``vecunit``) and each task, by role."""
    rep = Path(root) / "vecunit_proj" / "solution1" / "syn" / "report"
    out = {"unit": _resources(rep / "vecunit_csynth.xml")}
    for role in ("load", "exec", "store"):
        cands = sorted(p for p in rep.glob(f"vecunit_{role}_*_csynth.xml") if "Pipeline" not in p.name)
        if cands:
            out[role] = _resources(cands[0])
    return out


def pass_depth(root: Path) -> int | None:
    """The pass loop's pipeline depth, from csynth's log."""
    import re

    log = Path(root) / "logs" / "hls_run_tcl.log"
    m = re.findall(r"Final II = \d+, Depth = (\d+), loop 'PASS'", log.read_text(errors="replace"))
    return int(m[-1]) if m else None


def main(argv=None) -> int:
    root = Path((argv or sys.argv[1:])[0])
    jobs = job_cycles(root)
    print(json.dumps({"job_status_cycles": jobs, "job_intervals": np.diff([0, *jobs]).tolist(),
                      "pass_depth": pass_depth(root), "resources": resources(root)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
