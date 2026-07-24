"""calibrate_compute.py — fit the interleaver's CUSTOM gather compute timing (the direct method).

The interleaver reuses calibrated *infra* (the ``m_axi`` bus + the mem-stream adaptors) whose models
ship with the platform — but its ``il_compute`` gather is the design's **own** kernel, so its timing is
not shipped: you fit it. This is the half of the calibration story mem_copy has none of.

The gather is a pipelined word loop, so its cycle count is a line in the word count ``nw``::

    cycles = latency + ii · (nw − 1)          # ii = per-word initiation interval, latency = fixed cost

which is the **direct** method (``docs/guide/calib/fit.md``): fit a :class:`LinCalibModel` straight from
a ``(nw, cycles)`` sweep — no residual, because the compute has no transfer for pysim to already charge.
The measured cycle counts come from a **cosim sweep** of the ``il_compute`` kernel at a few sizes; wire
those in as ``NW_TO_CYCLES`` (they need the toolchain — the placeholders below are the seed law, clearly
marked, so the machinery runs end-to-end until real numbers replace them).

The fit lands in the platform library under ``components/il_compute_task/params.json``, keyed like any
component — so a build that selects the platform loads it (``InterleaverInband(compute_calib_dir=...)``)
with no re-fit.
"""
from __future__ import annotations

from pathlib import Path

from examples.interleaver.interleaver import (
    IL_COMPUTE_COMPONENT,
    IL_COMPUTE_II_SEED,
    IL_COMPUTE_LATENCY_SEED,
)

#: Measured ``il_compute`` cycle counts per word count ``nw`` — **placeholders**: these are the seed
#: law (``latency + ii·(nw−1)``), not a real cosim sweep. Replace with measured spans from an
#: ``il_compute`` csynth/cosim at each size to ship a real fit. Two+ distinct sizes fit the line.
NW_TO_CYCLES: "dict[int, float]" = {
    nw: IL_COMPUTE_LATENCY_SEED + IL_COMPUTE_II_SEED * (nw - 1)
    for nw in (64, 128, 256)
}


def fit_compute_model(nw_to_cycles: "dict[int, float]", out_dir: str | Path) -> Path:
    """Fit ``cycles = latency + ii·(nw − 1)`` from the ``{nw: cycles}`` sweep and write
    ``out_dir/params.json`` (the artifact :class:`IlCompute` loads). Returns the path."""
    import pandas as pd

    from waveflow.calib.calib import LinCalibModel

    if len(nw_to_cycles) < 2:
        raise ValueError("need >= 2 distinct sizes to fit a line (latency + ii)")
    df = pd.DataFrame([{"nw": nw, "cycles": cyc} for nw, cyc in sorted(nw_to_cycles.items())])
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model = LinCalibModel(basis=["nw"], target="cycles", fit_intercept=True, coeff_names=["nw"],
                          path=out / "params.json")
    model.fit(df)
    return model.save_model()


def calibrate(platform_dir: str | Path, name: str = "zynq7020_bfm_100mhz") -> Path:
    """Fit the compute model into the platform library's ``components/il_compute_task/``."""
    from waveflow.calib.platform import Platform

    plat = Platform.resolve(platform_dir, name)
    comp_dir = plat.component_dir(IL_COMPUTE_COMPONENT)
    return fit_compute_model(NW_TO_CYCLES, comp_dir)


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work-root", default="calib/work", help="untracked work root (default calib/work)")
    ap.add_argument("--name", default="zynq7020_bfm_100mhz", help="platform name")
    args = ap.parse_args(argv)
    path = calibrate(args.work_root, args.name)
    print(f"fit il_compute loop model -> {path}")
    print("  (NW_TO_CYCLES are placeholders — replace with a real il_compute cosim sweep to ship it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
