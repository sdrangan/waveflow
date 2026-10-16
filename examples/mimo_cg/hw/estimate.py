"""estimate.py — price a hardware configuration from the calibrated models, with no toolchain.

Step 5.6 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  The full-design estimate is the composition of
:mod:`examples.mimo_cg.hw.models`:

* **resources** — the sum of the detector's module rows and its integration term (channels, FIFOs,
  adapters), which is what :func:`waveflow.calib.resource_model.compose` walks;
* **cycles** — a job of ``nit`` iterations takes ``T0 + nit·T_iter`` between completions, with
  ``T_iter`` the matmul's span plus the vector unit's span and ``T0`` the vector unit's start span
  plus the job overhead of the memory word width.

::

    python -m examples.mimo_cg.hw.estimate --K 8 --L 8 --R 4 --C 8 --W 10 --g-s 8 --nit 3
    python -m examples.mimo_cg.hw.estimate --check fit        # errors on the fit detectors
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from examples.mimo_cg.hw import models as MD
from examples.mimo_cg.hw.space import HwConfig, is_valid
from examples.mimo_cg.mimo_cg import read_table

#: The clock the cycle counts are converted to time at (the 4 ns synthesis target).
CLOCK_NS = 4.0


@dataclass(frozen=True)
class Estimate:
    """One configuration, priced: csynth resources and job timing."""

    config: HwConfig
    lut: int
    ff: int
    dsp: int
    bram: int
    t0: float
    t_iter: float
    modules: dict

    def job_cycles(self, nit: int) -> float:
        """Cycles between the completions of consecutive ``nit``-iteration jobs."""
        return self.t0 + nit * self.t_iter

    def job_us(self, nit: int) -> float:
        return self.job_cycles(nit) * CLOCK_NS * 1e-3


def estimate(c: HwConfig, models: MD.Models | None = None) -> Estimate:
    """Price ``c``; raises if it is outside the design space the models were calibrated on."""
    if not is_valid(c):
        raise ValueError(f"{c} is not a configuration of the design space")
    models = models or MD.calibrated()
    if models is None:
        raise FileNotFoundError(
            f"no model file at {MD.MODEL_FILE}; run models --fit first"
        )
    res, cyc = models.resources(c), models.cycles(c)
    return Estimate(
        config=c,
        **res["total"],
        t0=cyc["t0"],
        t_iter=cyc["t_iter"],
        modules=res["modules"],
    )


def measured_detectors(role: str, data_dir: Path = MD.PAPER_DATA) -> list[dict]:
    """The measured detectors of one role: configuration, totals and job timing."""
    cyc = {
        (r["build"], r["quantity"]): float(r["cycles"])
        for r in read_table(data_dir / "hw_cycles.csv")
        if r["quantity"] in ("t0", "t_iter")
    }
    out = []
    for r in read_table(data_dir / "hw_builds.csv"):
        if r["top"] == "det" and r["role"] == role:
            c = HwConfig(**{k: int(r[k]) for k in HwConfig.__dataclass_fields__})
            row = {"build": r["build"], "config": c}
            row |= {ctr: int(r[ctr]) for ctr in MD.COUNTERS}
            row |= {
                "t0": cyc[(r["build"], "t0")],
                "t_iter": cyc[(r["build"], "t_iter")],
            }
            out.append(row)
    return out


def errors(
    role: str, models: MD.Models | None = None, data_dir: Path = MD.PAPER_DATA
) -> list[dict]:
    """Estimate against measurement for every detector of ``role``: one row per build."""
    rows = []
    for meas in measured_detectors(role, data_dir):
        est = estimate(meas["config"], models)
        row = {"build": meas["build"]}
        for q in (*MD.COUNTERS, "t0", "t_iter"):
            got, want = getattr(est, q), meas[q]
            row |= {
                f"{q}_meas": want,
                f"{q}_est": got,
                f"{q}_err_pct": 100 * (got - want) / want,
            }
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name, default in HwConfig().__dict__.items():
        ap.add_argument(
            f"--{name.replace('_', '-')}", dest=name, type=int, default=default
        )
    ap.add_argument(
        "--nit", type=int, default=None, help="also print the job time at this nit"
    )
    ap.add_argument(
        "--check",
        choices=("fit", "holdout"),
        help="estimate against the measured detectors",
    )
    args = ap.parse_args(argv)
    if args.check:
        rows = errors(args.check)
        for q in ("lut", "ff", "t_iter", "t0"):
            errs = [abs(r[f"{q}_err_pct"]) for r in rows]
            print(
                f"{q:7s} mean |error| {sum(errs) / len(errs):5.2f}%   worst {max(errs):5.2f}%   ({len(rows)} detectors)"
            )
        for q in ("dsp", "bram"):
            exact = sum(r[f"{q}_meas"] == r[f"{q}_est"] for r in rows)
            print(f"{q:7s} exact on {exact} of {len(rows)}")
        return 0
    c = HwConfig(**{k: getattr(args, k) for k in HwConfig.__dataclass_fields__})
    est = estimate(c)
    print(c)
    for name, row in est.modules.items():
        print(
            f"  {name:12s} LUT {row['lut']:7d}  FF {row['ff']:7d}  DSP {row['dsp']:5d}  BRAM {row['bram']:4d}"
        )
    print(
        f"  {'total':12s} LUT {est.lut:7d}  FF {est.ff:7d}  DSP {est.dsp:5d}  BRAM {est.bram:4d}"
    )
    print(f"  cycles per job: {est.t0:.0f} + {est.t_iter:.0f} per iteration")
    if args.nit:
        print(
            f"  nit = {args.nit}: {est.job_cycles(args.nit):.0f} cycles, {est.job_us(args.nit):.1f} us at {CLOCK_NS:g} ns"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
