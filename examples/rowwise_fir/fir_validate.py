"""fir_validate.py — timeline validation of the matrix-LT FIR sim vs RTL cosim.

Per plans/load_compute_store.md "Calibration with logging": match the **bus-visible**
event timeline of the block-fidelity sim against the generated kernel's RTL cosim.
RTL exposes the X-read burst span and the Y-write burst span; the sim exposes
``load_begin``/``load_end`` and ``store_begin``/``store_end``.  Both are anchored at
their first bus event (cancels the constant command-fetch offset), then the per-stage
*durations* are compared (the read model from the X-read span, the write model from the
Y-write span; compute is the load_end->store_begin gap).

Single-command validation only.  Timing params are PROVISIONAL, so a residual is
expected and reported, not asserted — the per-stage cosim calibration (≥3 n_row,
back-to-back 2-matrix cosim, sklearn fit) is the deferred follow-step that drives the
residual to <eps.  This script is the harness that consumes those numbers.

Run with the project venv (needs Vitis HLS + Vivado xsim)::

    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_validate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.toolchain import toolchain  # noqa: E402

from examples.rowwise_fir import fir_build  # noqa: E402
from examples.rowwise_fir.fir_sim import FIRSim, make_specs  # noqa: E402

CLK_NS = 10.0
N_ROWS, N_COLS = 4, 64


def _burst_beats(b: dict) -> int:
    return sum(1 for bt in b.get("beat_type", []) if bt == 0)


def cosim_spans() -> dict:
    """Cosim the generated kernel (port trace) and extract X-read / Y-write burst spans."""
    from vcdvcd import VCDVCD

    from waveflow.scripts.xsim_vcd import run_xsim_vcd
    from waveflow.utils.vcd import VcdParser

    fir_build.generate(N_ROWS, N_COLS)
    env = {"WAVEFLOW_ROWWISE_FIR_COSIM": "1", "WAVEFLOW_ROWWISE_FIR_TRACE_LEVEL": "port"}
    res = toolchain.run_vitis_hls_result(fir_build.GEN_DIR / "run.tcl",
                                         work_dir=fir_build.GEN_DIR, capture_output=True, env=env)
    out = (res.get("stdout") or "") + (res.get("stderr") or "")
    if "WAVEFLOW_SUCCESS" not in out:
        raise RuntimeError("cosim failed:\n" + out[-2000:])

    vcd_path = run_xsim_vcd(top="fir", comp="fir_gen_proj", out="dump.vcd",
                            soln="solution1", trace_level="port", workdir=fir_build.GEN_DIR)
    vcd = VCDVCD(str(vcd_path), signals=None, store_tvs=True)
    vp = VcdParser(vcd)
    clk = vp.add_clock_signal()
    aximm_sigs, _ = vp.add_aximm_signals(prefix="m_axi_gmem_", dir="both",
                                         lite_only=False, short_name_prefix="gmem_")
    write_bursts, read_bursts, clk_period = vp.extract_aximm_bursts(clk_name=clk, aximm_sigs=aximm_sigs)

    def span(bursts):
        if not bursts:
            return None
        t0 = min(float(b.get("data_tstart", b["tstart"])) for b in bursts)
        t1 = max(float(b["data_tend"]) for b in bursts)
        return {"start_ns": t0, "end_ns": t1, "dur_cyc": (t1 - t0) / float(clk_period),
                "words": sum(_burst_beats(b) for b in bursts)}

    return {
        "clk_period_ns": float(clk_period),
        "x_read": span(read_bursts),
        "y_write": span(write_bursts),
    }


def sim_spans() -> dict:
    """Run the block-fidelity sim (single 4x64) and extract the stage spans (anchored at cmd_arrive)."""
    specs = make_specs([(N_ROWS, N_COLS)])
    sim = FIRSim(specs)
    sim.run()
    ev = {e["event"]: e["t"] for e in sim.accel.events if e["tx_id"] == 0}
    anchor = ev["cmd_arrive"]
    to_cyc = lambda a, b: (ev[b] - ev[a]) / (CLK_NS * 1e-9)
    return {
        "load_span_cyc": to_cyc("load_begin", "load_end"),
        "store_span_cyc": to_cyc("store_begin", "store_end"),
        "compute_gap_cyc": to_cyc("load_end", "store_begin"),
        "total_cyc": (ev["resp_sent"] - anchor) / (CLK_NS * 1e-9),
    }


def main() -> None:
    print(f"=== matrix-LT FIR timeline validation ({N_ROWS}x{N_COLS}, single command) ===")
    sim = sim_spans()
    print("SIM (block-fidelity):")
    for k, v in sim.items():
        print(f"  {k:18s} = {v:8.1f} cyc")

    cos = cosim_spans()
    print(f"COSIM (RTL, clk={cos['clk_period_ns']}ns):")
    print(f"  x_read  : dur={cos['x_read']['dur_cyc']:8.1f} cyc  words={cos['x_read']['words']}")
    print(f"  y_write : dur={cos['y_write']['dur_cyc']:8.1f} cyc  words={cos['y_write']['words']}")

    def rel(a, b):
        return abs(a - b) / b if b else float("nan")

    read_err = rel(sim["load_span_cyc"], cos["x_read"]["dur_cyc"])
    write_err = rel(sim["store_span_cyc"], cos["y_write"]["dur_cyc"])
    print("RESIDUAL (sim vs cosim span duration; PROVISIONAL params):")
    print(f"  read-channel  : sim {sim['load_span_cyc']:.1f} vs cosim {cos['x_read']['dur_cyc']:.1f} "
          f"-> {read_err*100:.1f}%")
    print(f"  write-channel : sim {sim['store_span_cyc']:.1f} vs cosim {cos['y_write']['dur_cyc']:.1f} "
          f"-> {write_err*100:.1f}%")
    print("  (residual closes via the deferred per-stage calibration — see FIRTiming)")

    out = HERE / "results" / "timeline_single.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"sim": sim, "cosim": cos,
                               "read_rel_err": read_err, "write_rel_err": write_err}, indent=2) + "\n",
                   encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
