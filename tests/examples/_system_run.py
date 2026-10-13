"""The one way an example's RTL gate runs its design: through the **system** flow
(``plans/system_model.md`` S3f).  Not a test module -- the gates import it."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from waveflow.build.build import BuildConfig
from waveflow.toolchain.toolchain import find_vivado_path

#: Where every system gate's run lives (gitignored).
WORK = Path(__file__).resolve().parents[2] / "tests" / "build" / "_xsi_work"


def system_run(dag, root: Path, top: str, through: str = "compare") -> Path:
    """Run an example's system DAG *dag* (built with ``work_dir=WORK``) through *through*; return the
    run's work directory, ``WORK/<top>``.

    ``synth="check"``: a stale or missing csynth top fails the run, naming it -- a gate never grades
    RTL it did not produce.  Without Vivado it skips, loudly (the session gate counts the skip)."""
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (xsim)")
    res = dag.run(BuildConfig(root_dir=root, params={"synth": "check"}), through=through)
    bad = {n: r.message for n, r in res.items() if not r.success}
    if bad:
        pytest.fail(f"the {top} system run failed: {bad}")
    return WORK / top


def recorded_names(work: Path) -> list[str]:
    """The recordings a run gathered, sorted."""
    return sorted(p.name for p in (Path(work) / "traces").iterdir())


def compared_clean(work: Path) -> bool:
    """Every exactly-compared recording equal, RTL against pysim."""
    return json.loads((Path(work) / "compare.json").read_text(encoding="utf-8"))[
        "trace_mismatches"] == []


def trace_of(work: Path) -> tuple[Path, dict]:
    """A traced run's ``(VCD, its one wrapped kernel's hazard manifest)`` -- the manifest carrying
    the scope the wrapper's nets are under (``SystemTopSpec.hazard_manifests``)."""
    work = Path(work)
    (manifest,) = json.loads((work / "hazards.json").read_text(encoding="utf-8"))
    return work / f"{work.name}_trace.vcd", manifest
