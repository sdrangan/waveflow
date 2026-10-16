"""BuildSteps that bridge the Vitis HLS cosim report into the build DAG.

For now this module hosts :class:`ExtractCosimTimingStep` — a thin
wrapper around :class:`~waveflow.utils.cosimparse.CosimReportParser`
that runs after Vitis cosim and serializes the kernel's transaction
cycle count to a structured JSON artifact consumable by later steps
(notably the timing validator).

Generic by design: takes ``top`` and the upstream solution-directory
artifact name as constructor arguments so a second example can use the
same step type without subclassing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildStep
from waveflow.utils.cosimparse import CosimReportParser


@dataclass(kw_only=True)
class ExtractCosimTimingStep(BuildStep):
    """Read a Vitis cosim report and emit a structured ``cosim_timing`` JSON.

    The JSON shape mirrors what :class:`ExtractPyTimingStep` produces for
    the Python side, so :class:`ValidateTimingStep` can diff them by
    pulling the same ``transaction_cycles`` field from each:

    .. code-block:: json

        {
            "transaction_cycles": 144,
            "report_path": "...sim/report/poly_cosim.rpt",
            "vitis_version": "2025.1",
            "source": "cosim"
        }

    Construction parameters
    -----------------------
    top : str
        Kernel top-module name; used to locate ``<top>_cosim.rpt``.
    report_dir_artifact : str
        Name of the upstream artifact (a directory path) where the
        cosim report lives — typically the ``report_dir`` produced by
        ``CSynthStep``.  The step joins ``<report_dir>/sim/report/`` to
        the candidate filenames.
    output_path : str
        Repo-relative location of the produced JSON artifact.
    cosim_timing_artifact : str
        Name the parsed timing is published under.  A DAG that extracts cosim timing for more
        than one top needs a distinct name per step, since BuildDag refuses two producers of one
        artifact; the downstream :class:`ValidateTimingStep` takes the matching name in its own
        ``cosim_timing_artifact``.
    """

    description: str = (
        "Parse the Vitis cosim report and emit a structured cosim_timing JSON."
    )
    params: ClassVar[dict] = {}

    top: str
    report_dir_artifact: str = "report_dir"
    output_path: str = "results/cosim_timing.json"
    cosim_timing_artifact: str = "cosim_timing"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.report_dir_artifact]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.cosim_timing_artifact: Path(self.output_path)}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        report_dir = artifacts[self.report_dir_artifact]
        sol_path = Path(report_dir)
        parser = CosimReportParser(sol_path=sol_path, top=self.top)
        cycles = parser.get_transaction_cycles()
        if cycles is None:
            raise RuntimeError(
                f"Cosim report at {parser.report_path} has no parsable "
                f"transaction-cycle row — did cosim fail?"
            )

        cosim_timing = {
            "transaction_cycles": int(cycles),
            "report_path": str(parser.report_path),
            "vitis_version": _detect_vitis_version(parser.report_path),
            "source": "cosim",
            "top": self.top,
        }

        root_dir = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out_path = root_dir / self.output_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(cosim_timing, indent=2), encoding="utf-8")
        return {self.cosim_timing_artifact: out_path}


def _detect_vitis_version(report_path: Path) -> str | None:
    """Best-effort version detection.

    Heuristic: the structured ``<top>_cosim.rpt`` is the 2025.1+ shape.
    The legacy ``cosim.log`` is pre-2025.  Returns ``None`` when neither
    pattern matches the path stem.
    """
    name = report_path.name
    if name.endswith("_cosim.rpt"):
        return "2025.1+"
    if name == "cosim.log":
        return "pre-2025"
    return None


@dataclass(kw_only=True)
class ValidateTimingStep(BuildStep):
    """Compare Python-side and cosim-side transaction cycle counts.

    This is the *real* timing validator — it closes the cycle-approximate-
    Python loop by asserting that the SimPy model agrees with the
    measured RTL behaviour within a tolerance:

    .. code-block:: python

        abs(py_cycles - cosim_cycles) <= tolerance_cycles

    Produces a structured ``timing_verdict`` artifact regardless of
    pass/fail so downstream consumers (including the future model-
    training workflow per [[project-cycle-model-training]]) can read
    both raw numbers, not just a pass bit:

    .. code-block:: json

        {
            "pass": true,
            "py_cycles": 110,
            "cosim_cycles": 113,
            "delta": 3,
            "tolerance": 20,
            "py_timing_path": "...",
            "cosim_timing_path": "..."
        }

    On failure ``run()`` writes the verdict, then raises
    :class:`RuntimeError` with a one-line explanation so the build halts
    with a clear message.
    """

    description: str = (
        "Compare Python-side vs cosim-side transaction cycle counts and"
        " enforce a tolerance bound."
    )
    params: ClassVar[dict] = {}

    py_timing_artifact: str = "py_timing"
    cosim_timing_artifact: str = "cosim_timing"
    tolerance_cycles: int = 20
    output_path: str = "results/timing_verdict.json"
    # The artifact the verdict is published under.  A DAG holding two of these steps needs a
    # distinct name per step, since BuildDag refuses two producers of one artifact.
    verdict_artifact: str = "timing_verdict"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.py_timing_artifact, self.cosim_timing_artifact]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.verdict_artifact: Path(self.output_path)}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        py_path = Path(artifacts[self.py_timing_artifact])
        cosim_path = Path(artifacts[self.cosim_timing_artifact])
        py = json.loads(py_path.read_text(encoding="utf-8"))
        cs = json.loads(cosim_path.read_text(encoding="utf-8"))
        py_cycles = int(py["transaction_cycles"])
        cosim_cycles = int(cs["transaction_cycles"])
        delta = abs(py_cycles - cosim_cycles)
        passed = delta <= self.tolerance_cycles

        verdict = {
            "pass": passed,
            "py_cycles": py_cycles,
            "cosim_cycles": cosim_cycles,
            "delta": delta,
            "tolerance": int(self.tolerance_cycles),
            "py_timing_path": str(py_path),
            "cosim_timing_path": str(cosim_path),
        }

        root_dir = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out_path = root_dir / self.output_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(verdict, indent=2), encoding="utf-8")

        if not passed:
            raise RuntimeError(
                f"ValidateTimingStep: delta={delta} cycles "
                f"(py={py_cycles}, cosim={cosim_cycles}) exceeds tolerance="
                f"{self.tolerance_cycles}.  Verdict at {out_path}."
            )
        return {self.verdict_artifact: out_path}
