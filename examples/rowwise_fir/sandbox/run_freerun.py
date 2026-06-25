"""run_freerun.py — driver for the free-running streaming FIR sandbox
(plans/fir_freerun_sandbox.md).

Drives Vitis HLS 2025.1 (via waveflow.toolchain) and records findings into
results_freerun/measurements.json:

  csim  : every scenario C-sims bit-exact (gate 1 / 4 functional).
  csynth: per-stage II + the DATAFLOW region interval.
  cosim : single/two/three -> total RTL cycles -> the STEADY per-job period
          (two-single, three-two); compared to the control-driven ~1467 cyc/cmd.
          clean -> varying-size bit-exact; error -> per-job error + restart on RTL.

Run with the project venv from anywhere::

    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_freerun.py --smoke
    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_freerun.py --cosim
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

TCL = HERE / "run_freerun.tcl"
SOL = HERE / "waveflow_fir_freerun_proj" / "solution_freerun"
RESULTS = HERE / "results_freerun"
TOP = "fir"
CONTROL_DRIVEN_PERIOD = 1467   # cyc/cmd, dflow_notes.md Finding 4 (the sequential baseline)


def run(scenario: str, cosim: bool, trace: str = "none") -> str:
    env = {"WAVEFLOW_FIR_FR_SCENARIO": scenario}
    if cosim:
        env["WAVEFLOW_FIR_FR_COSIM"] = "1"
        env["WAVEFLOW_FIR_FR_TRACE"] = trace
    last = ""
    for attempt in (1, 2):
        tag = f"{scenario}{' +cosim' if cosim else ''}"
        print(f"[{tag}] vitis-run attempt {attempt} ...", flush=True)
        res = toolchain.run_vitis_hls_result(TCL, work_dir=HERE, capture_output=True, env=env)
        last = (res.get("stdout") or "") + (res.get("stderr") or "")
        if "WAVEFLOW_SUCCESS" in last:
            return last
        if not cosim:
            break   # csim/csynth failures are deterministic; no retry
    if "WAVEFLOW_SUCCESS" not in last:
        raise RuntimeError(f"[{scenario}] run_freerun.tcl did not succeed.\n--- tail ---\n{last[-3500:]}")
    return last


def cosim_cycles() -> int:
    cyc = CosimReportParser(sol_path=str(SOL), top=TOP).get_transaction_cycles()
    if cyc is None:
        raise RuntimeError("no cosim cycles parsed")
    return int(cyc)


def csynth_summary() -> dict:
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
                out["loops"][loop.tag] = ii.text if ii is not None else None
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="csim+csynth all scenarios (no cosim)")
    ap.add_argument("--cosim", action="store_true", help="full run incl. cosim measurement passes")
    args = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    meas: dict = {"clk_ns": 10.0, "control_driven_period_cyc": CONTROL_DRIVEN_PERIOD}

    if args.smoke or not args.cosim:
        for scn in ("single", "two", "three", "clean", "error"):
            run(scn, cosim=False)
            print(f"  csim OK: {scn}", flush=True)
        meas["csynth"] = csynth_summary()
        print(json.dumps(meas["csynth"], indent=2))

    if args.cosim:
        cyc, status = {}, {}
        for scn in ("single", "two", "three", "clean", "error"):
            try:
                run(scn, cosim=True)
                status[scn] = "PASS"
                if scn in ("single", "two", "three"):
                    cyc[scn] = cosim_cycles()
                    print(f"  cosim {scn}: PASS, {cyc[scn]} cycles", flush=True)
                else:
                    print(f"  cosim {scn}: PASS", flush=True)
            except Exception as e:  # noqa: BLE001
                status[scn] = f"FAIL: {str(e)[:240]}"
                print(f"  cosim {scn}: FAIL\n{e}", flush=True)
        meas["cosim_status"] = status
        meas["cosim_cycles"] = cyc
        meas["csynth"] = csynth_summary()
        if {"single", "two", "three"} <= cyc.keys():
            p21 = cyc["two"] - cyc["single"]
            p32 = cyc["three"] - cyc["two"]
            meas["per_job_period_two_minus_single"] = p21
            meas["per_job_period_three_minus_two"] = p32
            meas["steady_per_job_period_cyc"] = p32
            meas["speedup_vs_control_driven"] = round(CONTROL_DRIVEN_PERIOD / p32, 3) if p32 else None

    (RESULTS / "measurements.json").write_text(json.dumps(meas, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {RESULTS / 'measurements.json'}")


if __name__ == "__main__":
    main()
