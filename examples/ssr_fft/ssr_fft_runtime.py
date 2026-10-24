"""ssr_fft_runtime.py -- what a frame costs: the untimed bit-exact model, Waveflow's pysim, the RTL.

    python -m examples.ssr_fft.ssr_fft_runtime                   # L=1024: untimed + both pysims
    python -m examples.ssr_fft.ssr_fft_runtime --rtl             # ... + the RTL under XSI
    python -m examples.ssr_fft.ssr_fft_runtime --rtl --synth     # ... + a fresh csynth, timed

The three ways to run the same bit-exact FFT on the same frames
(``docs/evaluation/sim_speed/fft.md`` is written from what this records):

- **untimed** -- the model alone, ``model.pipeline`` a frame at a time: the Python a system engineer
  evaluates an algorithm with, and the exact arithmetic the pysim's tasks call.  No simulation.
- **pysim** -- Waveflow's loosely-timed simulation, both granularities: ``"tasks"`` (one process per
  ``hls::task``, every internal edge timed) and ``"flat"`` (one process for the FFT).
- **rtl** -- the synthesized RTL under XSI through the system flow (``rtl.run_xsi``), the runner's
  ``simulate`` phase separated from its one-time compile and elaborate.

Each is run at several frame counts and fitted to ``setup + frames * per_frame``, so the per-frame
cost is not muddied by building a graph or a simulator.  Every run checks every frame's bits.
Times are the best of ``--reps`` runs: other load on the machine only ever adds time.
Results go to ``runtime.json``, one entry per configuration (``L<L>_<reorder>``).
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import tempfile
import time
from datetime import date
from pathlib import Path

import numpy as np

from waveflow import events
from waveflow.dsp.ssr_fft import model as m
from waveflow.dsp.ssr_fft import rtl
from waveflow.dsp.ssr_fft.testbench import golden, pysim_output, run_pysim
from waveflow.vitis_l1.testbench import input_frames

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNTIME = HERE / "runtime.json"
FRAMES = (10, 50, 100)
PHASES = ("compile_rtl", "elaborate", "compile_tb", "simulate")


def _fit(frames: list[int], secs: list[float]) -> dict:
    """``secs ~ setup + frames * per_frame``, least squares."""
    per, setup = np.polyfit(np.asarray(frames, float), np.asarray(secs, float), 1)
    return {"per_frame_ms": 1e3 * per, "setup_ms": 1e3 * setup,
            "runs": {str(n): s for n, s in zip(frames, secs)}}


def _exact(got, want) -> bool:
    return len(got) == len(want) and all(
        np.array_equal(g[0], w[0]) and np.array_equal(g[1], w[1]) for g, w in zip(got, want))


def time_untimed(length: int, n: int, reps: int) -> tuple[float, bool]:
    """The model alone: ``model.pipeline`` over *n* frames, natural order in and out."""
    geo = m.Geometry(length)
    frames = [(m.to_words(np.asarray(re, dtype=np.int64)), m.to_words(np.asarray(im, dtype=np.int64)))
              for re, im in input_frames(n, length)]
    best, out = None, None
    for _ in range(reps):
        t = time.perf_counter()
        out = [m.pipeline(geo, re, im) for re, im in frames]
        dt = time.perf_counter() - t
        best = dt if best is None else min(best, dt)
    got = [(re.reshape(-1), im.reshape(-1)) for re, im in out]
    return best, _exact(got, golden(n, length))


def time_pysim(length: int, n: int, reorder: str, mode: str, reps: int) -> tuple[float, bool]:
    """One whole pysim run: build the testbench graph, write the scenario, ``run_sim``."""
    best, ok = None, True
    for _ in range(reps):
        with tempfile.TemporaryDirectory() as root:
            t = time.perf_counter()
            tb = run_pysim(Path(root), length=length, n_frames=n, reorder=reorder, pysim=mode)
            dt = time.perf_counter() - t
        ok = ok and _exact(pysim_output(tb), golden(n, length))
        best = dt if best is None else min(best, dt)
    return best, ok


def time_rtl(root: Path, length: int, n: int, reorder: str) -> dict:
    """One XSI run of the scenario: the runner's phases, and the bits."""
    rtl.generate(root, length, n_frames=n, reorder=reorder)
    with events.collect() as spans:
        t = time.perf_counter()
        rtl.run_xsi(root)
        total = time.perf_counter() - t
    phases = {p: sum(s["elapsed"] for s in spans if s["kind"] == "phase" and s["name"] == p)
              for p in PHASES}
    bits = rtl.check_bits(root, length, n)
    return {"total_s": total, **{f"{p}_s": v for p, v in phases.items()},
            "bits_exact": len(bits) == n and all(bits)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--length", type=int, default=1024)
    ap.add_argument("--frames", type=int, nargs="+", default=list(FRAMES))
    ap.add_argument("--reorder", default="pingpong", choices=("pingpong", "sob"))
    ap.add_argument("--reps", type=int, default=3, help="best of this many runs per point")
    ap.add_argument("--rtl", action="store_true", help="also run the RTL under XSI (needs Vivado)")
    ap.add_argument("--synth", action="store_true", help="with --rtl: csynth from scratch, timed")
    ap.add_argument("--root", type=Path, default=None,
                    help="the RTL build directory (default work/ssr_fft/rt_L<L>_<reorder>)")
    a = ap.parse_args()
    frames = sorted(a.frames)
    modes = ("tasks", "flat") if a.reorder == "pingpong" else ("tasks",)

    out = {"L": a.length, "reorder": a.reorder, "date": date.today().isoformat(),
           "machine": {"cpu": platform.processor(), "python": platform.python_version(),
                       "system": platform.platform()},
           "frames": frames, "reps": a.reps}

    secs, ok = zip(*(time_untimed(a.length, n, a.reps) for n in frames))
    out["untimed"] = {**_fit(frames, list(secs)), "bits_exact": all(ok)}
    print(f"untimed      {out['untimed']['per_frame_ms']:8.2f} ms/frame  exact={all(ok)}", flush=True)
    for mode in modes:
        secs, ok = zip(*(time_pysim(a.length, n, a.reorder, mode, a.reps) for n in frames))
        key = f"pysim_{mode}"
        out[key] = {**_fit(frames, list(secs)), "bits_exact": all(ok)}
        print(f"pysim {mode:6s} {out[key]['per_frame_ms']:8.2f} ms/frame  "
              f"setup {out[key]['setup_ms']:7.1f} ms  exact={all(ok)}", flush=True)

    if a.rtl:
        # A short path: csynth fails silently past the Windows path limit.
        root = a.root or REPO / "work" / "ssr_fft" / f"rt_L{a.length}_{a.reorder}"
        root.mkdir(parents=True, exist_ok=True)
        synth_s = None
        rtl.generate(root, a.length, n_frames=frames[0], reorder=a.reorder)
        if a.synth or not (root / f"{rtl.TOP}_proj").is_dir():
            shutil.rmtree(root / f"{rtl.TOP}_proj", ignore_errors=True)
            t = time.perf_counter()
            rtl.synth(root)
            synth_s = time.perf_counter() - t
        # A cold first run: without the previous snapshot, it compiles and elaborates, which is what
        # every design change costs.  The runs after it reuse that build and only simulate.
        shutil.rmtree(root / rtl.SYSTEM_DIR, ignore_errors=True)
        first = time_rtl(root, a.length, frames[0], a.reorder)
        runs = [time_rtl(root, a.length, n, a.reorder) for n in frames]
        fit = _fit(frames, [r["simulate_s"] for r in runs])
        out["rtl"] = {**fit, "bits_exact": all(r["bits_exact"] for r in runs),
                      "csynth_s": synth_s,
                      "build_s": {p: first[f"{p}_s"] for p in PHASES if p != "simulate"},
                      "phases": {str(n): r for n, r in zip(frames, runs)}}
        print(f"rtl (xsi)    {fit['per_frame_ms']:8.2f} ms/frame  exact={out['rtl']['bits_exact']}  "
              f"build {sum(out['rtl']['build_s'].values()):.1f} s"
              + (f"  csynth {synth_s:.0f} s" if synth_s else ""), flush=True)

    data = json.loads(RUNTIME.read_text(encoding="utf-8")) if RUNTIME.exists() else {}
    data[f"L{a.length}_{a.reorder}"] = out
    RUNTIME.write_text(json.dumps(dict(sorted(data.items())), indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
