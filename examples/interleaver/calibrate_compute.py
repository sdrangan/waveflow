"""calibrate_compute.py — fit the interleaver's CUSTOM gather compute timing (the direct method).

The interleaver reuses calibrated *infra* (the ``m_axi`` bus + the mem-stream adaptors) whose models
ship with the platform — but its ``il_compute`` gather is the design's **own** kernel, so its timing is
not shipped: you fit it. This is the half of the calibration story mem_copy has none of.

The typed-SOB gather (``y[i] = x[p[i]]``) is a pipelined ELEMENT loop at II=1, so its cycle count is a
line in the element count ``n``::

    cycles = latency + ii · (n − 1)           # ii = per-element initiation interval, latency = fixed cost

which is the **direct** method (``docs/guide/calib/fit.md``): fit a :class:`LinCalibModel` straight from
an ``(n, cycles)`` sweep — no residual, because the compute has no m_axi transfer for pysim to already
charge (it is pure SOB→SOB).  The cycle counts in :data:`N_TO_CYCLES` are **measured**: the ``il_compute``
per-firing span in a full-pipeline XSI run (``measure_compute_spans.py``), taken only at firings with no
output backpressure, so the span is the gather loop's own time.

Where it lands is the point of the split: the bus law and the mem-stream residuals are *platform*
properties and ship in the platform library, but ``il_compute`` is the interleaver's **own** kernel, so
its fit lives **with the example** — ``examples/interleaver/calib/<platform>/il_compute_task/params.json``
(see :func:`~examples.interleaver.interleaver.compute_calib_dir`).  That is the standard place for an
example's custom-component timing; the shared library stays reusable infra only.
"""
from __future__ import annotations

from pathlib import Path

from examples.interleaver.interleaver import compute_calib_dir

#: Measured ``il_compute`` per-firing cycle counts per element count ``n`` — the no-stall XSI fire-spans
#: from ``measure_compute_spans.py`` (full-pipeline trace, ``y_blk`` write-burst gated on a single
#: contiguous window).  All three sizes came back with span == ``n`` exactly and no backpressure, so the
#: gather is a clean II=1 element loop: ``cycles = n`` (ii=1, latency=1).  Two+ distinct sizes fit the line.
N_TO_CYCLES: "dict[int, float]" = {
    128: 128.0,
    256: 256.0,
    512: 512.0,
}


def fit_compute_model(n_to_cycles: "dict[int, float]", out_dir: str | Path) -> Path:
    """Fit ``cycles = latency + ii·(n − 1)`` from the ``{n: cycles}`` sweep and write
    ``out_dir/params.json`` (the artifact :class:`IlComputeInband` loads). Returns the path."""
    import pandas as pd

    from waveflow.calib.calib import LinCalibModel

    if len(n_to_cycles) < 2:
        raise ValueError("need >= 2 distinct sizes to fit a line (latency + ii)")
    df = pd.DataFrame([{"n": n, "cycles": cyc} for n, cyc in sorted(n_to_cycles.items())])
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model = LinCalibModel(basis=["n"], target="cycles", fit_intercept=True, coeff_names=["n"],
                          path=out / "params.json")
    model.fit(df)
    return model.save_model()


def calibrate(platform_name: str = "zynq7020_bfm_100mhz") -> Path:
    """Fit the compute model into THIS example's calib dir for *platform_name*
    (``examples/interleaver/calib/<platform>/il_compute_task/``) — ``il_compute`` is the interleaver's own
    kernel, not shared platform infra, so its fit lives with the example, not the platform library."""
    return fit_compute_model(N_TO_CYCLES, compute_calib_dir(platform_name))


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platform", default="zynq7020_bfm_100mhz",
                    help="platform name (keys the example's calib subdir)")
    args = ap.parse_args(argv)
    path = calibrate(args.platform)
    print(f"fit il_compute loop model -> {path}")
    print("  (from measured no-stall XSI fire-spans: cycles = n, ii=1, latency=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
