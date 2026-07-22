"""run_fir.py — Stage-A RTL gate driver for the generated free-running FIR.

Codegen once (fir_build.generate()), patch the generated m_axi depth (codegen can't infer the
hook's opaque gmem extent), then drive run.tcl per scenario (csim + csynth + optional cosim) and
collect the cosim per-job period:

  single / two / three -> total RTL cycles -> steady per-job period (two-single, three-two),
                          compared to the control-driven sandbox's ~1467 and the freerun ~704.
  clean                -> varying-size bit-exact;  error -> per-job error + restart, all on RTL.

Run with the project venv::

    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_fir.py --smoke
    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_fir.py --cosim
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.toolchain import toolchain                       # noqa: E402
from waveflow.utils.cosimparse import CosimReportParser        # noqa: E402

import examples.rowwise_fir.fir_build as fir_build             # noqa: E402

TCL = HERE / "run.tcl"
SOL = HERE / "waveflow_fir_proj" / "solution1"
RESULTS = HERE / "results_freerun"
TOP = "fir"
GMEM_DEPTH = 8192                 # must match fir_tb.cpp's GMEM_DEPTH
FREERUN_PERIOD = 704             # sandbox freerun cyc/job
CONTROL_DRIVEN_PERIOD = 1467     # control-driven sandbox cyc/cmd


def codegen_and_patch() -> None:
    """Generate the kernel, then patch gen/fir.hpp's m_axi depth so cosim's memory model
    spans the testbench gmem array (codegen defaults it to 1 — the hook's gmem use is opaque)."""
    fir_build.generate()
    hpp = HERE / "gen" / "fir.hpp"
    txt = hpp.read_text(encoding="utf-8")
    if "m_mem_depth" not in txt:
        raise RuntimeError("m_mem_depth not found in gen/fir.hpp")
    patched = re.sub(r"static const int m_mem_depth = \d+;",
                     f"static const int m_mem_depth = {GMEM_DEPTH};", txt)
    hpp.write_text(patched, encoding="utf-8")
    print(f"  codegen done; patched m_mem_depth -> {GMEM_DEPTH}", flush=True)


def run(scenario: str, cosim: bool, trace: str = "none") -> str:
    env = {"WAVEFLOW_FIR_SCENARIO": scenario}
    if cosim:
        env["WAVEFLOW_FIR_COSIM"] = "1"
        env["WAVEFLOW_FIR_TRACE"] = trace
    last = ""
    for attempt in (1, 2):
        print(f"[{scenario}{' +cosim' if cosim else ''}] vitis-run attempt {attempt} ...", flush=True)
        res = toolchain.run_vitis_hls_result(TCL, work_dir=HERE, capture_output=True, env=env)
        last = (res.get("stdout") or "") + (res.get("stderr") or "")
        if "WAVEFLOW_SUCCESS" in last:
            return last
        if not cosim:
            break
    if "WAVEFLOW_SUCCESS" not in last:
        raise RuntimeError(f"[{scenario}] run.tcl did not succeed.\n--- tail ---\n{last[-3500:]}")
    return last


def cosim_cycles() -> int:
    cyc = CosimReportParser(sol_path=str(SOL), top=TOP).get_transaction_cycles()
    if cyc is None:
        raise RuntimeError("no cosim cycles parsed")
    return int(cyc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="codegen + csim/csynth all scenarios")
    ap.add_argument("--cosim", action="store_true", help="codegen + cosim measurement passes")
    args = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    codegen_and_patch()
    meas: dict = {"clk_ns": 10.0, "freerun_sandbox_period_cyc": FREERUN_PERIOD,
                  "control_driven_period_cyc": CONTROL_DRIVEN_PERIOD}

    if args.smoke or not args.cosim:
        for scn in ("single", "two", "three", "clean", "error"):
            run(scn, cosim=False)
            print(f"  csim OK: {scn}", flush=True)

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
        if {"single", "two", "three"} <= cyc.keys():
            meas["per_job_period_two_minus_single"] = cyc["two"] - cyc["single"]
            meas["per_job_period_three_minus_two"] = cyc["three"] - cyc["two"]
            meas["steady_per_job_period_cyc"] = cyc["three"] - cyc["two"]

    (RESULTS / "measurements.json").write_text(json.dumps(meas, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {RESULTS / 'measurements.json'}")


if __name__ == "__main__":
    main()
