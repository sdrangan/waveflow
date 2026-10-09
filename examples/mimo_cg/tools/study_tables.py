"""study_tables.py — the hardware study's pure-Python tables, regenerated and compared.

Gate 9.0 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (§14, item 7): Phase 9 rebuilds the detector on
Waveflow's components, and the study's pure-Python tooling must keep regenerating its committed
tables byte for byte at HEAD.  :data:`TABLES` lists them with their writers: the design space and
its splits (``space``), the frozen models' validation (``validate``), the DSE (``dse``), the
decision set, the decision-fidelity scores and the learning curve (``fidelity``), the finding
(``finding``) and the finalists' list (``finalists``).  Each writer runs into a temporary directory
and its output is compared with ``paper_data/``, which nothing here writes.  Figures are not
compared.

Every other table of ``paper_data/`` is named in :data:`ELSEWHERE` with where it comes from: the
floating-point and accuracy runs, the tables that hardware builds produced (regenerated at the
commit that built them, §14 gate 9.0, item 8), and the component calibrations.

    python -m examples.mimo_cg.tools.study_tables            # list the tables
    python -m examples.mimo_cg.tools.study_tables --check    # regenerate and compare
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
PAPER = HERE / "paper_data"


def _space(name: str, writer: str) -> Callable[[Path], object]:
    def run(d: Path) -> object:
        from examples.mimo_cg.hw import space

        return getattr(space, writer)(d / f"{name}.csv")

    return run


def _validate(role: str) -> Callable[[Path], object]:
    def run(d: Path) -> object:
        from examples.mimo_cg.hw import validate

        return validate.validate(out_dir=d, role=role)

    return run


def _dse(d: Path) -> object:
    from examples.mimo_cg.hw import dse

    return dse.write(d)


def _decisions(d: Path) -> object:
    from examples.mimo_cg.hw import fidelity

    return fidelity.write_decisions(d / "bruteforce_decisions.csv")


def _scores(d: Path) -> object:
    from examples.mimo_cg.hw import fidelity

    return fidelity.write_scores(out_dir=d)


def _curve(d: Path) -> object:
    from examples.mimo_cg.hw import fidelity

    return fidelity.write_learning_curve(out_dir=d)


def _finding(d: Path) -> object:
    from examples.mimo_cg.hw import finding

    return finding.write(d)


def _finalists(d: Path) -> object:
    from examples.mimo_cg.hw import finalists

    return finalists.write_finalists(d / "finalists.csv")


def _migration_list(d: Path) -> object:
    from examples.mimo_cg.hw import migration

    return migration.write_list(d / "migration_list.csv")


def _migration_compare(d: Path) -> object:
    from examples.mimo_cg.hw import migration

    return migration.write_compare(out_dir=d)


def _migration_validation(d: Path) -> object:
    from examples.mimo_cg.hw import migration_models

    return migration_models.write_validation(out_dir=d)


def _migration_scores(d: Path) -> object:
    from examples.mimo_cg.hw import migration_models

    return migration_models.write_scores(out_dir=d)


#: (what, the tables it writes, the writer into a directory), in the study's order; the last four
#: are the re-measurement on Waveflow's components (plan step 9.4).
TABLES: tuple[tuple[str, tuple[str, ...], Callable[[Path], object]], ...] = (
    ("space: split", ("holdout_split",), _space("holdout_split", "write_split")),
    (
        "space: supplement",
        ("holdout_supplement",),
        _space("holdout_supplement", "write_supplement"),
    ),
    ("space: second round", ("calibration_v2",), _space("calibration_v2", "write_v2")),
    (
        "space: brute-force grid",
        ("bruteforce_grid",),
        _space("bruteforce_grid", "write_bruteforce"),
    ),
    (
        "validate: held-out",
        ("model_validation", "model_validation_metrics"),
        _validate("holdout"),
    ),
    (
        "validate: supplement",
        ("model_validation_supplement", "model_validation_supplement_metrics"),
        _validate("supplement"),
    ),
    (
        "validate: supplement2",
        ("model_validation_supplement2", "model_validation_supplement2_metrics"),
        _validate("supplement2"),
    ),
    ("dse", ("dse_frontier", "dse_scenarios"), _dse),
    ("fidelity: decisions", ("bruteforce_decisions",), _decisions),
    (
        "fidelity: scores",
        (
            "bruteforce_error_metrics",
            "bruteforce_errors",
            "bruteforce_frontiers",
            "decision_fidelity",
            "decision_fidelity_metrics",
            "dse_cost",
        ),
        _scores,
    ),
    ("fidelity: learning curve", ("learning_curve",), _curve),
    ("finding", ("dse_guard_pairs", "dse_guard", "dse_shape"), _finding),
    ("finalists: the list", ("finalists",), _finalists),
    ("migration: the list", ("migration_list",), _migration_list),
    (
        "migration: comparison",
        ("migration_compare", "migration_metrics"),
        _migration_compare,
    ),
    (
        "migration: model validation",
        ("migration_model_validation",),
        _migration_validation,
    ),
    (
        "migration: brute-force scores",
        (
            "migration_bruteforce_decisions",
            "migration_bruteforce_fidelity_metrics",
            "migration_bruteforce_errors",
            "migration_bruteforce_error_metrics",
        ),
        _migration_scores,
    ),
)

_BUILT = "built with Vitis HLS / Vivado 2024.1 at 6a2cdca; regenerated at that commit"
_ON_COMPONENTS = (
    "built on Waveflow's components (step 9.4, Vitis HLS / Vivado xsim 2024.1)"
)

#: Every other committed table (a name ending in ``*`` is a prefix), and where it comes from.
ELSEWHERE: dict[str, str] = {
    "float_ber": "Phase 1 floating-point runs (mimo_cg_build.py)",
    "float_ranges": "Phase 1 floating-point runs (mimo_cg_build.py)",
    "zf_crossings": "Phase 1 floating-point runs (mimo_cg_build.py)",
    "accuracy_grid": "Phase 3 accuracy sweep; 26 points checked by accuracy_points --check",
    "accuracy_losses": "Phase 3 accuracy analysis (mimo_cg_accuracy_analysis.py)",
    "accuracy_frontier": "Phase 3 accuracy analysis (mimo_cg_accuracy_analysis.py)",
    "accuracy_stress_points": "gate 8.0; checked by accuracy_points --check",
    "hw_builds": _BUILT,
    "hw_modules": _BUILT,
    "hw_cycles": _BUILT,
    "bruteforce_builds": _BUILT,
    "bruteforce_modules": _BUILT,
    "bruteforce_cycles": _BUILT,
    "impl_check": _BUILT,
    "impl_check_modules": _BUILT,
    "finalists_impl": _BUILT,
    "finalists_pairs": _BUILT,
    "run_log": "wall-clock times written by hand when each campaign ran",
    "model_validation_v1_metrics": "frozen copy of model v1's scores (step 6.1)",
    "model_validation_v1_supplement_metrics": "frozen copy of model v1's scores (step 6.1)",
    "linalg_*": "systolic unit calibration (hw/linalg_cal.py), Vitis HLS / Vivado xsim 2024.1",
    "cg_*": "CG vector unit calibration (hw/cg_cal.py), Vitis HLS / Vivado xsim 2024.1",
    "migration_builds": _ON_COMPONENTS,
    "migration_modules": _ON_COMPONENTS,
    "migration_cycles": _ON_COMPONENTS,
    "migration_finalists": _ON_COMPONENTS
    + "; Vivado v.2024.1, read from the build trees",
    "migration_finalists_modules": _ON_COMPONENTS + "; Vivado's hierarchical reports",
    "migration_bruteforce_builds": _ON_COMPONENTS,
    "migration_bruteforce_modules": _ON_COMPONENTS,
    "migration_bruteforce_cycles": _ON_COMPONENTS,
}


def checked_names() -> list[str]:
    return [name for _what, names, _w in TABLES for name in names]


def origin(name: str) -> str | None:
    """Where a committed table that :data:`TABLES` does not check comes from."""
    if name in ELSEWHERE:
        return ELSEWHERE[name]
    for key, where in ELSEWHERE.items():
        if key.endswith("*") and name.startswith(key[:-1]):
            return where
    return None


def _digests() -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(PAPER.iterdir())
    }


def check() -> int:
    """Regenerate every table of :data:`TABLES` and compare; returns the number that differ."""
    before, bad, t_all = _digests(), 0, time.time()
    with tempfile.TemporaryDirectory(prefix="study_tables_") as tmp:
        for what, names, writer in TABLES:
            t0 = time.time()
            d = Path(tmp) / what.replace(": ", "_").replace(" ", "_")
            d.mkdir()
            writer(d)
            for name in names:
                got = d / f"{name}.csv"
                same = (
                    got.is_file()
                    and got.read_bytes() == (PAPER / f"{name}.csv").read_bytes()
                )
                bad += not same
                print(
                    f"{what:26s} {name:40s} {'identical' if same else 'DIFFERENT'}"
                    f"  {time.time() - t0:5.1f} s",
                    flush=True,
                )
    if _digests() != before:
        print("paper_data/ changed during the check")
        bad += 1
    n = len(checked_names())
    print(f"{n - bad} of {n} tables identical, {time.time() - t_all:.0f} s")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="regenerate and compare")
    args = ap.parse_args(argv)
    if args.check:
        return 1 if check() else 0
    for what, names, _w in TABLES:
        print(f"{what:26s} {', '.join(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
