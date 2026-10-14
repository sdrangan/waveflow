"""poly_calibrate.py — fit poly's declared timed phases against RTL (``plans/pipelined_ops.md`` Stage 5).

    python -m examples.stream_inband.poly_calibrate            # both widths (Vitis: csynth + a traced cosim)
    python -m examples.stream_inband.poly_calibrate --width 32

Everything but the scenario is generated from the module's declarations (``PolyAccel.hdr_phase``,
``PolyAccel.samp_phase``; :mod:`waveflow.calib.phases`):

1. **pysim** runs the ``calib`` scenario (commands of :data:`~scenarios.CALIB_NSAMP` samples) and
   records every phase occurrence: which transfers each span starts and ends on, and its features;
2. **cosim** runs the same scenario with the kernel's ports traced (``run.tcl``'s ``vcd`` stage in
   its own project, ``cal_w<W>``) and its response is checked like every cosim;
3. the trace is decoded into each timed port's transfers (:func:`~waveflow.calib.phases.rtl_xfers`)
   and joined with pysim's records (:func:`~waveflow.calib.phases.phase_corpus`): pysim gives the
   pairing and the features, the RTL the times;
4. each span is fitted on the clean rows, the interior lengths :data:`~scenarios.CALIB_HOLDOUT`
   held out, and the parameters filed in ``calib/w<W>/<phase>/<span>.json`` -- where the phases load
   them from.  The report, residuals included, goes to ``results/phase_calib_w<W>.json``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIDTHS = (32, 64)
TOPS = {32: "poly", 64: "poly_bw64"}


def calib_dir(word_bw: int) -> Path:
    """Where poly's fitted phase parameters live, per configuration (the width)."""
    return HERE / "calib" / f"w{word_bw}"


def pysim_records(word_bw: int, data_dir: Path):
    """Run the calib scenario in pysim; return the module (its phases hold the records)."""
    from examples.stream_inband.poly import PolyAccel, PolyTB, connect
    from waveflow.hw.clock import Clock
    from waveflow.simulation.simulation import Simulation
    from waveflow.utils.burst_io import read_bursts

    d = data_dir / f"w{word_bw}" / "calib"
    sim = Simulation()
    clk = Clock(freq=100e6)
    accel = PolyAccel(name="poly_accel", sim=sim, clk=clk, in_bw=word_bw, out_bw=word_bw)
    tb = PolyTB(name="poly_tb", sim=sim, stimulus=d / "in", word_bw=word_bw,
                n_out=len(read_bursts(d / "expected")))
    connect(sim, tb, accel, clk)
    sim.run_sim()
    return accel


def traced_cosim(word_bw: int, root: Path = HERE, live_output: bool = False) -> Path:
    """csynth + cosim of the calib scenario with the ports traced; the VCD's path."""

    from waveflow.scripts.xsim_vcd import run_xsim_vcd
    from waveflow.toolchain import toolchain

    from examples.stream_inband import scenarios as S
    from examples.stream_inband.poly_build import width_dir

    proj = f"cal_w{word_bw}"
    (width_dir(root, word_bw) / "calib" / "cosim").mkdir(parents=True, exist_ok=True)
    env = {"WAVEFLOW_POLY_STAGE": "vcd", "WAVEFLOW_POLY_WIDTH": str(word_bw),
           "WAVEFLOW_POLY_SCENARIO": "calib", "WAVEFLOW_POLY_PROJ": proj,
           "WAVEFLOW_POLY_CLK_PERIOD_NS": "10"}
    toolchain.run_vitis_hls(root / "run.tcl", work_dir=root, capture_output=not live_output,
                            env=env)
    problems = S.check(width_dir(root, word_bw), "cosim", ["calib"])["calib"]
    if problems:
        raise RuntimeError(f"cosim of calib (w{word_bw}) differs from the expected: {problems}")
    return Path(run_xsim_vcd(top=TOPS[word_bw], comp=proj, out=f"calib_w{word_bw}.vcd",
                             trace_level="port", workdir=root))


def calibrate(word_bw: int, vcd: Path | None = None, root: Path = HERE, file: bool = True) -> dict:
    """Steps 1-4 for one width; returns the report.  *vcd*: reuse a traced run (the committed ones,
    ``vcd/calib_w<W>.vcd``, reproduce the filed parameters with no toolchain).  ``file=False``: fit and
    report without filing the parameters or the report."""
    from examples.stream_inband import scenarios as S
    from examples.stream_inband.poly import samples_per_word
    from waveflow.calib.phases import fit_phase, phase_corpus, rtl_xfers

    data_dir = root / "data"
    S.write_scenarios(data_dir / f"w{word_bw}", word_bw)
    accel = pysim_records(word_bw, data_dir)
    if vcd is None:
        vcd = traced_cosim(word_bw, root)
    rtl = rtl_xfers(vcd, accel, scope=f"apatb_{TOPS[word_bw]}_top.AESL_inst_{TOPS[word_bw]}")
    pf = samples_per_word(word_bw)
    held_words = {n // pf for n in S.CALIB_HOLDOUT}
    report = {"word_bw": word_bw, "vcd": Path(vcd).as_posix(), "phases": {}}
    for phase in accel.timing_phases.values():
        phase.calib_dir = calib_dir(word_bw) / phase.name
        rows = phase_corpus(phase, rtl)
        fit = fit_phase(phase, rows, save=file,
                        holdout=(lambda f: f.get("nwords") in held_words) if phase.features else None)
        report["phases"][phase.name] = {"rows": rows, "fit": fit}
    if not file:
        return report
    out = root / "results" / f"phase_calib_w{word_bw}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=float) + "\n", encoding="utf-8")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--width", type=int, choices=WIDTHS, action="append")
    ap.add_argument("--vcd", type=Path, help="reuse a traced run's VCD (one width)")
    a = ap.parse_args()
    for w in a.width or WIDTHS:
        rep = calibrate(w, a.vcd)
        for name, ph in rep["phases"].items():
            for span, f in ph["fit"].items():
                print(f"w{w} {name}.{span}: {f['params']}")
                for feats, rtl, pred in f["holdout"]:
                    print(f"    held out {feats}: rtl {rtl:.0f}, predicted {pred:.1f}")


if __name__ == "__main__":
    main()
