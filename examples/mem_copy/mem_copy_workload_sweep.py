"""mem_copy_workload_sweep.py — fifty job lengths at RTL and in pysim, and what each point costs.

``plans/incremental_xsi.md`` Stage 4.  Every axis is a **workload** axis: the job length is a runtime
field of the copy command, so the hardware -- and with it the elaborated XSI snapshot and the
compiled testbench -- is the same at every point.  What changes per point is only the scenario under
``xsi/vectors/``: the command stream, the arena image, and ``run.json`` (the arena's size and the
cycle bound, which the generated testbench reads at run time).  So after the first point an RTL
point costs what it simulates, not what it takes to build a simulator.

    python -m examples.mem_copy.mem_copy_workload_sweep              # 50 points: n_words 16..800
    python -m examples.mem_copy.mem_copy_workload_sweep --n-words 64 128

Each point runs the pysim (``pysim``) and the RTL (``rtlsim``, traced, through
:class:`~waveflow.build.trace_steps.RtlSimStep` and so :class:`~waveflow.build.xsi_snapshot.XsiSnapshot`),
then :class:`RtlCheckStep` checks the RTL bit-exact and records both completion numbers in
``results/workload_sweep.jsonl``.  The per-point cost of each comes from the timing events
(:func:`waveflow.events.analyze_events`), printed at the end.

No platform: nothing here is filed into a calibration library.  ``mem_copy_sweep.py`` is the
calibration sweep; this one measures the cost of evaluating a workload at each fidelity.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent

from waveflow import events  # noqa: E402
from waveflow.build.build import BuildConfig, BuildStep  # noqa: E402
from waveflow.build.sweep import ParamGrid, Stage, SweepRunner, sweep_cli  # noqa: E402

from examples.mem_copy.mem_copy import (  # noqa: E402
    XSI_N, XSI_NUM_CMDS, check_mem_copy_xsi_outputs, xsi_jobs,
)
from examples.mem_copy.mem_copy_build import build_mem_copy_dag  # noqa: E402

#: Fifty job lengths, 16 words apart.
N_WORDS = tuple(range(16, 16 * 51, 16))

#: Jobs per point, the same at every size (as in mem_copy_sweep.py).
NUM_CMDS = 4

RESULTS = HERE / "results" / "workload_sweep.jsonl"


@dataclass(kw_only=True)
class RtlCheckStep(BuildStep):
    """Check this point's RTL run bit-exact and record its completion cycle beside the pysim's.

    ``RtlSimStep`` asserts nothing by design; a sweep that reported fifty cycle counts without
    checking the copies would be measuring runs nobody knew were correct.
    """

    description = "Check the RTL run of this workload point; record RTL and pysim cycles."
    consumes = ["trace_vcd", "pysim_results"]
    produces = {"rtl_check": Path("results/rtl_check.json")}
    params = {"n_words": XSI_N, "num_cmds": XSI_NUM_CMDS}

    def run(self, config: BuildConfig, n_words, num_cmds, trace_vcd, pysim_results, **_) -> dict:
        n, k = int(n_words), int(num_cmds)
        rtl = check_mem_copy_xsi_outputs(Path(config.root_dir) / "xsi", None, jobs=xsi_jobs(n, k))
        pysim = json.loads(Path(pysim_results).read_text(encoding="utf-8"))
        row = {"n_words": n, "num_cmds": k, "rtl_done_cycle": rtl,
               "pysim_end_cycles": pysim["end_cycles"]}
        out = Path(config.root_dir) / "results" / "rtl_check.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(row, indent=1) + "\n", encoding="utf-8")
        with RESULTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return {"rtl_check": out}


def build_workload_dag():
    dag = build_mem_copy_dag()
    dag.add(RtlCheckStep(name="rtl_check"))
    return dag


GRID = ParamGrid(n_words=N_WORDS, _workload=("n_words",))

RUNNER = SweepRunner(dag_factory=build_workload_dag, root_dir=HERE,
                     summary=HERE / "results" / "workload_sweep.json",
                     extra_params={"live_output": False, "num_cmds": NUM_CMDS})

#: One pass per point.  ``codegen_tb`` is forced so a point that changed the testbench would be
#: caught regenerating it -- it does not: the text is identical, so the snapshot's stamp matches and
#: g++ does not run.  ``codegen_dut`` and ``csynth`` are absent: the DUT is scenario-independent.
STAGES = [Stage("rtl_check", name="point", use_platform=False,
                force=["pysim", "codegen_tb", "rtlsim", "rtl_check"])]

DRY_RUN_STAGES = [Stage("pysim", use_platform=False, force=["pysim"])]


def per_point_costs(spans: list[dict]) -> dict[str, list[float]]:
    """Seconds per point of each step that ran, from the sweep's own timing spans."""
    out: dict[str, list[float]] = {}
    for e in spans:
        if e.get("kind") == "step" and e.get("name") in ("pysim", "rtlsim", "codegen_tb"):
            out.setdefault(e["name"], []).append(float(e["elapsed"]))
        elif e.get("kind") == "phase":
            out.setdefault(f"xsi:{e['name']}", []).append(float(e["elapsed"]))
    return out


def format_costs(costs: dict[str, list[float]]) -> str:
    lines = [f"{'what':<18}{'count':>6}{'first s':>10}{'median s':>10}{'max s':>9}"]
    for name in sorted(costs):
        v = costs[name]
        lines.append(f"{name:<18}{len(v):>6}{v[0]:>10.2f}{statistics.median(v):>10.2f}{max(v):>9.2f}")
    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.unlink(missing_ok=True)
    with events.logging_to(HERE), events.collect() as spans:
        rc = sweep_cli(RUNNER, GRID, description="Sweep mem_copy job lengths at RTL and in pysim",
                       stages=STAGES, dry_run_stages=DRY_RUN_STAGES, argv=argv)
    print("\nPer-point cost (from the timing events):")
    print(format_costs(per_point_costs(spans)))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
