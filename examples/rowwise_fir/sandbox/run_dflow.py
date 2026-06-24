"""run_dflow.py — driver for the control-driven DATAFLOW FIR sandbox
(plans/fir-cleanup.md §1 Phase 1).

Drives Vitis HLS 2025.1 (via waveflow.toolchain) through the four Phase-1
de-risk passes and records the findings into results_dflow/measurements.json:

  csim   : every scenario C-sims bit-exact (gate).
  csynth : per-stage II/latency + the DATAFLOW region interval (overlap evidence).
  cosim  : single / two / three  -> total RTL cycles -> per-command seam + overlap;
           clean                 -> varying-size bit-exact RTL;
           error                 -> halt + restart on real RTL.

Run with the project venv from anywhere::

    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_dflow.py --smoke
    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_dflow.py --cosim
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from waveflow.toolchain import toolchain                       # noqa: E402
from waveflow.utils.cosimparse import CosimReportParser        # noqa: E402

TCL = HERE / "run_dflow.tcl"
PROJ = HERE / "waveflow_fir_dflow_proj"
SOL = PROJ / "solution_dflow"
RESULTS = HERE / "results_dflow"
TOP = "fir"


def run(scenario: str, cosim: bool, trace: str = "none") -> str:
    env = {"WAVEFLOW_FIR_DFLOW_SCENARIO": scenario}
    if cosim:
        env["WAVEFLOW_FIR_DFLOW_COSIM"] = "1"
        env["WAVEFLOW_FIR_DFLOW_TRACE"] = trace
    last = ""
    for attempt in (1, 2):
        tag = f"{scenario}{' +cosim' if cosim else ''}"
        print(f"[{tag}] vitis-run attempt {attempt} ...", flush=True)
        res = toolchain.run_vitis_hls_result(TCL, work_dir=HERE, capture_output=True, env=env)
        last = (res.get("stdout") or "") + (res.get("stderr") or "")
        if "WAVEFLOW_SUCCESS" in last:
            return last
        if not cosim:
            break   # csynth/csim failures are deterministic; no retry
    if "WAVEFLOW_SUCCESS" not in last:
        raise RuntimeError(f"[{scenario}] run_dflow.tcl did not succeed.\n--- tail ---\n{last[-3000:]}")
    return last


def cosim_cycles() -> int:
    cyc = CosimReportParser(sol_path=str(SOL), top=TOP).get_transaction_cycles()
    if cyc is None:
        raise RuntimeError("no cosim cycles parsed")
    return int(cyc)


def csynth_summary() -> dict:
    """Per-loop pipeline II + the top/region latency/interval from csynth.xml."""
    xml = SOL / "syn" / "report" / "csynth.xml"
    root = ET.parse(xml).getroot()
    perf = root.find("PerformanceEstimates")
    out: dict = {"loops": {}}
    if perf is not None:
        summ = perf.find("SummaryOfOverallLatency")
        if summ is not None:
            for k in ("Best-caseLatency", "Worst-caseLatency",
                      "Interval-min", "Interval-max", "PipelineType"):
                el = summ.find(k)
                if el is not None:
                    out[k] = el.text
        loops = perf.find("SummaryOfLoopLatency")
        if loops is not None:
            for loop in loops:
                ii = loop.find("PipelineII")
                lat = loop.find("IterationLatency")
                out["loops"][loop.tag] = {
                    "PipelineII": ii.text if ii is not None else None,
                    "IterationLatency": lat.text if lat is not None else None,
                }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="csim+csynth of clean+error only (no cosim)")
    ap.add_argument("--cosim", action="store_true", help="full run incl. cosim measurement passes")
    args = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    meas: dict = {"clk_ns": 10.0}

    if args.smoke or not args.cosim:
        for scn in ("clean", "error", "single", "two", "three"):
            run(scn, cosim=False)
            print(f"  csim OK: {scn}", flush=True)
        meas["csynth_three"] = csynth_summary()   # last (three) solution on disk
        print(json.dumps(meas["csynth_three"], indent=2))

    if args.cosim:
        cyc = {}
        status = {}
        # Run every scenario; record pass/fail + cycles. Don't abort the whole
        # run on one scenario (so the cycle points are still collected).
        for scn in ("clean", "error", "single", "two", "three"):
            try:
                run(scn, cosim=True)
                status[scn] = "PASS"
                if scn in ("single", "two", "three"):
                    cyc[scn] = cosim_cycles()
                    print(f"  cosim {scn}: PASS, {cyc[scn]} cycles", flush=True)
                else:
                    print(f"  cosim {scn}: PASS", flush=True)
            except Exception as e:  # noqa: BLE001 -- record and continue
                status[scn] = f"FAIL: {str(e)[:200]}"
                print(f"  cosim {scn}: FAIL\n{e}", flush=True)
        meas["cosim_status"] = status
        meas["cosim_cycles"] = cyc
        meas["csynth"] = csynth_summary()
        # seam = per-command increment; overlap vs a serialized stage estimate
        if {"single", "two", "three"} <= cyc.keys():
            meas["per_command_increment_two_minus_single"] = cyc["two"] - cyc["single"]
            meas["per_command_increment_three_minus_two"] = cyc["three"] - cyc["two"]

    (RESULTS / "measurements.json").write_text(json.dumps(meas, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {RESULTS / 'measurements.json'}")


if __name__ == "__main__":
    main()
