"""finalists.py — the designs the finding quotes, taken through Vivado implementation.

Step 6.7 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 6.0 decision record, item 1).  The DSE
and the brute-force comparison work in csynth numbers, because that is what a sweep can afford and
what the models were validated against.  The finding is stated for hardware, so the handful of
designs it quotes are built for real: csynth, the RTL run, then place and route at 4 ns.

The rule (fixed here, and ``paper_data/finalists.csv`` committed, before any of them is built)
----------------------------------------------------------------------------------------------
For the default modulation and antenna count (16-QAM, M = 64) at each K, with at most 0.5 dB of SNR
loss, the model's cheapest design in LUTs over the **whole** design space, at two job-time budgets
of the committed decision set — the loosest (the smallest hardware that works) and the second
tightest (a fast one) — and twice each: with any guard, and with no guard bits (``g_s = 0``).
Twelve designs, in six pairs that differ in one thing: whether the two scalar accumulators have
guard bits.

::

    python -m examples.mimo_cg.hw.finalists --write       # the list, before the run
    python -m examples.mimo_cg.hw.finalists --run --shard 0/3   # csynth, RTL, Vivado (long);
                                                                # one process per shard
    python -m examples.mimo_cg.hw.finalists               # (re)write paper_data/finalists_impl.csv
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from examples.mimo_cg.hw import build as B
from examples.mimo_cg.hw import dse, impl_check
from examples.mimo_cg.hw import measure as M
from examples.mimo_cg.hw import models as MD
from examples.mimo_cg.hw.fidelity import DECISIONS
from examples.mimo_cg.hw.space import HwConfig, label
from examples.mimo_cg.mimo_cg import provenance, read_table, write_table

PAPER_DATA = MD.PAPER_DATA
FINALISTS = PAPER_DATA / "finalists.csv"
#: The scenarios: the Phase 1 default modulation and antenna count, at each K.
SCENARIOS = (("16qam", 64, 4), ("16qam", 64, 8), ("16qam", 64, 16))
LOSS_DB = 0.5
#: The job-time budgets, as positions among the committed budgets of the scenario (ascending).
BUDGETS = {"loose": -1, "tight": 1}
#: The two members of a pair: any format, or only formats without guard bits.
GUARDS = {"guard": None, "no guard": 0}
COUNTERS = ("lut", "ff", "dsp", "bram")


def build_name(c: HwConfig) -> str:
    return f"fin_{label('det', c)}"


def select(models: MD.Models | None = None) -> list[dict]:
    """The finalists, by the rule of the module docstring: one row per design, with the model's
    numbers for it."""
    hw, acc = dse.hw_table(models), dse.accuracy()
    hw = hw[hw.cmd_depth == dse.FRONTIER_CMD_DEPTH]
    committed = read_table(DECISIONS)
    rows = []
    for scn in SCENARIOS:
        mod, M_, K = scn
        taus = sorted(
            {
                int(r["job_budget"])
                for r in committed
                if (r["modulation"], int(r["M"]), int(r["K"])) == scn
                and float(r["budget_db"]) == LOSS_DB
            }
        )
        cands = dse.candidates(hw, acc, scn, LOSS_DB)
        for speed, where in BUDGETS.items():
            tau = taus[where]
            for guard, g_s in GUARDS.items():
                pool = cands if g_s is None else cands[cands.g_s == g_s]
                i = dse.pick(
                    pool.lut.to_numpy(),
                    pool.job.to_numpy(),
                    tau,
                    eligible=~pool.guarded.to_numpy(),
                )
                r = pool.iloc[i]
                c = HwConfig(**{k: int(r[k]) for k in dse.KNOBS})
                rows.append(
                    {
                        "name": f"k{K}_{speed}_{guard.replace(' ', '_')}",
                        "build": build_name(c),
                        "modulation": mod,
                        "M": M_,
                        "speed": speed,
                        "guard": guard,
                        "job_budget": tau,
                        "nit": int(r["nit"]),
                        "loss_db": round(float(r["loss"]), 4),
                        **{k: getattr(c, k) for k in dse.KNOBS},
                        **{f"model_{k}": int(r[k]) for k in COUNTERS},
                        "model_job": round(float(r["job"]), 1),
                    }
                )
    return rows


def write_finalists(path: Path = FINALISTS) -> Path:
    from examples.mimo_cg.hw.validate import model_sha256

    note = provenance("finalists", model_sha256=model_sha256()[:16], loss_db=LOSS_DB)
    write_table(path, select(), note)
    return path


def read_finalists(path: Path = FINALISTS) -> list[dict]:
    rows = read_table(path)
    for r in rows:
        r["config"] = HwConfig(**{k: int(r[k]) for k in dse.KNOBS})
    return rows


# --- the run ---------------------------------------------------------------------------------


def build(row: dict) -> dict:
    """One finalist through csynth and the RTL run (the brute-force job list, no waveform, the
    build kept whole for Vivado), then implementation.  Returns ``{build, ok, seconds, error}``.
    """
    name, c = row["build"], row["config"]
    rec_path = M.POINTS_DIR / f"{name}.json"
    if not (rec_path.is_file() and "error" not in json.loads(rec_path.read_text())):
        rec = M.measure(name, "det", c, role="finalist", steady=True, trace=False)
        if "error" in rec:
            return {"build": name, "ok": False, "seconds": 0, "error": rec["error"]}
    if impl_check.implemented(name):
        return {"build": name, "ok": True, "seconds": 0, "error": ""}
    return impl_check.run_impl(name) | {"error": ""}


def impl_rows(rows: list[dict] | None = None) -> list[dict]:
    """One row per finalist: the model's numbers, csynth's and the implemented ones, with the
    measured job time and the achieved clock."""
    out = []
    for r in rows or read_finalists():
        name = r["build"]
        rec = json.loads((M.POINTS_DIR / f"{name}.json").read_text(encoding="utf-8"))
        impl = impl_check.parse_report(impl_check.report_path(name).read_text())
        secs = B.BUILD_ROOT / name / "impl.seconds"
        row = {
            k: r[k] for k in ("name", "build", "K", "speed", "guard", "W", "g_s", "nit")
        }
        for k in COUNTERS:
            row |= {
                f"model_{k}": int(r[f"model_{k}"]),
                f"csynth_{k}": rec["resources"]["total"][k],
                f"impl_{k}": impl[k],
            }
        row |= {
            "model_job": float(r["model_job"]),
            "rtl_job": rec["intervals"]["steady"][str(int(r["nit"]))],
            "bit_exact": int(rec["rtl"]["bit_exact"]),
            "csynth_est_ns": rec["resources"]["est_ns"],
            "cp_post_impl_ns": impl["cp_post_impl"],
            "timing_met": impl["timing_met"],
            "impl_seconds": float(secs.read_text()) if secs.is_file() else "",
            "tool": impl["tool"],
        }
        out.append(row)
    return out


def pair_rows(rows: list[dict]) -> list[dict]:
    """Per pair (one K, one job-time budget): what leaving the guard bits out costs, in percent
    of the design with guard bits, by the model, by csynth and as implemented."""
    by = {(r["K"], r["speed"], r["guard"]): r for r in rows}
    out = []
    for (K, speed, guard), with_g in by.items():
        if guard != "guard":
            continue
        without = by[(K, speed, "no guard")]
        row = {
            "K": K,
            "speed": speed,
            "with": f"W{with_g['W']}g{with_g['g_s']} n{with_g['nit']}",
            "without": f"W{without['W']}g{without['g_s']} n{without['nit']}",
        }
        for k in COUNTERS:
            for src in ("model", "csynth", "impl"):
                a, b = float(with_g[f"{src}_{k}"]), float(without[f"{src}_{k}"])
                row[f"{src}_{k}_pct"] = round(100.0 * (b - a) / a, 2) if a else ""
        row["rtl_job_pct"] = round(
            100.0
            * (float(without["rtl_job"]) - float(with_g["rtl_job"]))
            / float(with_g["rtl_job"]),
            2,
        )
        out.append(row)
    return out


def write(out_dir: Path = PAPER_DATA) -> dict:
    rows = impl_rows()
    tools = sorted({r["tool"] for r in rows})
    note = provenance(
        "finalists_impl",
        tool="+".join(tools),
        hls=impl_check._hls_tool(),
        part=B.PART,
        period_ns=B.PERIOD_NS,
        flow="export_design -flow impl",
    )
    out = {
        "finalists_impl": Path(out_dir) / "finalists_impl.csv",
        "finalists_pairs": Path(out_dir) / "finalists_pairs.csv",
    }
    write_table(out["finalists_impl"], rows, note)
    write_table(out["finalists_pairs"], pair_rows(rows), note)
    return out


def done(rows: list[dict] | None = None) -> list[str]:
    """The finalists that are measured and implemented."""
    return [
        r["build"]
        for r in rows or read_finalists()
        if (M.POINTS_DIR / f"{r['build']}.json").is_file()
        and impl_check.implemented(r["build"])
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help=f"write {FINALISTS.name}")
    ap.add_argument("--run", action="store_true", help="build and implement (long)")
    ap.add_argument(
        "--shard",
        default="0/1",
        help="i/n: every n-th finalist from the i-th, one after the other; run the n shards "
        "as separate processes to build in parallel",
    )
    args = ap.parse_args(argv)
    if args.write:
        path = write_finalists()
        for r in read_table(path):
            print(
                f"  {r['name']:20s} {r['build']:50s} LUT {int(r['model_lut']):6d}  "
                f"DSP {int(r['model_dsp']):4d}  job {float(r['model_job']):8.0f}"
            )
        print("wrote", path)
        return 0
    rows = read_finalists()
    if args.run:
        i, n = (int(x) for x in args.shard.split("/"))
        for row in rows[i::n]:
            print(build(row), flush=True)
    if len(done(rows)) < len(rows):
        print(
            f"{len(done(rows))} of {len(rows)} finalists implemented; no table written yet"
        )
        return 0
    for name, path in write().items():
        print(f"{name} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
