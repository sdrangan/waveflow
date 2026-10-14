"""ltbf_accuracy_figures.py -- the accuracy DSE's figures, from ``paper_data/accuracy_losses.csv``.

``plans/mimo_cg/ltbf_cg_dse.md`` step 3.3.  Deterministic (same CSV, same bytes):

* ``loss_vs_t_<band>_<srs>.png`` -- per target (rows) and array (columns): the median per-UE loss
  (exact LTBF minus the design, dB, signed) against ``t`` for every word width at guard 1 and the
  float CG, regime 1, factored ``Q``; the 90th percentile dashed;
* ``form_<band>_<srs>.png`` -- explicit minus factored median loss at each width, regime 1;
* ``regime_<band>_<srs>.png`` -- regime 2 minus regime 1 median loss, float and 16 bits.

    python -m examples.ltbf_cg.ltbf_accuracy_figures [--csv paper_data/accuracy_losses.csv]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd  # type: ignore[import-untyped]

HERE = Path(__file__).resolve().parent
PAPER = HERE / "paper_data"
WIDTHS = (10, 12, 14, 16, 18, 20, 24)
SIDES = (8, 16, 32)
TARGETS = ("-6_3", "-6_14")
#: One sequential hue, light to dark, for the word width (a magnitude); the float reference in ink.
RAMP = ("#8fbbdb", "#6aa5d0", "#4a8fc4", "#2f78b5", "#1a5f9f", "#0d4583", "#062c5e")
INK = "#1f1f1f"
PNG_META = {"Software": None}


def _legend(fig, axes, y: float = 1.0) -> None:
    """One legend for the figure, from every panel (a panel may be empty), its top at ``y``."""
    seen: dict = {}
    for ax in np.ravel(axes):
        for h, lab in zip(*ax.get_legend_handles_labels(), strict=True):
            seen.setdefault(lab, h)
    if seen:
        fig.legend(
            list(seen.values()),
            list(seen),
            loc="upper center",
            bbox_to_anchor=(0.5, y),
            ncol=8,
            fontsize=8,
            frameon=False,
        )


def _sel(df, **kw):
    m = np.ones(len(df), dtype=bool)
    for k, v in kw.items():
        m &= (df[k] == v).to_numpy()
    return df[m]


def load(csv: Path) -> pd.DataFrame:
    df = pd.read_csv(csv)
    df = df[df.kind.isin(["float", "fixed"]) & (df.r == 1) & (df.nue == 10)].copy()
    df["regime"] = df.regime.astype(int)
    df["t"] = df.t.astype(int)
    return df


def loss_vs_t(df: pd.DataFrame, band: str, srs: str, out: Path) -> Path:
    fig, axes = plt.subplots(2, 3, figsize=(12, 6.4), sharex=True)
    for i, target in enumerate(TARGETS):
        for j, side in enumerate(SIDES):
            ax = axes[i, j]
            g = _sel(df, band=band, srs=srs, target=target, n=side, regime=1)
            fl = _sel(g, kind="float").sort_values("t")
            if not fl.empty:
                ax.plot(fl.t, fl.median_loss_db, "-", color=INK, lw=2, label="float CG")
                ax.plot(fl.t, fl.p90_loss_db, "--", color=INK, lw=1)
            for w, col in zip(WIDTHS, RAMP, strict=True):
                fx = _sel(
                    g, kind="fixed", format=f"W{w}g1", form="factored"
                ).sort_values("t")
                if fx.empty:
                    continue
                ax.plot(
                    fx.t, fx.median_loss_db, "-", color=col, lw=1.4, label=f"W = {w}"
                )
                ax.plot(fx.t, fx.p90_loss_db, "--", color=col, lw=0.8)
            ax.axhline(0, color="0.6", lw=0.8)
            ax.set_title(
                f"{side}x{side}, target [{target.replace('_', ', ')}] dB", fontsize=9
            )
            ax.set_yscale("symlog", linthresh=0.1)
            ax.grid(True, color="0.9", lw=0.6)
            if i == 1:
                ax.set_xlabel("CG iterations t")
            if j == 0:
                ax.set_ylabel("loss vs exact LTBF (dB)")
    _legend(fig, axes, y=0.955)
    fig.suptitle(f"{band}, SRS {srs}: median (solid) and 90th percentile (dashed), regime 1, factored",
                 y=0.995, fontsize=10)  # fmt: skip
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    p = out / f"loss_vs_t_{band}_{srs}.png"
    fig.savefig(p, dpi=130, metadata=PNG_META)
    plt.close(fig)
    return p


def diff_figure(df, band, srs, out, kind: str) -> Path:
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.4), sharey=True)
    for j, side in enumerate(SIDES):
        ax = axes[j]
        g = _sel(df, band=band, srs=srs, target="-6_3", n=side)
        if kind == "form":
            series = [(w, col) for w, col in zip(WIDTHS, RAMP, strict=True)]
            for w, col in series:
                a = _sel(
                    g, kind="fixed", format=f"W{w}g1", form="explicit", regime=1
                ).set_index("t")
                b = _sel(
                    g, kind="fixed", format=f"W{w}g1", form="factored", regime=1
                ).set_index("t")
                d = (a.median_loss_db - b.median_loss_db).dropna()
                if len(d):
                    ax.plot(d.index, d.values, "-", color=col, lw=1.4, label=f"W = {w}")
            ax.set_title(f"{side}x{side}: explicit - factored", fontsize=9)
        else:
            for lab, sel, col in (("float", {"kind": "float"}, INK),
                                  ("W = 16", {"kind": "fixed", "format": "W16g1", "form": "factored"}, RAMP[3])):  # fmt: skip
                a = _sel(g, regime=2, **sel).set_index("t")
                b = _sel(g, regime=1, **sel).set_index("t")
                d = (a.median_loss_db - b.median_loss_db).dropna()
                if len(d):
                    ax.plot(d.index, d.values, "-", color=col, lw=1.4, label=lab)
            ax.set_title(f"{side}x{side}: regime 2 - regime 1", fontsize=9)
        ax.axhline(0, color="0.6", lw=0.8)
        ax.set_yscale("symlog", linthresh=0.01)
        ax.grid(True, color="0.9", lw=0.6)
        ax.set_xlabel("CG iterations t")
    axes[0].set_ylabel("median loss difference (dB)")
    _legend(fig, axes)
    fig.tight_layout(rect=(0, 0, 1, 0.88))
    p = out / f"{kind}_{band}_{srs}.png"
    fig.savefig(p, dpi=130, metadata=PNG_META)
    plt.close(fig)
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", default=str(PAPER / "accuracy_losses.csv"))
    ap.add_argument("--out", default=str(PAPER / "figures"))
    a = ap.parse_args(argv)
    df = load(Path(a.csv))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for band in sorted(df.band.unique()):
        for srs in sorted(df.srs.unique()):
            for p in (loss_vs_t(df, band, srs, out), diff_figure(df, band, srs, out, "form"),
                      diff_figure(df, band, srs, out, "regime")):  # fmt: skip
                print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
