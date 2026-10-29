"""build.py -- the system's devices in C++: generated headers, HLS tops, csynth (plan step 5.1).

``plans/ltbf_dse/ltbf_dse.md`` Phase 5.  The four HLS tops of the system (spec §5.1), each a
free-running composite whose leaves' bodies are hand-written (``src/ltbf_tasks.h``,
``src/ltbf_ingest_task.h``) or the framework's (the systolic engine, the vector unit, the in-band
memory streams):

* ``ltbf_sys``    -- :class:`~examples.ltbf_dse.hw.sys_wrap.SysWrap` (``SystolicEngineMem`` + tap + Ctrl),
  built with ``config_compile -pipeline_style flp`` (Vitis 2024.1 crashes on the engine's free-running
  pipeline, plan §14);
* ``ltbf_vec``    -- :class:`~examples.ltbf_dse.hw.vec_wrap.VecWrap` (the vector unit, one build for both
  programs, and its memory front end);
* ``ltbf_ingest`` -- :class:`~examples.ltbf_dse.hw.ingest.IngestDev`;
* ``ltbf_hub``    -- :class:`~examples.ltbf_dse.hw.resp_hub.RespHub`, three sources.

Their parameters are AC9's (``system.SysParams()``: N_rx 64, ``c`` 8, ``Mmax`` 16, ``J`` 9, the formats
profiled on drop 1).  ``src/`` is the only C++ source; ``include/`` and ``gen/`` (each top's ``.tcl``
beside its ``.cpp``) are build output, untracked (``plans/source_layout.md``).

    python -m examples.ltbf_dse.hw.build --through csynth     # codegen, then csynth of every top
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.composite_gen import (
    GEN_DIR,
    INCLUDE_DIR,
    composite_top_spec,
    render_tcl,
    render_top,
    tcl_path,
)
from waveflow.build.streamutils import MemMgrStep, MemStreamStep
from waveflow.build.system_dag import CsynthStep
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.hw.mem_stream import MemRCmd, MemWCmd
from waveflow.linalg.build import collect_parts, linalg_headers_dag
from waveflow.simulation.simulation import Simulation
from waveflow.vecunit.build import PART, PERIOD_NS, VecUnitHeaderStep
from waveflow.vecunit.hls import CORE_HEADER, DEFINES, io_traits
from waveflow.vecunit.isa import SCHEMAS as VEC_SCHEMAS

ROOT = Path(__file__).resolve().parents[1]
#: Vitis HLS 2024.1 and the systolic engine (spike 1.2 b, plan §14).
FLP = ("config_compile -pipeline_style flp",)
TOPS = ("ltbf_sys", "ltbf_vec", "ltbf_ingest", "ltbf_hub", "ltbf_hub1")
#: The device gates' systems (``devsys``) and their workspaces under ``xsi_work/``.
DEVICES = ("sys", "vec", "ingest")
WORK_DIR = "xsi_work"


def devices(sim: Simulation | None = None) -> dict[str, Any]:
    """The four devices at AC9's parameters, named as their tops."""
    from examples.ltbf_dse.hw import system as sy
    from examples.ltbf_dse.hw.devsys import RespHub1
    from examples.ltbf_dse.hw.ingest import IngestDev
    from examples.ltbf_dse.hw.resp_hub import RespHub
    from examples.ltbf_dse.hw.sys_wrap import SysWrap
    from examples.ltbf_dse.hw.vec_wrap import VecWrap

    sim = sim or Simulation()
    inp = sy.inputs(sy.SysParams())
    S = sy.LtbfSystem(inp)
    p, d, cfg = S.p, S.d, S.cfg
    W = p.word_bits
    return {
        "ltbf_sys": SysWrap(name="ltbf_sys", sim=sim, engine_cfg=sy.engine_cfg(S)),
        "ltbf_vec": VecWrap(name="ltbf_vec", sim=sim, vu_cfg=S.vu, word_bits=W),
        "ltbf_ingest": IngestDev(name="ltbf_ingest", sim=sim, dma_cfg=sy.ingest_cfg(S)),
        "ltbf_hub": RespHub(name="ltbf_hub", sim=sim, n_src=3, word_bits=W),
        "ltbf_hub1": RespHub1(name="ltbf_hub1", sim=sim, word_bits=W),
        "_formats": (cfg.a, cfg.bc),
        "_vu": S.vu,
        "_word_bits": W,
        "_d": d,
    }


def own_schemas() -> tuple[type, ...]:
    from examples.ltbf_dse.hw import records as rec
    from examples.ltbf_dse.hw.vec_wrap import StorePlan, VecMemJob

    return (rec.Resp, rec.SysCfg, rec.SysStatus, rec.IngestCfg, rec.IngestStatus, VecMemJob,
            StorePlan)  # fmt: skip


def gen_headers(root: Path, devs: dict) -> None:
    """Every header the four tops include, into ``include/``."""
    W = int(devs["_word_bits"])
    words = sorted({32, 64, W})
    eng = collect_parts(devs["ltbf_sys"])
    vu = devs["_vu"]
    schemas = tuple(dict.fromkeys(s for s in (*eng.schemas, *VEC_SCHEMAS, *own_schemas())
                                  if s not in (MemRCmd, MemWCmd)))  # fmt: skip
    dag = linalg_headers_dag((*eng.traits, io_traits(vu)), (*eng.bodies, CORE_HEADER), INCLUDE_DIR,
                             words, schemas)  # fmt: skip
    dag.add(VecUnitHeaderStep([vu], INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
    dag.add(MemStreamStep(output_dir=INCLUDE_DIR))
    for cls in (MemRCmd, MemWCmd):
        dag.add(
            DataSchemaStep(
                cls, word_bw_supported=words, include_dir=INCLUDE_DIR, framed=True
            )
        )
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = {k: r.message for k, r in res.items() if not r.success}
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")


def gen_top(root: Path, mod, W: int, **tcl) -> str:
    spec = composite_top_spec(mod, width=W)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    tcl_path(root, spec.top_name).write_text(
        render_tcl(spec.top_name, part=PART, period_ns=PERIOD_NS, **tcl),
        encoding="utf-8",
    )
    return spec.top_name


def generate(root: Path = ROOT) -> list[str]:
    """Headers, the four tops and their ``.tcl``: Python only, seconds."""
    devs = devices()
    gen_headers(root, devs)
    W = int(devs["_word_bits"])
    return [
        gen_top(root, devs["ltbf_sys"], W, solution_config=FLP),
        gen_top(root, devs["ltbf_vec"], W, defines=DEFINES),
        gen_top(root, devs["ltbf_ingest"], W),
        gen_top(root, devs["ltbf_hub"], W),
        gen_top(root, devs["ltbf_hub1"], W),
    ]


@dataclass(kw_only=True)
class LtbfCodegenStep(BuildStep):
    """``codegen``: :func:`generate`.  Never fresh: its inputs are Python the DAG cannot see, and
    csynth after it decides by content."""

    description = "Generate the headers, the four device tops and their .tcl."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        generate(root)
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


def build_dag() -> BuildDag:
    """``codegen``, then a csynth a top."""
    dag = BuildDag()
    dag.add(LtbfCodegenStep(name="codegen"))
    for top in TOPS:
        dag.add(CsynthStep(top=top))
    return dag


def device_dag(names=DEVICES, work_dir=WORK_DIR, trace: bool = False,
               trace_bus: bool = False) -> BuildDag:
    """``codegen``, then the framework's system flow for each device's gate system
    (``devsys.<name>_system``): csynth of its tops, the system RTL, the scenario, pysim, XSI with the
    host's C++ twin, and ``compare`` (RTL against pysim), each prefixed ``<name>_``."""
    from examples.ltbf_dse.hw import devsys
    from waveflow.build.system_dag import add_system_steps

    dag = BuildDag()
    dag.add(LtbfCodegenStep(name="codegen"))
    for name in names:
        sysm = getattr(devsys, f"{name}_system")()
        add_system_steps(dag, sysm, work_dir=work_dir, top=f"ltbf_{name}_top", prefix=f"{name}_",
                         workspace=f"ltbf_{name}", trace=trace, trace_bus=trace_bus)  # fmt: skip
    return dag


def system_dag(work_dir=WORK_DIR, trace: bool = False, trace_bus: bool = False) -> BuildDag:
    """``codegen``, then the framework's system flow for AC9's system (step 5.2): ``system.build`` at
    :data:`~examples.ltbf_dse.hw.system.AC9` for the RTL flow -- csynth of its tops, the system RTL,
    the scenario (the C++ host's, from a reference pysim run), pysim, and XSI with ``hw/ltbf_host.h``,
    each prefixed ``ltbf_``; before pysim, Shared Mem measured in its own harness (``umemcal_``) and
    its timing filed in ``calib/``.  The Resp FIFO's order is timing's, so the gate (the test) compares what
    must agree word for word rather than the flow's ``ltbf_compare`` (plan §9 e)."""
    from examples.ltbf_dse.hw import system as sy
    from waveflow.build.phase_calib import MemCalibStep
    from waveflow.build.system_dag import add_system_steps
    from waveflow.hw.clock import Clock
    from waveflow.hw.harness import memory_harness

    dag = BuildDag()
    dag.add(LtbfCodegenStep(name="codegen"))
    hw, S, _host = sy.build(sy.inputs(sy.AC9), rtl=True)
    add_system_steps(dag, hw, work_dir=work_dir, top="ltbf_top", prefix="ltbf_", workspace="ltbf",
                     trace=trace, trace_bus=trace_bus)  # fmt: skip
    # Shared Mem alone first (plans/unit_harness.md Stage 4c): its RTL reads cost a cycle more a word
    # than pysim's plain memory, so the system's pysim loads the timing measured here.
    clk = Clock(freq=sy.F_HOST)
    uncal = memory_harness(lambda sim: sy.shared_mem(sim, S.mem_span, clk, calibrated=False),
                           sy.mem_script("calib", S.mem_span), n_cycles=40_000)  # fmt: skip
    xsi = add_system_steps(dag, uncal, work_dir=work_dir, top="ltbf_mem_unit", prefix="umemcal_")
    mem_cal = MemCalibStep(sysm=uncal, work=xsi.work, prefix="umemcal_", store=sy.CALIB_DIR)
    dag.add(mem_cal)
    dag.require("ltbf_pysim", [mem_cal.a("mem_timing")])
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0], default_through=None,
                root_dir=ROOT,
                extra_args=[(("--synth",), {"choices": ("build", "check"), "default": "build"})],
                params_from_args=lambda a: {"synth": a.synth})  # fmt: skip
