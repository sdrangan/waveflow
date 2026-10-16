"""finding.py — what the exploration says about the design: guard bits, and where speed comes from.

Step 6.8 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  Everything here is read off the DSE
(:mod:`examples.mimo_cg.hw.dse`): measured accuracy, predicted csynth cost, the whole design space.
The same comparison in implemented hardware, for twelve of these designs, is
:mod:`examples.mimo_cg.hw.finalists`.

The question
------------
Phase 3 found that a few guard bits on the two scalar accumulators (rᴴr and pᴴAp) let every
vector and matrix register be 2–6 bits narrower at the same accuracy.  What is that worth in
hardware?  For every scenario, at 0.5 dB of SNR loss and at each of the eight committed job-time
budgets, take the cheapest design in LUTs **with any guard** and the cheapest **with no guard
bits**, and compare them (:func:`guard_pairs`).  Both are picks over the whole space, so each side
is free to spend its width on fewer iterations, more lanes or a larger array.

The second table (:func:`shape_rows`) is how the cheapest design changes along the job-time
budget: which knob the exploration turns to go faster.

::

    python -m examples.mimo_cg.hw.finding      # paper_data/dse_guard_pairs.csv, dse_guard.csv,
                                               # dse_shape.csv and the frontier figure
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

import pandas as pd

from examples.mimo_cg.hw import dse
from examples.mimo_cg.hw import models as MD
from examples.mimo_cg.hw.estimate import CLOCK_NS
from examples.mimo_cg.hw.fidelity import DECISIONS, IMAGES
from examples.mimo_cg.hw.space import HwConfig
from examples.mimo_cg.mimo_cg import provenance, read_table, write_table

PAPER_DATA = MD.PAPER_DATA
LOSS_DB = 0.5
COUNTERS = dse.RESOURCES
#: The scenarios of the frontier figure: the Phase 1 default modulation and antenna count.
FIGURE_SCENARIOS = (("16qam", 64, 4), ("16qam", 64, 8), ("16qam", 64, 16))


def job_budgets(scenario: tuple) -> list[int]:
    """The eight committed job-time budgets of ``scenario`` at :data:`LOSS_DB`, ascending."""
    return sorted(
        {
            int(r["job_budget"])
            for r in read_table(DECISIONS)
            if (r["modulation"], int(r["M"]), int(r["K"])) == scenario
            and float(r["budget_db"]) == LOSS_DB
        }
    )


def _context() -> tuple[pd.DataFrame, pd.DataFrame]:
    hw = dse.hw_table()
    return hw[hw.cmd_depth == dse.FRONTIER_CMD_DEPTH], dse.accuracy()


def _cheapest(cands: pd.DataFrame, tau: float, no_guard: bool):
    ok = ~cands.guarded.to_numpy()
    if no_guard:
        ok &= (cands.g_s == 0).to_numpy()
    i = dse.pick(cands.lut.to_numpy(), cands.job.to_numpy(), tau, eligible=ok)
    return None if i is None else cands.iloc[i]


def guard_pairs(hw: pd.DataFrame, acc: pd.DataFrame) -> list[dict]:
    """One row per scenario and job-time budget: the cheapest design in LUTs with any guard, the
    cheapest with ``g_s = 0`` (blank when no such design meets the two budgets), and what leaving
    the guard out costs, in percent of the design with it."""
    rows = []
    for scn in dse.scenarios(acc):
        cands = dse.candidates(hw, acc, scn, LOSS_DB)
        for i, tau in enumerate(job_budgets(scn)):
            row = {"modulation": scn[0], "M": scn[1], "K": scn[2]}
            row |= {"budget": i, "job_budget": tau}
            pair = {
                "with": _cheapest(cands, tau, False),
                "without": _cheapest(cands, tau, True),
            }
            for side, r in pair.items():
                for k in (
                    "W",
                    "g_s",
                    "nit",
                    "L",
                    "R",
                    "C",
                    "cmul",
                    "mem_dw",
                    *COUNTERS,
                ):
                    row[f"{side}_{k}"] = "" if r is None else int(r[k])
                row[f"{side}_job"] = "" if r is None else round(float(r["job"]), 1)
            if pair["without"] is not None:
                a, b = pair["with"], pair["without"]
                for k in COUNTERS:
                    row[f"{k}_pct"] = round(100.0 * (b[k] - a[k]) / a[k], 2)
            else:
                row |= {f"{k}_pct": "" for k in COUNTERS}
            rows.append(row)
    return rows


def guard_summary(pairs: list[dict]) -> list[dict]:
    """The guard comparison in a few lines, over all pairs and by modulation and K."""
    out = []

    def add(group: str, rows: list[dict]) -> None:
        both = [r for r in rows if r["without_W"] != ""]
        row = {
            "group": group,
            "questions": len(rows),
            "no_design_without_guard": len(rows) - len(both),
        }
        for k in COUNTERS:
            v = sorted(float(r[f"{k}_pct"]) for r in both)
            row |= {
                f"{k}_median_pct": round(statistics.median(v), 2) if v else "",
                f"{k}_max_pct": round(v[-1], 2) if v else "",
            }
        wider = [int(r["without_W"]) - int(r["with_W"]) for r in both]
        more = [int(r["without_nit"]) - int(r["with_nit"]) for r in both]
        row |= {
            "wider_bits_median": statistics.median(wider) if wider else "",
            "wider_by_4_or_more": sum(w >= 4 for w in wider),
            "more_iterations": sum(m > 0 for m in more),
        }
        out.append(row)

    add("all", pairs)
    for mod in ("qpsk", "16qam", "64qam"):
        add(mod, [r for r in pairs if r["modulation"] == mod])
    for K in (4, 8, 16):
        add(f"K = {K}", [r for r in pairs if r["K"] == K])
    return out


def shape_rows(pairs: list[dict], models: MD.Models | None = None) -> list[dict]:
    """Per job-time budget (0 the tightest, 7 the loosest), over the 27 scenarios: what the
    cheapest design looks like — its lanes, its array, and how the iteration splits between the
    vector unit and the matmul."""
    models = models or MD.calibrated()
    out = []
    for i in sorted({r["budget"] for r in pairs}):
        mine = [r for r in pairs if r["budget"] == i]
        share, lut, dsp, job = [], [], [], []
        for r in mine:
            knobs = {
                k: int(r[f"with_{k}"])
                for k in ("W", "g_s", "L", "R", "C", "cmul", "mem_dw")
            }
            c = HwConfig(K=int(r["K"]), sob_depth=2, cmd_depth=2, **knobs)
            s = models.spans(c)
            share.append(s["vec.iter"] / (s["vec.iter"] + s["mm.iter"]))
            lut.append(int(r["with_lut"]))
            dsp.append(int(r["with_dsp"]))
            job.append(float(r["with_job"]) * CLOCK_NS * 1e-3)
        lanes = sorted(int(r["with_L"]) for r in mine)
        pes = sorted(int(r["with_R"]) * int(r["with_C"]) for r in mine)
        out.append(
            {
                "budget": i,
                "scenarios": len(mine),
                "lanes_min": lanes[0],
                "lanes_median": statistics.median(lanes),
                "lanes_max": lanes[-1],
                "array_pes_min": pes[0],
                "array_pes_median": statistics.median(pes),
                "array_pes_max": pes[-1],
                "three_multiply_form": sum(int(r["with_cmul"]) == 3 for r in mine),
                "words_32_bit": sum(int(r["with_mem_dw"]) == 32 for r in mine),
                "vec_share_of_iteration_median": round(statistics.median(share), 3),
                "lut_median": statistics.median(lut),
                "dsp_median": statistics.median(dsp),
                "job_us_median": round(statistics.median(job), 2),
            }
        )
    return out


def write(out_dir: Path = PAPER_DATA) -> dict:
    from examples.mimo_cg.hw.validate import hls_tool, model_sha256

    hw, acc = _context()
    pairs = guard_pairs(hw, acc)
    note = provenance(
        "dse_guard",
        tool=hls_tool(),
        model_sha256=model_sha256()[:16],
        loss_db=LOSS_DB,
        cost="csynth, predicted",
    )
    out = {}
    for name, rows in (
        ("dse_guard_pairs", pairs),
        ("dse_guard", guard_summary(pairs)),
        ("dse_shape", shape_rows(pairs)),
    ):
        out[name] = Path(out_dir) / f"{name}.csv"
        write_table(out[name], rows, note)
    return out


# --- the frontier figure ---------------------------------------------------------------------


def frontier_points(hw: pd.DataFrame, acc: pd.DataFrame, scenario: tuple) -> dict:
    """The (job time in µs, LUTs) frontier of ``scenario`` at :data:`LOSS_DB`, with any guard and
    with none: ``{"with": [(x, y), …], "without": […]}``, each sorted by job time."""
    cands = dse.candidates(hw, acc, scenario, LOSS_DB)
    cands = cands[~cands.guarded]
    out = {}
    for side, pool in (("with", cands), ("without", cands[cands.g_s == 0])):
        front = pool[dse.pareto(pool[["lut", "job"]].to_numpy())].sort_values("job")
        out[side] = [
            (float(j) * CLOCK_NS * 1e-3, int(lut))
            for j, lut in zip(front.job, front.lut, strict=True)
        ]
    return out


def render_frontier(path: Path, hw: pd.DataFrame, acc: pd.DataFrame) -> Path:
    """LUTs against job time for the three default scenarios, with and without guard bits."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

    from examples.mimo_cg.hw.validate_figure import _compact
    from examples.mimo_cg.mimo_cg_figures import GRID, INK, STYLE, _save

    # the reference palette's first two categorical slots, in order: blue, orange
    series = (
        ("with", "#2a78d6", "o", "guard bits on the two scalars"),
        ("without", "#eb6834", "s", "no guard bits"),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 3, figsize=(7.0, 2.9), sharey=True)
        for ax, scn in zip(axes, FIGURE_SCENARIOS, strict=True):
            pts = frontier_points(hw, acc, scn)
            for side, color, marker, label in series:
                x, y = zip(*pts[side], strict=True)
                ax.step(x, y, where="post", color=color, linewidth=1.5, zorder=3)
                ax.plot(
                    x,
                    y,
                    linestyle="none",
                    marker=marker,
                    markersize=3.5,
                    color=color,
                    markeredgecolor="#fcfcfb",
                    markeredgewidth=0.5,
                    zorder=4,
                    label=label,
                )
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_title(f"K = {scn[2]}", color=INK)
            ax.set_xlabel("job time (µs)")
            ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
            ax.xaxis.set_major_formatter(FuncFormatter(_compact))
            ax.xaxis.set_minor_formatter(NullFormatter())
            ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
            ax.yaxis.set_major_formatter(FuncFormatter(_compact))
            ax.yaxis.set_minor_formatter(NullFormatter())
            ax.tick_params(length=0)
            ax.grid(True, which="major", color=GRID, linewidth=0.5)
            ax.grid(False, which="minor")
        axes[0].set_ylabel("LUTs (csynth, predicted)")
        axes[0].legend(loc="upper right", fontsize=6.5, handlelength=1.2)
        fig.suptitle(
            "The cheapest hardware at each job time, 16-QAM with 64 antennas, within 0.5 dB "
            "of exact MMSE\n"
            "each point is a design of the 107,460-configuration space, priced by the "
            "calibrated models",
            fontsize=7.5,
            color=INK,
        )
        fig.tight_layout()
        return _save(fig, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-figure", action="store_true")
    args = ap.parse_args(argv)
    out = write()
    for name, path in out.items():
        print(f"{name} -> {path}")
    for r in read_table(out["dse_guard"]):
        print(
            f"  {r['group']:7s} {int(r['questions']):3d} questions, "
            f"{int(r['no_design_without_guard']):2d} with no design without guard; "
            f"no guard costs LUT {r['lut_median_pct']}% FF {r['ff_median_pct']}% "
            f"BRAM {r['bram_median_pct']}% DSP {r['dsp_median_pct']}% (medians)"
        )
    if not args.no_figure:
        hw, acc = _context()
        print("wrote", render_frontier(IMAGES / "dse_frontier.svg", hw, acc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
