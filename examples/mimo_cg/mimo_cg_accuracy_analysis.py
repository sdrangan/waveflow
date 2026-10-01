"""mimo_cg_accuracy_analysis.py — Phase 3's analysis: SNR loss, the accuracy frontier, figures.

Step 3.4 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (AC3.2).  Reads the merged sweep,
``paper_data/accuracy_grid.csv`` (step 3.3), and writes:

* ``paper_data/accuracy_losses.csv`` — for every detector at every case (the 27 configurations
  in the recurrence form, plus the explicit-residual spot check): the SNR at which its BER first
  falls to 1e-3, interpolated in log-BER between the 1-dB grid points; its loss against float
  exact MMSE; and, for a fixed-point design, its **quantization-only** loss against float CG at
  the same ``nit``.  A design that never reaches 1e-3 in its ±6 dB window is ``floor``, with no
  SNR and no loss; it is never dropped.
* ``paper_data/accuracy_frontier.csv`` — for every case, the fixed-point designs (W, g_s, nit)
  within 0.5 dB of float exact MMSE that no other such design beats or ties on all three of
  W, g_s and nit.  The ``headline`` is the smallest W, then the smallest g_s, then the
  smallest nit; a case with no design in the budget gets one ``none`` row.
* the figures, rendered by :mod:`examples.mimo_cg.mimo_cg_accuracy_figures` into
  ``docs/examples/mimo_cg/images/``.

Every reference is from the same paired samples: the MMSE and float-CG curves come from the
sweep itself, not Phase 1's table.  The exact-μ unbiasing is a simulation-side genie that makes
CG slightly pessimistic at 2–3 iterations (§14, M1 review), so the frontier may be slightly
conservative there.

Run from the repo root (one command for every table and figure)::

    python -m examples.mimo_cg.mimo_cg_accuracy_analysis
    python -m examples.mimo_cg.mimo_cg_accuracy_analysis --force   # byte-identical re-run
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from examples.mimo_cg.mimo_cg import (
    TARGET_BER,
    mmse_crossing_db,
    provenance,
    read_table,
    write_table,
)
from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli

HERE = Path(__file__).resolve().parent

#: The accuracy budget of AC3.2: SNR loss at BER 1e-3 against float exact MMSE.
LOSS_BUDGET_DB = 0.5
FLOOR = "floor"
_CASE_COLUMNS = ("modulation", "M", "K", "residual")


def _opt_int(s: str) -> int | None:
    return int(s) if s != "" else None


def design_curves(rows: list[dict]) -> dict[tuple, dict[str, dict]]:
    """``{case: {detector: {"W", "g_s", "nit", "points"}}}`` from accuracy-grid rows.

    ``case`` is ``(modulation, M, K, residual)``; cases and detectors keep the table's order, and
    ``points`` are ``(rho_db, ber)`` in SNR order.
    """
    out: dict[tuple, dict[str, dict]] = {}
    for r in rows:
        case = (r["modulation"], int(r["M"]), int(r["K"]), r["residual"])
        d = out.setdefault(case, {}).setdefault(
            r["detector"],
            {
                "W": _opt_int(r["W"]),
                "g_s": _opt_int(r["g_s"]),
                "nit": _opt_int(r["nit"]),
                "points": [],
            },
        )
        d["points"].append((float(r["rho_db"]), float(r["ber"])))
    for dets in out.values():
        for d in dets.values():
            d["points"].sort()
    return out


def crossing_db(
    points: list[tuple[float, float]], target: float = TARGET_BER
) -> float | None:
    """SNR at which a BER curve first falls to ``target`` (log-BER interpolation); ``None`` if
    it never does in the window (a floor).

    Raises if the curve already starts below ``target``: the crossing would then lie below the
    window, and calling that a floor would be wrong.
    """
    if points[0][1] < target:
        raise ValueError(
            f"BER {points[0][1]:.3g} at {points[0][0]} dB is already below {target}: "
            "the SNR window starts too high"
        )
    return mmse_crossing_db([{"rho_db": s, "ber": b} for s, b in points], target)


def loss_rows(rows: list[dict]) -> list[dict]:
    """One row per (case, detector): the BER-1e-3 SNR and the two losses (see module doc)."""
    out = []
    for case, dets in design_curves(rows).items():
        ref = crossing_db(dets["mmse"]["points"])
        if ref is None:
            raise RuntimeError(
                f"float exact MMSE never reaches BER {TARGET_BER} in the window of {case}"
            )
        cg = {
            d["nit"]: crossing_db(d["points"])
            for name, d in dets.items()
            if name.startswith("cg")
        }
        for name, d in dets.items():
            snr = crossing_db(d["points"])
            ref_cg = cg.get(d["nit"]) if name.startswith("fx:") else None
            out.append(
                {
                    **dict(zip(_CASE_COLUMNS, case, strict=True)),
                    "detector": name,
                    "W": "" if d["W"] is None else d["W"],
                    "g_s": "" if d["g_s"] is None else d["g_s"],
                    "nit": "" if d["nit"] is None else d["nit"],
                    "status": "ok" if snr is not None else FLOOR,
                    "snr_db": "" if snr is None else snr,
                    "loss_mmse_db": "" if snr is None else snr - ref,
                    "loss_cg_db": (
                        "" if snr is None or ref_cg is None else snr - ref_cg
                    ),
                }
            )
    return out


def _dominates(a: tuple, b: tuple) -> bool:
    return a != b and all(x <= y for x, y in zip(a, b, strict=True))


def frontier_rows(losses: list[dict], budget_db: float = LOSS_BUDGET_DB) -> list[dict]:
    """Per case, the non-dominated fixed-point (W, g_s, nit) within ``budget_db`` of MMSE.

    ``losses`` are :func:`loss_rows` (or the table read back).  ``role`` is ``headline`` for the
    smallest W, then g_s, then nit (never dominated, so always on the frontier), ``frontier``
    for the other non-dominated designs, and ``none`` for a case with no design in the budget.
    """
    cases: dict[tuple, list[dict]] = {}
    for r in losses:
        case = tuple(r[c] for c in _CASE_COLUMNS)
        designs = cases.setdefault(case, [])
        if str(r["detector"]).startswith("fx:") and r["status"] == "ok":
            designs.append(r)
    out = []
    for case, designs in cases.items():
        feasible = {
            (int(d["W"]), int(d["g_s"]), int(d["nit"])): d
            for d in designs
            if float(d["loss_mmse_db"]) <= budget_db
        }
        front = sorted(
            c for c in feasible if not any(_dominates(e, c) for e in feasible)
        )
        base = dict(zip(_CASE_COLUMNS, case, strict=True))
        if not front:
            out.append(
                {
                    **base,
                    "W": "",
                    "g_s": "",
                    "nit": "",
                    "loss_mmse_db": "",
                    "loss_cg_db": "",
                    "role": "none",
                }
            )
            continue
        for i, cost in enumerate(front):
            d = feasible[cost]
            out.append(
                {
                    **base,
                    "W": cost[0],
                    "g_s": cost[1],
                    "nit": cost[2],
                    "loss_mmse_db": float(d["loss_mmse_db"]),
                    "loss_cg_db": (
                        float(d["loss_cg_db"]) if d["loss_cg_db"] != "" else ""
                    ),
                    "role": "headline" if i == 0 else "frontier",
                }
            )
    return out


# --- the build: one command for every table and figure ----------------------------------------

_SOURCES = {
    "accuracy_grid": "paper_data/accuracy_grid.csv",
    "analysis_source": "mimo_cg_accuracy_analysis.py",
    "accuracy_figures_source": "mimo_cg_accuracy_figures.py",
    "figures_source": "mimo_cg_figures.py",
    "mimo_cg_source": "mimo_cg.py",
}


def _paper_data(config: BuildConfig, name: str) -> Path:
    path = Path(config.root_dir) / "paper_data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(kw_only=True)
class AccuracyLossesStep(BuildStep):
    description = "SNR loss at BER 1e-3 of every detector, against MMSE and float CG."
    consumes: ClassVar[list] = ["accuracy_grid", "analysis_source", "mimo_cg_source"]
    produces: ClassVar[dict] = {
        "accuracy_losses": Path("paper_data/accuracy_losses.csv")
    }
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, accuracy_grid, **_) -> dict:
        path = _paper_data(config, "accuracy_losses.csv")
        write_table(
            path,
            loss_rows(read_table(accuracy_grid)),
            provenance(
                "accuracy_losses", target_ber=TARGET_BER, interpolation="log-BER"
            ),
        )
        return {"accuracy_losses": path}


@dataclass(kw_only=True)
class AccuracyFrontierStep(BuildStep):
    description = "Non-dominated (W, g_s, nit) within 0.5 dB of MMSE, per case."
    consumes: ClassVar[list] = ["accuracy_losses", "analysis_source"]
    produces: ClassVar[dict] = {
        "accuracy_frontier": Path("paper_data/accuracy_frontier.csv")
    }
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, accuracy_losses, **_) -> dict:
        path = _paper_data(config, "accuracy_frontier.csv")
        write_table(
            path,
            frontier_rows(read_table(accuracy_losses)),
            provenance(
                "accuracy_frontier", target_ber=TARGET_BER, budget_db=LOSS_BUDGET_DB
            ),
        )
        return {"accuracy_frontier": path}


@dataclass(kw_only=True)
class AccuracyFiguresStep(BuildStep):
    description = "Render the accuracy figures into docs/examples/mimo_cg/images/."
    consumes: ClassVar[list] = [
        "accuracy_grid",
        "accuracy_losses",
        "accuracy_frontier",
        "accuracy_figures_source",
        "figures_source",
    ]
    produces: ClassVar[dict] = {
        "accuracy_figures": Path("results/accuracy_figures.txt")
    }
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, accuracy_grid, accuracy_losses, **_) -> dict:
        from examples.mimo_cg.mimo_cg_accuracy_figures import write_accuracy_figures

        root = Path(config.root_dir)
        repo = root.parents[1]
        images = repo / "docs" / "examples" / "mimo_cg" / "images"
        written = write_accuracy_figures(accuracy_grid, accuracy_losses, images)
        manifest = root / "results" / "accuracy_figures.txt"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            "".join(
                f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(repo)}\n"
                for p in written
            ),
            encoding="utf-8",
        )
        return {"accuracy_figures": manifest}


def build_accuracy_analysis_dag() -> BuildDag:
    dag = BuildDag()
    for artifact, path in _SOURCES.items():
        dag.add(SourceStep(artifact=artifact, path=Path(path)))
    dag.add(AccuracyLossesStep(name="accuracy_losses"))
    dag.add(AccuracyFrontierStep(name="accuracy_frontier"))
    dag.add(AccuracyFiguresStep(name="accuracy_figures"))
    return dag


def main() -> None:
    run_dag_cli(
        build_accuracy_analysis_dag,
        description="Phase 3 of the CG massive-MIMO study: SNR loss, frontier, figures.",
        default_through="accuracy_figures",
        root_dir=HERE,
    )


if __name__ == "__main__":
    main()
