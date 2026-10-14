"""validate.py -- fit the engine's cost models on the fit builds; score them on the held-out builds.

``plans/mimo_cg/ltbf_cg_dse.md`` step 4.9 (AC7).  Fits :mod:`examples.ltbf_cg.hw.models` on the
pre-registered fit builds only, freezes the parameters (``hw/models.json``), then predicts every
held-out build from its knobs alone: cycles (Vitis's per-iteration latency at the build's maximum
sizes, against the structural count with *predicted* overheads), DSP, LUT, FF, BRAM and URAM.
Writes ``paper_data/model_validation.csv`` and the scatter ``paper_data/figures/model_validation.png``.
AC7: median relative error <= 10 % for cycles and DSP, <= 15 % for LUT, FF, BRAM, URAM.

    python -m examples.ltbf_cg.hw.validate
"""

from __future__ import annotations

import csv
import sys

import numpy as np

from examples.ltbf_cg.hw import models as md

PAPER = md.eg.HERE.parent / "paper_data"
TARGETS = ("cycles", "DSP", "LUT", "FF", "BRAM_18K", "URAM")
BOUND = {
    "cycles": 0.10,
    "DSP": 0.10,
    "LUT": 0.15,
    "FF": 0.15,
    "BRAM_18K": 0.15,
    "URAM": 0.15,
}


def rel_err(pred: float, meas: float) -> float:
    if meas == 0:
        return 0.0 if pred < 0.5 else float("inf")
    return abs(pred - meas) / meas


def main() -> int:
    fit_b = md.load_reports("fit")
    held = md.load_reports("heldout")
    print(f"{len(fit_b)} fit builds, {len(held)} held-out builds")
    params = md.fit(fit_b)
    md.save(params)
    rows = []
    for k, rec in held:
        res = md.predict_resources(params, k)
        o = md.predict_overheads(params, k)
        pred = {"cycles": md.iter_cycles_at_max(k, o), **res, "URAM": 0.0}
        meas = {
            "cycles": md.measured_iter(rec),
            **{r: float(rec["resources"][r]) for r in TARGETS[1:]},
        }
        for t in TARGETS:
            rows.append({"build": k.name, "quantity": t, "measured": meas[t],
                         "predicted": round(pred[t], 2), "rel_err": round(rel_err(pred[t], meas[t]), 5)})  # fmt: skip
    PAPER.mkdir(parents=True, exist_ok=True)
    out = PAPER / "model_validation.csv"
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    ok = True
    summary = []
    for t in TARGETS:
        e = np.array([r["rel_err"] for r in rows if r["quantity"] == t])
        med, worst = float(np.median(e)), float(np.max(e))
        good = med <= BOUND[t]
        ok &= good
        summary.append((t, med, worst, good))
        print(
            f"{t:9s} median {med:7.2%}  worst {worst:7.2%}  bound {BOUND[t]:.0%}  {'ok' if good else 'MISSED'}"
        )
    _figure(rows)
    print(f"wrote {out}")
    return 0 if ok else 1


def _figure(rows: list[dict]) -> None:
    import matplotlib
    import matplotlib.ticker

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 5, figsize=(16, 3.4))
    for ax, t in zip(axes, TARGETS[:5], strict=True):
        m = np.array([r["measured"] for r in rows if r["quantity"] == t])
        p = np.array([r["predicted"] for r in rows if r["quantity"] == t])
        ax.loglog(m, p, "o", ms=4)
        lo, hi = min(m.min(), p.min()) * 0.8, max(m.max(), p.max()) * 1.25
        ax.plot([lo, hi], [lo, hi], "-", lw=1, color="0.5")
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.set_title(t)
        ax.set_xlabel("measured (csynth)")
        ax.set_ylabel("predicted")
    fig.tight_layout()
    d = PAPER / "figures"
    d.mkdir(parents=True, exist_ok=True)
    fig.savefig(d / "model_validation.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    sys.exit(main())
