"""mimo_cg_build.py — Phase 1's build: floating-point BER, ZF crossings, ranges and figures.

Step 1.4 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  Tables go to ``paper_data/`` (committed);
figures go to ``results/figures/`` (not committed).  Run from the repo root::

    python -m examples.mimo_cg.mimo_cg_build --list-steps
    python -m examples.mimo_cg.mimo_cg_build --through float_figures
    python -m examples.mimo_cg.mimo_cg_build --through float_figures --force   # re-run all

``--workers`` changes only the wall-clock time: every point is seeded by its parameters and
simulated with single-threaded BLAS, so the tables are identical for any worker count.

``--max-bits`` is for quick trials only.  The committed ``paper_data/float_ber.csv`` is the
default budget (1e7 bits per point), which its provenance line records and
``tests/examples/test_mimo_cg_build.py`` checks.  A changed parameter alone does not mark the
step stale, so regenerate the committed table with ``--force`` and the default budget.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from examples.mimo_cg.mimo_cg import (
    MAX_BITS,
    MIN_ERRORS,
    NS,
    SNR_DB,
    TARGET_BER,
    all_configs,
    mmse_crossing_db,
    provenance,
    run_float_ber,
    run_float_ranges,
    run_zf_crossings,
    write_table,
)
from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli

_SOURCE_DIR = Path(__file__).resolve().parent

#: Default worker processes.  Results do not depend on it (tested), so the default uses the
#: machine: a serial run of the full grid takes hours, and the DAG re-runs the tables whenever a
#: model source file changes, even if only a docstring did.
DEFAULT_WORKERS = min(8, os.cpu_count() or 1)
_SOURCES = {
    "mimo_link_source": "mimo_link.py",
    "detectors_source": "detectors.py",
    "mimo_cg_source": "mimo_cg.py",
    "figures_source": "mimo_cg_figures.py",
}
_MODEL: list[str] = ["mimo_link_source", "detectors_source", "mimo_cg_source"]


def _paper_data(config: BuildConfig, name: str) -> Path:
    path = Path(config.root_dir) / "paper_data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(kw_only=True)
class ZfCrossingsStep(BuildStep):
    description = "Analytical ZF SNR at BER 1e-3 for every (M, K, modulation)."
    consumes: ClassVar[list] = _MODEL
    produces: ClassVar[dict] = {"zf_crossings": Path("paper_data/zf_crossings.csv")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        path = _paper_data(config, "zf_crossings.csv")
        write_table(
            path, run_zf_crossings(), provenance("zf_crossings", target_ber=TARGET_BER)
        )
        return {"zf_crossings": path}


@dataclass(kw_only=True)
class FloatBerStep(BuildStep):
    description = (
        "Floating-point BER vs SNR of ZF, exact MMSE and CG-MMSE over the 27 configs."
    )
    consumes: ClassVar[list] = _MODEL
    produces: ClassVar[dict] = {"float_ber": Path("paper_data/float_ber.csv")}
    params: ClassVar[dict] = {"workers": DEFAULT_WORKERS, "max_bits": MAX_BITS}

    def run(self, config: BuildConfig, workers, max_bits, **_) -> dict:
        rows = run_float_ber(workers=workers, max_bits=max_bits)
        path = _paper_data(config, "float_ber.csv")
        comment = provenance(
            "float_ber",
            ns=NS,
            min_errors=MIN_ERRORS,
            max_bits=max_bits,
            snr_db="-20..20",
        )
        write_table(path, rows, comment)
        missing = []
        for cfg in all_configs():
            mmse = [
                r
                for r in rows
                if (r["M"], r["K"], r["modulation"], r["detector"])
                == (cfg.M, cfg.K, cfg.modulation, "mmse")
            ]
            if mmse_crossing_db(mmse) is None:
                missing.append(cfg)
        if missing:
            raise RuntimeError(
                f"exact MMSE never reaches BER {TARGET_BER} on {SNR_DB[0]}..{SNR_DB[-1]} dB for "
                f"{missing}; see {path}"
            )
        return {"float_ber": path}


@dataclass(kw_only=True)
class FloatRangesStep(BuildStep):
    description = (
        "Dynamic range of every CG variable, with and without normalizing by M."
    )
    consumes: ClassVar[list] = _MODEL
    produces: ClassVar[dict] = {"float_ranges": Path("paper_data/float_ranges.csv")}
    params: ClassVar[dict] = {"workers": DEFAULT_WORKERS}

    def run(self, config: BuildConfig, workers, **_) -> dict:
        path = _paper_data(config, "float_ranges.csv")
        write_table(path, run_float_ranges(workers=workers), provenance("float_ranges"))
        return {"float_ranges": path}


@dataclass(kw_only=True)
class FloatFiguresStep(BuildStep):
    description = "Render the BER figures into results/figures/."
    consumes: ClassVar[list] = [
        "float_ber",
        "float_ranges",
        "zf_crossings",
        "figures_source",
    ]
    produces: ClassVar[dict] = {"float_figures": Path("results/float_figures.txt")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, float_ber, zf_crossings, **_) -> dict:
        from examples.mimo_cg.mimo_cg_figures import write_figures

        root = Path(config.root_dir)
        images = root / "results" / "figures"
        written = write_figures(float_ber, images, zf_crossings_csv=zf_crossings)
        manifest = root / "results" / "float_figures.txt"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            "".join(
                f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root.parents[1])}\n"
                for p in written
            ),
            encoding="utf-8",
        )
        return {"float_figures": manifest}


def build_mimo_cg_dag() -> BuildDag:
    dag = BuildDag()
    for artifact, path in _SOURCES.items():
        dag.add(SourceStep(artifact=artifact, path=Path(path)))
    dag.add(ZfCrossingsStep(name="zf_crossings"))
    dag.add(FloatBerStep(name="float_ber"))
    dag.add(FloatRangesStep(name="float_ranges"))
    dag.add(FloatFiguresStep(name="float_figures"))
    return dag


def main() -> None:
    run_dag_cli(
        build_mimo_cg_dag,
        description="Phase 1 of the CG massive-MIMO study: floating-point BER, ranges, figures.",
        default_through="float_figures",
        root_dir=_SOURCE_DIR,
        extra_args=[
            (
                ("--workers",),
                {
                    "type": int,
                    "default": DEFAULT_WORKERS,
                    "help": "Parallel worker processes (results do not depend on it).",
                },
            ),
            (
                ("--max-bits",),
                {"type": int, "default": MAX_BITS, "help": "Bit budget per point."},
            ),
        ],
        params_from_args=lambda a: {"workers": a.workers, "max_bits": a.max_bits},
    )


if __name__ == "__main__":
    main()
