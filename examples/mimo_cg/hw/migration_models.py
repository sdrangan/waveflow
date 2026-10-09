"""migration_models.py — the study's hardware models, refitted on the detector built from Waveflow's
components, and the brute force on the components scored with them.

Step 9.4e of ``plans/mimo_cg/mimo_cg_paper_sims.md``: gate 9.0's brute-force trigger fired at step
9.4d, and the user chose to re-run the brute force on the components, refit the models on the
re-measured calibration builds into a new file (the study's v2 stays frozen) and re-score the
committed decision set into new tables.

The model keeps the study's forms (:mod:`examples.mimo_cg.hw.models`, v2) and changes what the
hardware changed:

* the cores' DSP and block RAM are their components' counted rules
  (:func:`examples.mimo_cg.hw.migration.counted`), not the old blocks'; their LUT and FF keep the
  study's regressions (``CgVec.*`` and ``CgMm.*``, refitted);
* the loader deserializes ``A`` in L-lane groups, as it does ``B``: two L-lane parts instead of a
  K-lane and an L-lane one;
* the ``A`` channel holds L-lane groups (:func:`examples.mimo_cg.hw.migration.a_channel_bram`);
* every regression and measured table is refitted on the 86 re-measured calibration builds (the
  migration tables' builds of the roles ``fit`` and ``fit2``).

::

    python -m examples.mimo_cg.hw.migration_models --fit       # fit and freeze (before any build)
    python -m examples.mimo_cg.hw.migration_models --validate  # the re-measured held-out builds
    python -m examples.mimo_cg.hw.campaign --role migration_bruteforce --shard i/6 [--resume]
    python -m examples.mimo_cg.hw.campaign --merge migration_bruteforce
    python -m examples.mimo_cg.hw.migration_models --score     # the committed decisions, re-scored
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from examples.mimo_cg.hw import migration as MG
from examples.mimo_cg.hw import models as MD
from examples.mimo_cg.hw.common import DEFAULT_N
from examples.mimo_cg.hw.space import HwConfig
from examples.mimo_cg.mimo_cg import provenance, read_table, write_table

PAPER_DATA = MD.PAPER_DATA
PLATFORM_DIR = MD.HERE.parent / "calib" / "platforms" / "xczu48dr_250mhz_components"
MODEL_FILE = PLATFORM_DIR / "models" / "mimo_cg_hw.json"
PROVENANCE_FILE = PLATFORM_DIR / "models" / "mimo_cg_hw.provenance.json"
VERSION = "components-1"
KNOBS = tuple(HwConfig.__dataclass_fields__)
COUNTERS = MD.COUNTERS
#: The cores, by the study's names for the blocks they replace (the regression names).
CORE_OF = {"CgVec": "CgVectorCore", "CgMm": "SystolicCore"}
REG_OF = {v: k for k, v in CORE_OF.items()}
DETECTOR_MODULES = tuple(CORE_OF.get(m, m) for m in MD.DETECTOR_MODULES)
#: The loaders' pipelined loops, by part: the new ones are L lanes wide on both matrices.
LOADER_LOOPS = {"CgLoad": (("A", "L"), ("B", "L")), "CgStore": (("X", "L"),)}
_LOOP_NAMES = {
    ("CgLoad", "A"): ("WORDS", "WORDS_G"),
    ("CgLoad", "B"): ("WORDS1", "WORDS_G1"),
    ("CgStore", "X"): ("WORDS", "WORDS_G"),
}
BRUTEFORCE_ROLE = "migration_bruteforce"
STEM = "migration_bruteforce"


@functools.cache
def _core_counted(K: int, L: int, R: int, C: int, cmul: int, W: int, g_s: int) -> dict:
    c = HwConfig(K=K, L=L, R=R, C=C, cmul=cmul, W=W, g_s=g_s)
    cores = MG.cores("det", c)
    return {name: MG.counted(core) for name, core in cores.items()}


def core_counted(c) -> dict:
    """``{"vec": {dsp, bram}, "mm": {dsp, bram}}``: the cores' counted rules at ``c``."""
    k = MD.knobs(c)
    return _core_counted(*(k[x] for x in ("K", "L", "R", "C", "cmul", "W", "g_s")))


def channels(k: dict) -> list[tuple]:
    """The detector's stream-of-blocks channels, ``A`` in L-lane groups written as ``B`` is."""
    from waveflow.linalg.lanes import n_groups

    out = []
    for name, words, bits, banks, dual in MD.channels(k, "det"):
        if name == "a_blk":
            words, bits = n_groups(k["K"] * k["K"], k["L"]), 2 * k["W"] * k["L"]
            dual = k["L"] < k["mem_dw"] // 32
        out.append((name, words, bits, banks, dual))
    return out


class ComponentModels(MD.Models):
    """The study's model of the detector, on the components' cores (see the module doc)."""

    def module(self, cls: str, c) -> dict:
        k = MD.knobs(c)
        if cls in REG_OF:
            reg = REG_OF[cls]
            fn, counted = (
                (MD._vec_terms, MD.vec_counted)
                if reg == "CgVec"
                else (MD._mm_terms, MD.mm_counted)
            )
            cnt, terms = counted(k), fn(
                k
            )  # the structural LUT and FF, as the study counts
            core = core_counted(c)["vec" if reg == "CgVec" else "mm"]
            return {
                "lut": self._reg(f"{reg}.lut", terms) + cnt["lut"],
                "ff": self._reg(f"{reg}.ff", terms) + cnt["ff"],
                "dsp": core["dsp"],
                "bram": core["bram"],
            }
        if cls in LOADER_LOOPS:
            own = self._row(f"{cls}.own", k["mem_dw"])
            short = "load" if cls == "CgLoad" else "store"
            parts = [
                self._lanes(short, k[lanes], k["W"], k["mem_dw"])
                for _part, lanes in LOADER_LOOPS[cls]
            ]
            return {
                "lut": own["lut"] + sum(p["lut"] for p in parts),
                "ff": own["ff"] + sum(p["ff"] for p in parts),
                "dsp": 0,
                "bram": 0,
            }
        return super().module(cls, c)

    def integration(self, c) -> dict:
        k = MD.knobs(c)
        out = {ctr: 0 for ctr in COUNTERS}
        for _name, words, bits, banks, dual in channels(k):
            mem = MD.sob_memory(words, bits, banks, dual)
            for ctr in ("lut", "ff", "bram"):
                out[ctr] += mem[ctr]
        for row in (
            self._row("adapters", k["mem_dw"]),
            self._row("fifos", k["cmd_depth"]),
        ):
            for ctr in ("lut", "ff", "bram"):
                out[ctr] += row.get(ctr, 0)
        return out

    def resources(self, c) -> dict:
        mods = {cls: self.module(cls, c) for cls in DETECTOR_MODULES}
        mods["integration"] = self.integration(c)
        mods = {
            name: {ctr: round(row[ctr]) for ctr in COUNTERS}
            for name, row in mods.items()
        }
        total = {ctr: sum(row[ctr] for row in mods.values()) for ctr in COUNTERS}
        return {"total": total, "modules": mods}


# --- the fit ----------------------------------------------------------------------------------


def _view(
    data_dir: Path, roles: tuple[str, ...]
) -> tuple[dict, list[dict], list[dict]]:
    """The re-measured builds of ``roles`` (their old roles in the list), under their old labels:
    ``(configs by build, module rows, cycle rows)``, with ``top`` and ``role`` as in the study.
    """
    plan = {r["build"]: r for r in MG.read_list(Path(data_dir) / MG.LIST.name)}
    keep = {b for b, r in plan.items() if r["role"] in roles}

    def relabel(r: dict) -> dict:
        p = plan[r["build"]]
        return r | {"build": p["old"], "top": p["top"], "role": p["role"]}

    cfg = {plan[b]["old"]: {k: int(plan[b][k]) for k in KNOBS} for b in sorted(keep)}
    modules = [
        relabel(r)
        for r in read_table(Path(data_dir) / "migration_modules.csv")
        if r["build"] in keep
    ]
    cycles = [
        relabel(r)
        for r in read_table(Path(data_dir) / "migration_cycles.csv")
        if r["build"] in keep
    ]
    return cfg, modules, cycles


def _loop(sub: dict, build: str, cls: str, part: str) -> dict:
    for name in _LOOP_NAMES[(cls, part)]:
        if (build, f"{cls}.{name}") in sub:
            return sub[(build, f"{cls}.{name}")]
    raise KeyError((build, cls, part))


def fit(data_dir: Path = PAPER_DATA, loo: bool = True) -> ComponentModels:
    """The study's fit (:func:`examples.mimo_cg.hw.models.fit`), on the re-measured calibration
    builds and the components' structure."""
    cfg, modules, cycles = _view(data_dir, MD.FIT_ROLES)
    regress = functools.partial(MD._regress, loo=loo)
    m = ComponentModels(
        meta={
            "part": MD.PART,
            "period_ns": MD.B.PERIOD_NS,
            "fit_builds": len(cfg),
            "N": DEFAULT_N,
            "version": VERSION,
            "fit_roles": list(MD.FIT_ROLES),
            "data": "migration_*.csv (plan step 9.4b)",
        }
    )

    def rows(top: str, kind: str) -> list[dict]:
        return [r for r in modules if r["top"] == top and r["kind"] == kind]

    def val(r: dict, ctr: str) -> int:
        return int(r[ctr])

    # the two cores, each from its own unit builds: the study's counted LUT and FF come out first
    for reg, top, terms, counted in (
        ("CgVec", "vec", MD._vec_terms, MD.vec_counted),
        ("CgMm", "mm", MD._mm_terms, MD.mm_counted),
    ):
        own = [r for r in rows(top, "module") if r["name"] == CORE_OF[reg]]
        for ctr in ("lut", "ff"):
            samples = [
                (terms(cfg[r["build"]]), val(r, ctr) - counted(cfg[r["build"]])[ctr])
                for r in own
            ]
            name = f"{reg}.{ctr}"
            m.coef[name], m.report[name] = regress(
                name, samples, scale=[val(r, ctr) for r in own]
            )

    # the glue and the channels, from detector builds only
    det = rows("det", "module")
    sub = {(r["build"], r["name"]): r for r in rows("det", "subblock")}
    for cls in ("MemRStream", "MemWStream"):
        for mem_dw in sorted({cfg[r["build"]]["mem_dw"] for r in det}):
            got = [
                {"lut": val(r, "lut"), "ff": val(r, "ff")}
                for r in det
                if r["name"] == cls and cfg[r["build"]]["mem_dw"] == mem_dw
            ]
            m.table[f"{cls}|{mem_dw}"] = MD._single(got, f"{cls} at {mem_dw}-bit words")
    for cls in ("CgCmdRx", "CgCtrl"):
        own = [r for r in det if r["name"] == cls]
        for ctr in ("lut", "ff"):
            name = f"{cls}.{ctr}"
            m.coef[name], m.report[name] = regress(
                name, [(MD._small_terms(cfg[r["build"]]), val(r, ctr)) for r in own]
            )
    for cls, parts in LOADER_LOOPS.items():
        short = "load" if cls == "CgLoad" else "store"
        own = [r for r in det if r["name"] == cls]
        for ctr in ("lut", "ff"):
            samples = {}
            for r in own:
                k = cfg[r["build"]]
                for part, lanes in parts:
                    key = (k[lanes], k["W"], k["mem_dw"], part)
                    samples[key] = (
                        MD._lane_terms(k[lanes], k["W"], k["mem_dw"]),
                        val(_loop(sub, r["build"], cls, part), ctr),
                    )
            name = f"{short}.{ctr}"
            m.coef[name], m.report[name] = regress(name, list(samples.values()))
        for mem_dw in sorted({cfg[r["build"]]["mem_dw"] for r in own}):
            rest = [
                {
                    ctr: val(r, ctr)
                    - sum(val(_loop(sub, r["build"], cls, p), ctr) for p, _ in parts)
                    for ctr in ("lut", "ff")
                }
                for r in own
                if cfg[r["build"]]["mem_dw"] == mem_dw
            ]
            m.table[f"{cls}.own|{mem_dw}"] = {
                ctr: round(sum(x[ctr] for x in rest) / len(rest))
                for ctr in ("lut", "ff")
            }
    dets = sorted({r["build"] for r in det})
    for mem_dw in sorted({cfg[b]["mem_dw"] for b in dets}):
        got = []
        for b in dets:
            if cfg[b]["mem_dw"] == mem_dw:
                inst = [r for r in rows("det", "instance") if r["build"] == b]
                got.append(
                    {
                        ctr: sum(val(r, ctr) for r in inst)
                        for ctr in ("lut", "ff", "bram")
                    }
                )
        m.table[f"adapters|{mem_dw}"] = MD._single(
            got, f"adapters at {mem_dw}-bit words"
        )
    for depth in sorted({cfg[b]["cmd_depth"] for b in dets}):
        got = []
        for b in dets:
            if cfg[b]["cmd_depth"] == depth:
                fifo = [
                    r
                    for r in modules
                    if r["build"] == b and r["kind"] in ("fifo", "unitemized")
                ]
                got.append(
                    {
                        ctr: sum(val(r, ctr) for r in fifo)
                        for ctr in ("lut", "ff", "bram")
                    }
                )
        lut = sorted({g["lut"] for g in got})
        m.table[f"fifos|{depth}"] = {
            **MD._single(
                [{k: v for k, v in g.items() if k != "lut"} for g in got], "FIFOs"
            ),
            "lut": lut[len(lut) // 2],
        }

    # cycles: core spans from the unit builds; the loop and job offsets from the detectors
    def span(top: str, q: str) -> list[tuple[dict, float]]:
        return [
            (cfg[r["build"]], float(r["cycles"]))
            for r in cycles
            if r["top"] == top and r["quantity"] == q
        ]

    for name, top in (("vec.iter", "vec"), ("vec.init", "vec"), ("mm.iter", "mm")):
        got = span(top, name)
        counted = [MD.mm_iter_counted(k) if name == "mm.iter" else 0 for k, _y in got]
        m.coef[name], m.report[name] = regress(
            name,
            [
                (MD.TERMS[name][0](k), y - cnt)
                for (k, y), cnt in zip(got, counted, strict=True)
            ],
            scale=[y for _k, y in got],
        )
    by = {
        (r["build"], r["quantity"]): float(r["cycles"])
        for r in cycles
        if r["top"] == "det" and r["quantity"] != "job_interval"
    }
    gaps = {by[(b, "t_iter")] - by[(b, "mm.iter")] - by[(b, "vec.iter")] for b in dets}
    m.table["handoff|loop"] = {
        "cycles": MD._single([{"cycles": g} for g in gaps], "loop handoff")["cycles"]
    }
    for mem_dw in sorted({cfg[b]["mem_dw"] for b in dets}):
        extra = [
            {"cycles": round(by[(b, "t0")] - by[(b, "vec.init")])}
            for b in dets
            if cfg[b]["mem_dw"] == mem_dw
        ]
        m.table[f"t0_extra|{mem_dw}"] = MD._single(
            extra, f"job overhead at {mem_dw}-bit words"
        )
    return m


def model_sha256(path: Path = MODEL_FILE) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(m: ComponentModels) -> Path:
    path = m.save(MODEL_FILE)
    data = {
        n: hashlib.sha256((PAPER_DATA / f"{n}.csv").read_bytes()).hexdigest()
        for n in (
            "migration_list",
            "migration_builds",
            "migration_modules",
            "migration_cycles",
        )
    }
    PROVENANCE_FILE.write_text(
        json.dumps(
            {"version": VERSION, "sha256": model_sha256(path), "fit_data_sha256": data,
             "step": "9.4e", "tool": "vitis_hls 2024.1"},
            indent=1, sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )  # fmt: skip
    return path


@functools.cache
def calibrated() -> ComponentModels:
    return ComponentModels.load(MODEL_FILE)


# --- the re-measured held-out builds ----------------------------------------------------------


def validate(
    data_dir: Path = PAPER_DATA, models: ComponentModels | None = None
) -> list[dict]:
    """The model against every re-measured held-out build: per detector its totals and cycles,
    per unit build its core's row and span.  Reported, with no thresholds."""
    models = models or calibrated()
    plan = {r["old"]: r for r in MG.read_list(Path(data_dir) / MG.LIST.name)}
    held = ("holdout", "supplement", "supplement2")
    cfg, modules, cycles = _view(data_dir, held)
    totals = {
        MG.old_label(r["build"]): r
        for r in read_table(Path(data_dir) / "migration_builds.csv")
    }
    cyc = {
        (r["build"], r["quantity"]): float(r["cycles"]) for r in cycles if r["cycles"]
    }
    out = []
    for b, k in cfg.items():
        top, c = plan[b]["top"], HwConfig(**k)
        if top == "det":
            res, t = models.resources(c)["total"], models.cycles(c)
            got = {ctr: float(totals[b][ctr]) for ctr in COUNTERS}
            got |= {q: cyc[(b, q)] for q in ("t0", "t_iter")}
            pred = {**res, **t}
            scope = "detector"
        else:
            cls = "CgVectorCore" if top == "vec" else "SystolicCore"
            (row,) = [
                r
                for r in modules
                if r["build"] == b and r["kind"] == "module" and r["name"] == cls
            ]
            got = {ctr: float(row[ctr]) for ctr in COUNTERS}
            pred = dict(models.module(cls, c))
            span = "vec.iter" if top == "vec" else "mm.iter"
            got[span], pred[span] = cyc[(b, span)], models.span(span, c)
            scope = f"block:{cls}"
        for q, g in got.items():
            p = float(pred[q])
            out.append(
                {"build": b, "role": plan[b]["role"], "scope": scope, "quantity": q,
                 "measured": g, "predicted": round(p, 3),
                 "error_pct": round(100 * (p - g) / g, 3) if g else ""}
            )  # fmt: skip
    return out


# --- the brute force on the components, scored ------------------------------------------------


def bf_label(old: str) -> str:
    return MG.PREFIX + old


def bruteforce_builds() -> list[tuple[str, str, str, HwConfig]]:
    """The campaign's builds of the role: the 1,440 detectors of the committed sub-grid, each
    labelled ``mig_<bf label>``."""
    from examples.mimo_cg.hw.space import read_bruteforce

    return [(bf_label(b), t, BRUTEFORCE_ROLE, c) for b, t, _r, c in read_bruteforce()]


def _bruteforce_view(data_dir: Path, out: Path) -> Path:
    """The merged tables of the role, as ``bruteforce_builds.csv`` and ``bruteforce_cycles.csv``
    under the brute force's own labels, for :func:`examples.mimo_cg.hw.fidelity.measured_table`.
    """
    for name in ("builds", "cycles"):
        rows = read_table(Path(data_dir) / f"{STEM}_{name}.csv")
        for r in rows:
            r["build"] = MG.old_label(r["build"])
        write_table(out / f"bruteforce_{name}.csv", rows, "view")
    return out


def score(data_dir: Path = PAPER_DATA, models: ComponentModels | None = None) -> dict:
    """The committed decisions (``bruteforce_decisions.csv``, their job-time budgets), each
    re-made with this model and judged on the brute force measured on the components, as
    :func:`examples.mimo_cg.hw.fidelity.score` judges (``check=False``: the picks are this
    model's).  Returns ``{table: rows}``."""
    from examples.mimo_cg.hw import dse
    from examples.mimo_cg.hw import fidelity as F

    models = models or calibrated()
    with tempfile.TemporaryDirectory() as tmp:
        measured = F.measured_table(_bruteforce_view(Path(data_dir), Path(tmp)))
    hw, acc = F.subgrid_table(models), dse.accuracy()
    committed = read_table(F.DECISIONS)
    rows = F.score(hw, acc, measured, committed, check=False)
    errors = F.model_errors(hw, measured)
    return {
        f"{STEM}_decisions": rows,
        f"{STEM}_fidelity_metrics": F.metrics(rows),
        f"{STEM}_errors": errors,
        f"{STEM}_error_metrics": F.error_metrics(errors),
    }


def write_scores(data_dir: Path = PAPER_DATA, out_dir: Path = PAPER_DATA) -> dict:
    note = provenance(
        "migration_bruteforce_scores",
        tool=_tool(Path(data_dir) / f"{STEM}_builds.csv"),
        model_sha256=model_sha256()[:16],
        model=VERSION,
        part=MD.PART,
        period_ns=MD.B.PERIOD_NS,
    )
    out = {}
    for name, rows in score(data_dir).items():
        out[name] = Path(out_dir) / f"{name}.csv"
        write_table(out[name], rows, note)
    return out


def _tool(table: Path) -> str:
    """The tool a merged table was measured with (its header's ``tool=``)."""
    import re

    head = Path(table).read_text(encoding="utf-8").splitlines()[0]
    return re.search(r"tool=([^,]+)", head).group(1)


def write_validation(data_dir: Path = PAPER_DATA, out_dir: Path = PAPER_DATA) -> Path:
    path = Path(out_dir) / "migration_model_validation.csv"
    note = provenance(
        "migration_model_validation",
        tool=_tool(Path(data_dir) / "migration_builds.csv"),
        model_sha256=model_sha256()[:16],
    )
    write_table(path, validate(data_dir), note)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--fit", action="store_true", help=f"fit and write {MODEL_FILE.name}"
    )
    ap.add_argument("--validate", action="store_true", help="the held-out builds")
    ap.add_argument("--score", action="store_true", help="score the decisions")
    args = ap.parse_args(argv)
    if args.fit:
        m = fit()
        print("wrote", freeze(m), model_sha256())
        print(f"{'regression':12s}  n terms  max|res|  LOO mean  LOO max")
        for name, r in m.report.items():
            print(
                f"{name:12s} {r['n']:2d} {r['terms']:5d} {r['max_abs_residual']:9.1f} "
                f"{r['loo_mape_pct']:8.2f}% {r['loo_max_pct']:7.2f}%"
            )
    if args.validate:
        print("wrote", write_validation())
    if args.score:
        for name, path in write_scores().items():
            print(f"{name} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
