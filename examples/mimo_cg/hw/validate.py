"""validate.py — the frozen models against the held-out builds (AC5).

Step 5.8 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  The models are **loaded**, never refitted
here: ``calib/platforms/xczu48dr_250mhz/models/mimo_cg_hw.json`` as committed in step 5.6, before
any held-out build ran.  The measurements are the ``holdout`` rows of ``paper_data/hw_*.csv``.

What is scored (fixed in this file before the held-out builds were measured)
---------------------------------------------------------------------------
*Block configurations* — the 12 held-out vector-unit builds (the ``CgVec`` row), the 12 held-out
matmul builds (the ``CgMm`` row) and the glue of the 10 held-out detectors (everything in the
detector but the two blocks: framer, load, store, control, memory streams, channels, FIFOs and
adapters).  A block configuration is **exact** when its DSP count and its BRAM count are both
predicted exactly.

*Full designs* — the 10 held-out detectors: total LUT and FF, and **job cycles**: for every
measured job of a detector, the interval between completions against ``T0 + nit·T_iter``.

AC5 asks for exact DSP and BRAM on at least 90% of the block configurations, a mean absolute
percentage error of at most 10% for LUT and for FF, and of at most 5% for job cycles, on the full
designs.  The tables also give per-block LUT and FF errors, block-span errors, and the worst case
of every metric.

Rows added at the M5 review (2026-10-04), after the results were known: they are disclosures,
not AC5 gates, and carry no threshold.

* *Blocks with BRAM > 0* and *channel memories* — most of the 34 exact block comparisons are
  zero against zero, so the table also counts the blocks whose BRAM is not zero, and every
  stream-of-blocks memory of the held-out builds against the counted rule.
* *Disjoint from the fit* — a held-out unit build whose block also sits inside a ``fit`` detector,
  and a held-out detector whose vector unit or matmul is also a ``fit`` unit build, are left out
  (:func:`overlapping`), and the AC5 metrics are given again for what remains.

``python -m examples.mimo_cg.hw.validate`` writes ``paper_data/model_validation.csv`` (one row
per build, scope and quantity), ``paper_data/model_validation_metrics.csv`` (one row per metric)
and ``docs/examples/mimo_cg/images/model_validation.svg``.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from examples.mimo_cg.hw import models as MD
from examples.mimo_cg.hw.space import HwConfig
from examples.mimo_cg.mimo_cg import provenance, read_table, write_table

HERE = Path(__file__).resolve().parent
PAPER_DATA = MD.PAPER_DATA
IMAGES = HERE.parents[2] / "docs" / "examples" / "mimo_cg" / "images"
ROLE = "holdout"
GLUE = tuple(m for m in MD.DETECTOR_MODULES if m not in ("CgVec", "CgMm"))
#: AC5's thresholds (kept as written at gate 5.0).
MIN_EXACT_PCT = 90.0
MAX_AREA_MAPE = 10.0
MAX_CYCLE_MAPE = 5.0
BLOCK_OF_TOP = {"vec": "CgVec", "mm": "CgMm"}


def _pct(pred: float, meas: float) -> float:
    return 100.0 * (pred - meas) / meas if meas else 0.0


def _row(
    build: str, top: str, scope: str, quantity: str, meas: float, pred: float
) -> dict:
    return {
        "build": build,
        "top": top,
        "scope": scope,
        "quantity": quantity,
        "measured": meas,
        "predicted": pred,
        "error": pred - meas,
        "error_pct": round(_pct(pred, meas), 4),
        "exact": int(round(pred) == round(meas)),
    }


def detail_rows(
    models: MD.Models, data_dir: Path = PAPER_DATA, role: str = ROLE
) -> list[dict]:
    """Prediction against measurement for every build of ``role``."""
    builds = [r for r in read_table(data_dir / "hw_builds.csv") if r["role"] == role]
    modules = [r for r in read_table(data_dir / "hw_modules.csv") if r["role"] == role]
    cycles = [r for r in read_table(data_dir / "hw_cycles.csv") if r["role"] == role]
    out = []
    for b in builds:
        name, top = b["build"], b["top"]
        c = HwConfig(**{k: int(b[k]) for k in HwConfig.__dataclass_fields__})
        mine = {
            r["name"]: r
            for r in modules
            if r["build"] == name and r["kind"] == "module"
        }
        cyc = [r for r in cycles if r["build"] == name]
        span = {
            r["quantity"]: float(r["cycles"])
            for r in cyc
            if "." in r["quantity"] and r["cycles"] != ""
        }
        pred_span = models.spans(c)
        if top in BLOCK_OF_TOP:  # a unit build: its block's row and its block's spans
            cls = BLOCK_OF_TOP[top]
            pred = models.module(cls, c)
            for ctr in MD.COUNTERS:
                out.append(
                    _row(
                        name,
                        top,
                        f"block:{cls}",
                        ctr,
                        int(mine[cls][ctr]),
                        round(pred[ctr]),
                    )
                )
            for q in ("vec.iter", "vec.init") if top == "vec" else ("mm.iter",):
                out.append(
                    _row(name, top, f"block:{cls}", q, span[q], round(pred_span[q], 2))
                )
            continue
        res = models.resources(c)
        glue_pred = {
            ctr: sum(res["modules"][m][ctr] for m in (*GLUE, "integration"))
            for ctr in MD.COUNTERS
        }
        for ctr in MD.COUNTERS:
            total = int(b[ctr])
            blocks = sum(int(mine[cls][ctr]) for cls in BLOCK_OF_TOP.values())
            out.append(
                _row(name, top, "block:glue", ctr, total - blocks, glue_pred[ctr])
            )
            out.append(_row(name, top, "design", ctr, total, res["total"][ctr]))
        fit = {
            r["quantity"]: float(r["cycles"])
            for r in cyc
            if r["quantity"] in ("t0", "t_iter")
        }
        timing = models.cycles(c)
        for q in ("t_iter", "t0"):
            out.append(_row(name, top, "design", q, fit[q], round(timing[q], 2)))
        for r in cyc:
            if r["quantity"] == "job_interval":
                nit = int(r["nit"])
                pred = round(models.job_cycles(c, nit), 2)
                out.append(
                    _row(
                        name,
                        top,
                        "design",
                        f"job_cycles@nit{nit}",
                        float(r["cycles"]),
                        pred,
                    )
                )
        for q in ("mm.iter", "vec.iter", "vec.init"):
            out.append(_row(name, top, "design", q, span[q], round(pred_span[q], 2)))
    return out


def _stats(rows: list[dict]) -> tuple[float, float]:
    errs = [abs(r["error_pct"]) for r in rows]
    return sum(errs) / len(errs), max(errs)


def summary_rows(detail: list[dict]) -> list[dict]:
    """One row per metric; the AC5 metrics carry their threshold and whether they meet it."""
    out = []

    def add(metric: str, n: int, value: float, worst, threshold="", passed="") -> None:
        out.append(
            {
                "metric": metric,
                "n": n,
                "value": round(value, 3),
                "worst": "" if worst == "" else round(worst, 3),
                "threshold": threshold,
                "pass": passed,
            }
        )

    # block configurations: DSP and BRAM exact
    blocks = sorted(
        {(r["build"], r["scope"]) for r in detail if r["scope"].startswith("block:")}
    )
    exact = {ctr: 0 for ctr in ("dsp", "bram", "both")}
    for build, scope in blocks:
        got = {
            r["quantity"]: r["exact"]
            for r in detail
            if (r["build"], r["scope"]) == (build, scope)
        }
        exact["dsp"] += got["dsp"]
        exact["bram"] += got["bram"]
        exact["both"] += got["dsp"] and got["bram"]
    both = 100.0 * exact["both"] / len(blocks)
    add(
        "blocks: DSP and BRAM both exact (%)",
        len(blocks),
        both,
        "",
        f">= {MIN_EXACT_PCT:g}",
        int(both >= MIN_EXACT_PCT),
    )
    for ctr in ("dsp", "bram"):
        add(
            f"blocks: {ctr.upper()} exact (%)",
            len(blocks),
            100.0 * exact[ctr] / len(blocks),
            "",
        )

    # full designs: LUT, FF and job cycles
    design = [r for r in detail if r["scope"] == "design"]
    for ctr in ("lut", "ff"):
        rows = [r for r in design if r["quantity"] == ctr]
        mean, worst = _stats(rows)
        add(
            f"designs: {ctr.upper()} MAPE (%)",
            len(rows),
            mean,
            worst,
            f"<= {MAX_AREA_MAPE:g}",
            int(mean <= MAX_AREA_MAPE),
        )
    jobs = [r for r in design if r["quantity"].startswith("job_cycles")]
    mean, worst = _stats(jobs)
    add(
        "designs: job cycles MAPE (%)",
        len(jobs),
        mean,
        worst,
        f"<= {MAX_CYCLE_MAPE:g}",
        int(mean <= MAX_CYCLE_MAPE),
    )
    for ctr in ("dsp", "bram"):
        rows = [r for r in design if r["quantity"] == ctr]
        add(
            f"designs: {ctr.upper()} exact (%)",
            len(rows),
            100.0 * sum(r["exact"] for r in rows) / len(rows),
            "",
        )
    for q in ("t_iter", "t0"):
        rows = [r for r in design if r["quantity"] == q]
        add(f"designs: {q} MAPE (%)", len(rows), *_stats(rows))

    # per block: LUT and FF, and the block spans
    for scope in ("block:CgVec", "block:CgMm", "block:glue"):
        for q in ("lut", "ff"):
            rows = [r for r in detail if r["scope"] == scope and r["quantity"] == q]
            add(f"{scope[6:]}: {q.upper()} MAPE (%)", len(rows), *_stats(rows))
    for q in ("vec.iter", "vec.init", "mm.iter"):
        rows = [
            r for r in detail if r["scope"].startswith("block:") and r["quantity"] == q
        ]
        add(f"unit builds: {q} span MAPE (%)", len(rows), *_stats(rows))
    return out


def overlapping(data_dir: Path = PAPER_DATA, role: str = ROLE) -> set[str]:
    """Held-out builds that share a block with a ``fit`` build of the other kind of top."""
    rows = read_table(data_dir / "hw_builds.csv")
    knobs = HwConfig.__dataclass_fields__
    cfg = {r["build"]: HwConfig(**{k: int(r[k]) for k in knobs}) for r in rows}
    key = {"vec": HwConfig.vec_key, "mm": HwConfig.mm_key}

    def keys(kind: str, top: str) -> set:
        return {
            key[kind](cfg[r["build"]])
            for r in rows
            if r["role"] == "fit" and r["top"] == top
        }

    fit_unit = {kind: keys(kind, kind) for kind in key}
    fit_det = {kind: keys(kind, "det") for kind in key}
    out = set()
    for r in rows:
        if r["role"] != role:
            continue
        c = cfg[r["build"]]
        if r["top"] in key and key[r["top"]](c) in fit_det[r["top"]]:
            out.add(r["build"])
        if r["top"] == "det" and any(key[kind](c) in fit_unit[kind] for kind in key):
            out.add(r["build"])
    return out


def channel_exactness(
    data_dir: Path = PAPER_DATA, role: str = ROLE
) -> tuple[int, int, int]:
    """``(memories, with BRAM > 0, reproduced exactly)`` over the held-out builds' channels."""
    builds = {
        r["build"]: r
        for r in read_table(data_dir / "hw_builds.csv")
        if r["role"] == role
    }
    n = nonzero = exact = 0
    for r in read_table(data_dir / "hw_modules.csv"):
        if r["role"] != role or r["kind"] != "memory":
            continue
        b = builds[r["build"]]
        c = HwConfig(**{k: int(b[k]) for k in HwConfig.__dataclass_fields__})
        name = r["name"].removesuffix("_U")
        dual = {ch: d for ch, _w, _b, _k, d in MD.channels(c, r["top"])}[name]
        want = MD.sob_memory(int(r["words"]), int(r["bits"]), int(r["banks"]), dual)
        n += 1
        nonzero += int(r["bram"]) > 0
        exact += {k: int(r[k]) for k in ("bram", "lut", "ff")} == want
    return n, nonzero, exact


def disclosure_rows(
    detail: list[dict], data_dir: Path = PAPER_DATA, role: str = ROLE
) -> list[dict]:
    """The rows added at the M5 review: what "exact" rests on, and the fit-disjoint subset."""
    out = []

    def add(metric: str, n: int, value: float, worst="") -> None:
        out.append(
            {
                "metric": metric,
                "n": n,
                "value": round(value, 3),
                "worst": "" if worst == "" else round(worst, 3),
                "threshold": "",
                "pass": "",
            }
        )

    def both_exact(blocks: list) -> int:
        return sum(
            by[(*b, "bram")]["exact"] and by[(*b, "dsp")]["exact"] for b in blocks
        )

    by = {(r["build"], r["scope"], r["quantity"]): r for r in detail}
    blocks = sorted(
        {(r["build"], r["scope"]) for r in detail if r["scope"].startswith("block:")}
    )
    with_bram = [b for b in blocks if by[(*b, "bram")]["measured"] > 0]
    if with_bram:
        add(
            "blocks with BRAM > 0: DSP and BRAM both exact (%)",
            len(with_bram),
            100.0 * both_exact(with_bram) / len(with_bram),
        )
    n, nonzero, exact = channel_exactness(data_dir, role)
    add("channel memories: BRAM, LUT and FF exact (%)", n, 100.0 * exact / n)
    add("channel memories with BRAM > 0 (count)", n, nonzero)

    skip = overlapping(data_dir, role)
    kept = [b for b in blocks if b[0] not in skip]
    add(
        "disjoint from fit: blocks DSP and BRAM both exact (%)",
        len(kept),
        100.0 * both_exact(kept) / len(kept),
    )
    design = [r for r in detail if r["scope"] == "design" and r["build"] not in skip]
    for label, rows in (
        ("LUT", [r for r in design if r["quantity"] == "lut"]),
        ("FF", [r for r in design if r["quantity"] == "ff"]),
        ("job cycles", [r for r in design if r["quantity"].startswith("job_cycles")]),
    ):
        add(f"disjoint from fit: designs {label} MAPE (%)", len(rows), *_stats(rows))
    return out


def hls_tool(data_dir: Path = PAPER_DATA) -> str:
    """The tool version the measurements were taken with (``hw_builds.csv``'s header)."""
    import re

    head = (data_dir / "hw_builds.csv").read_text(encoding="utf-8").splitlines()[0]
    return re.search(r"tool=([^,]+)", head).group(1)


def model_sha256() -> str:
    return hashlib.sha256(MD.MODEL_FILE.read_bytes()).hexdigest()


def validate(
    data_dir: Path = PAPER_DATA, out_dir: Path = PAPER_DATA, role: str = ROLE
) -> tuple[list[dict], list[dict]]:
    """Score the committed models on the rows of ``role`` and write the two tables.

    ``holdout`` is AC5's set and writes ``model_validation*.csv``.  ``supplement`` is the extra
    held-out set of the M5 review: the same metrics, in ``model_validation_supplement*.csv``, with
    no thresholds, because AC5 is judged on the first set.
    """
    models = MD.Models.load()
    detail = detail_rows(models, data_dir, role)
    summary = summary_rows(detail) + disclosure_rows(detail, data_dir, role)
    stem = "model_validation" if role == ROLE else f"model_validation_{role}"
    if role != ROLE:
        summary = [r | {"threshold": "", "pass": ""} for r in summary]
    note = provenance(
        stem,
        tool=hls_tool(data_dir),
        role=role,
        model_sha256=model_sha256()[:16],
        part=MD.PART,
    )
    write_table(Path(out_dir) / f"{stem}.csv", detail, note)
    write_table(Path(out_dir) / f"{stem}_metrics.csv", summary, note)
    return detail, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--force", action="store_true", help="accepted for symmetry: every run rewrites"
    )
    ap.add_argument("--no-figure", action="store_true")
    ap.add_argument("--role", choices=(ROLE, "supplement"), default=ROLE)
    args = ap.parse_args(argv)
    detail, summary = validate(role=args.role)
    print(f"model {model_sha256()[:16]}…  {len(detail)} rows")
    for r in summary:
        gate = (
            f"   [{r['threshold']}: {'PASS' if r['pass'] else 'FAIL'}]"
            if r["threshold"]
            else ""
        )
        worst = f"  worst {r['worst']}" if r["worst"] != "" else ""
        print(f"  {r['metric']:42s} n={r['n']:3d}  {r['value']:8.3f}{worst}{gate}")
    if not args.no_figure and args.role == ROLE:
        from examples.mimo_cg.hw.validate_figure import render

        print("wrote", render(detail, IMAGES / "model_validation.svg"))
    return 0 if all(r["pass"] in ("", 1) for r in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
