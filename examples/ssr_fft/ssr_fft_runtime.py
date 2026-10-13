"""ssr_fft_runtime.py -- how the tool run times grow with the number of frames in a scenario.

    python -m examples.ssr_fft.ssr_fft_runtime                          # L=1024, 8 .. 200 frames
    python -m examples.ssr_fft.ssr_fft_runtime --frames 8 100 --synth   # also time a fresh csynth

For one ``SsrFft`` configuration and each frame count, the wall time of:

- **pysim** -- building the testbench graph, writing the scenario, and ``run_sim``;
- **RTL (XSI)** -- ``rtl.run_xsi`` split into its runner phases (``compile_rtl``, ``elaborate``,
  ``compile_tb``, ``simulate``) from the event spans.  The build is incremental: only the first run
  compiles and elaborates, and a new frame count changes the vectors alone;
- **golden** -- the bit-exact reference both backends are checked against.

Synthesis compiles the DUT alone (``gen/ssr_fft.cpp``), so the frame count cannot reach it: it is
timed once, with ``--synth``.  Every run checks every frame's bits.  Results go to ``runtime.json``,
one entry per configuration (``L<L>_<reorder>``).
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import date
from pathlib import Path

import numpy as np

from waveflow import events
from waveflow.dsp.ssr_fft import rtl
from waveflow.dsp.ssr_fft.testbench import (R, SsrFftTB, golden, pysim_frame_cycles,
                                            pysim_output, write_scenario)
from waveflow.simulation.simulation import Simulation

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNTIME = HERE / "runtime.json"
FRAMES = (8, 25, 50, 100, 200)
PHASES = ("compile_rtl", "elaborate", "compile_tb", "simulate")


def _same(got, want) -> bool:
    return len(got) == len(want) and all(
        np.array_equal(g[0], w[0]) and np.array_equal(g[1], w[1]) for g, w in zip(got, want))


def time_pysim(root: Path, length: int, n: int, reorder: str, want) -> dict:
    t0 = time.perf_counter()
    tb = SsrFftTB(name="tb", sim=Simulation(), root=root, length=length, n_frames=n,
                  reorder=reorder)
    t1 = time.perf_counter()
    write_scenario(root, n, length, **tb.config)
    t2 = time.perf_counter()
    tb.sim.run_sim()
    t3 = time.perf_counter()
    cyc = pysim_frame_cycles(tb)
    return {"build_s": t1 - t0, "scenario_s": t2 - t1, "sim_s": t3 - t2, "total_s": t3 - t0,
            "bits_exact": _same(pysim_output(tb), want),
            "interval": sorted({round(x) for x in np.diff(cyc)}), "cycles": round(cyc[-1])}


def time_xsi(root: Path, length: int, n: int, reorder: str) -> dict:
    t0 = time.perf_counter()
    rtl.generate(root, length, n_frames=n, reorder=reorder)
    t1 = time.perf_counter()
    with events.collect() as spans:
        rtl.run_xsi(root)
    t2 = time.perf_counter()
    phases = {p: sum(s["elapsed"] for s in spans if s["kind"] == "phase" and s["name"] == p)
              for p in PHASES}
    bits = rtl.check_bits(root, length, n)
    frames = rtl.frame_times(root, length)
    return {"generate_s": t1 - t0, "run_xsi_s": t2 - t1, **{f"{p}_s": v for p, v in phases.items()},
            "bits_exact": len(bits) == n and all(bits),
            "interval": sorted({int(x) for x in np.diff([f["done"] for f in frames])}),
            "cycles": int(frames[-1]["done"]) if frames else None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--length", type=int, default=1024)
    ap.add_argument("--frames", type=int, nargs="+", default=list(FRAMES))
    ap.add_argument("--reorder", default="pingpong", choices=("pingpong", "sob"))
    ap.add_argument("--synth", action="store_true", help="csynth from scratch, timed")
    ap.add_argument("--no-xsi", action="store_true")
    a = ap.parse_args()

    # A short path: csynth fails silently past the Windows path limit.
    root = REPO / "work" / "ssr_fft" / f"rt_L{a.length}_{a.reorder}"
    root.mkdir(parents=True, exist_ok=True)
    data = json.loads(RUNTIME.read_text(encoding="utf-8")) if RUNTIME.exists() else {}
    out = {"L": a.length, "reorder": a.reorder, "words_per_frame": a.length // R,
           "date": date.today().isoformat(), "synth_s": None, "runs": []}
    data[f"L{a.length}_{a.reorder}"] = out

    if not a.no_xsi:
        rtl.generate(root, a.length, n_frames=min(a.frames), reorder=a.reorder)
        if a.synth or not (root / f"{rtl.TOP}_proj").is_dir():
            shutil.rmtree(root / f"{rtl.TOP}_proj", ignore_errors=True)
            t = time.perf_counter()
            rtl.synth(root)
            out["synth_s"] = time.perf_counter() - t
            print(f"csynth: {out['synth_s']:.1f} s", flush=True)

    for n in sorted(a.frames):
        t = time.perf_counter()
        want = golden(n, a.length)
        row = {"n_frames": n, "golden_s": time.perf_counter() - t,
               "pysim": time_pysim(root, a.length, n, a.reorder, want)}
        if not a.no_xsi:
            row["xsi"] = time_xsi(root, a.length, n, a.reorder)
        out["runs"].append(row)
        p, x = row["pysim"], row.get("xsi", {})
        print(f"n={n:4d}  golden {row['golden_s']:6.2f} s | pysim {p['total_s']:7.2f} s "
              f"(sim {p['sim_s']:7.2f}, exact {p['bits_exact']}, interval {p['interval']}) | "
              f"xsi {x.get('run_xsi_s', 0):6.2f} s (simulate {x.get('simulate_s', 0):6.2f}, "
              f"exact {x.get('bits_exact')}, interval {x.get('interval')})", flush=True)
        RUNTIME.write_text(json.dumps(dict(sorted(data.items())), indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
