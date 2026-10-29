"""Spike 1.2 b (plans/ltbf_dse/ltbf_dse.md): ``SystolicEngineMem`` at LTBF shapes.

csynth (Vitis HLS 2024.1) of the memory-backed systolic engine sized for an inner dimension of
K = 1024 (``Pmax · Kmax = 1024``), the centre otherwise, 16-bit operands; its resources, estimated
clock, csynth wall time and peak memory go to ``hw/build/spike_b/<name>/result.json`` (local).

    python -m examples.ltbf_dse.spikes.spike_b_systolic --name k1024
"""

from __future__ import annotations

import argparse
import json
import resource
import time
from pathlib import Path

from examples.systolic import engine_mem_harness as EM
from examples.systolic.engine_harness import PART, PERIOD_NS, build_config, reg
from examples.systolic.systolic_measure import _resources
from waveflow.build.build import BuildDag
from waveflow.build.composite_gen import composite_top_spec, render_tcl, tcl_path
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.harness import harness

HERE = Path(__file__).resolve().parent
#: Vitis HLS 2024.1 segfaults when it makes the systolic core's STEP pipeline free-running; the
#: flushable style avoids it with the engine unchanged (user, 2026-10-28; lessons file).
FLP = ("config_compile -pipeline_style flp",)


class FlpCodegenStep(EM.EngineMemCodegenStep):
    """The harness's codegen, its csynth script with :data:`FLP`."""

    def run(self, config, **kw):
        out = super().run(config, **kw)
        spec = composite_top_spec(
            EM.engine_mem(**self.cfg), width=int(self.cfg.get("word_bits", 64))
        )
        tcl_path(Path(config.root_dir), spec.top_name).write_text(
            render_tcl(
                spec.top_name, part=PART, period_ns=PERIOD_NS, solution_config=FLP
            ),
            encoding="utf-8",
        )
        return out


def build_dag(cfg: dict) -> BuildDag:
    """:func:`examples.systolic.engine_mem_harness.build_dag` with :class:`FlpCodegenStep`."""
    sysm = harness(EM.engine_mem(**cfg), workload="test", n_cycles=400_000)
    dag = BuildDag()
    dag.add(FlpCodegenStep(name="codegen", cfg=cfg))
    add_system_steps(dag, sysm, work_dir=EM.WORK_DIR, top=EM.TOP)
    return dag


OUT = HERE.parent / "hw" / "build" / "spike_b"

#: name -> the parameters that differ from the harness's centre.
POINTS = {
    "centre": {},
    "centre_w16_p128": {"Pmax": 128, "a": reg(16, 4), "b": reg(16, 4), "c": reg(16, 4)},
    "p16": {"Pmax": 16},
    "k1024": {
        "Pmax": 128,
        "word_bits": 128,
        "a": reg(16, 4),
        "b": reg(16, 4),
        "c": reg(16, 4),
    },
    "k1024_k16": {
        "Kmax": 16,
        "Pmax": 64,
        "word_bits": 128,
        "a": reg(16, 4),
        "b": reg(16, 4),
        "c": reg(16, 4),
    },
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", choices=sorted(POINTS), required=True)
    a = ap.parse_args()
    root = OUT / f"{a.name}_flp"
    root.mkdir(parents=True, exist_ok=True)
    cfg = POINTS[a.name]
    t0 = time.time()
    res = build_dag(cfg).run(build_config(root), through="csynth")
    wall = time.time() - t0
    ok = all(r.success for r in res.values())
    out = {
        "name": a.name,
        "cfg": {k: (str(v) if not isinstance(v, int) else v) for k, v in cfg.items()},
        "ok": ok,
        "wall_s": round(wall, 1),
        "peak_rss_children_mb": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        // 1024,
        "steps": {
            k: (r.success, "" if r.success else r.message[:2000])
            for k, r in res.items()
        },
    }
    if ok:
        xmls = sorted(root.rglob("solution1/syn/report/csynth.xml"))
        out["resources"] = {x.parents[3].name: _resources(x.parents[3]) for x in xmls}
    (root / "result.json").write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str)[:3000])


if __name__ == "__main__":
    main()
