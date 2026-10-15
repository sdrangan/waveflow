"""stream_kernel_build.py — the free-running poly's build DAG (``plans/unit_harness.md`` Stage 1).

    python -m examples.stream_kernel.stream_kernel_build                  # everything, through compare
    python -m examples.stream_kernel.stream_kernel_build --through pysim  # pysim only (no Vivado)
    python -m examples.stream_kernel.stream_kernel_build --through csynth # build the RTL, stop there
    python -m examples.stream_kernel.stream_kernel_build --trace          # the XSI run with its ports traced
    python -m examples.stream_kernel.stream_kernel_build --through calib_phases   # calibrate the phases

The kernel's body is hand-written (``src/poly_kernel_task.h``, the HLS twin of ``PolyKernel.run_iter``);
everything around it is generated: the command and response structs from :class:`CmdHdr` /
:class:`Resp`, the float32 lane routines the body packs samples with, and the free-running
``ap_ctrl_none`` top from the module's own ports.  After ``codegen`` every step is the framework's
(:func:`~waveflow.build.system_dag.add_system_steps`): ``csynth``, ``system_rtl`` (no crossbar: the
kernel's three streams are the top's ports), ``scenario`` / ``pysim``, ``system_xsi`` (the kernel at
RTL under XSI, a stream driver and two sinks around it) and ``compare`` (the sinks' recordings, RTL
against pysim).

``calib_phases`` calibrates the kernel's timed phases (:mod:`waveflow.build.phase_calib`): the
calibration workload (the kernel's ``workload("calib")`` and ``calib_holdout``) as a second, traced system sharing
``codegen`` and ``csynth``, then the fit, filed in ``calib/``.  Nothing here is calibration code.

``src/`` is the only C++ source here; ``include/`` and ``gen/`` are build output, untracked.
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
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep
from waveflow.build.phase_calib import add_calib_steps
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.simulation.simulation import Simulation

from waveflow.hw.harness import harness
from waveflow.hw.hw_system import HwSystem

from examples.stream_kernel.stream_kernel import (
    SCHEMA_CLASSES,
    W,
    Float32,
    PolyKernel,
)

HERE = Path(__file__).resolve().parent
#: The RTL run's top and where it runs (``<work_dir>/<top>``).
TOP, WORK_DIR = "stream_kernel_top", "xsi_work"


def system() -> HwSystem:
    """The system the DAG runs, in pysim and at RTL: the kernel's harness on its test workload --
    generated from its boundary, no testbench code here."""
    return harness(PolyKernel, workload="test")


def gen_headers(root: Path = HERE) -> None:
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))          # the generated top includes memmgr.hpp
    for cls in SCHEMA_CLASSES:
        dag.add(DataSchemaStep(cls, word_bw_supported=[W], include_dir=INCLUDE_DIR))
    dag.add(ArrayUtilsStep(Float32, [W]))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")


def gen_top(root: Path = HERE) -> Path:
    mod = PolyKernel(name=PolyKernel.cpp_kernel_name, sim=Simulation())
    spec = composite_top_spec(mod, width=W)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    tcl_path(root, spec.top_name).write_text(render_tcl(spec.top_name), encoding="utf-8")
    return cpp


def generate(root: Path = HERE) -> str:
    """Everything before csynth -- headers, the kernel's top and its ``.tcl``; the top's name.
    Python only, seconds."""
    gen_headers(root)
    return gen_top(root).stem


@dataclass(kw_only=True)
class StreamKernelCodegenStep(BuildStep):
    """``codegen``: :func:`generate`.  Never fresh: its inputs are Python and framework headers the DAG
    cannot see, and the rewrite is cheap -- csynth decides by content, so identical bytes re-run
    nothing."""

    description = "Generate the headers, the lane routines and the kernel's free-running top."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        generate(root)
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


def build_dag(work_dir=WORK_DIR, trace: bool = False, sysm: HwSystem | None = None) -> BuildDag:
    """``codegen``, the framework's system steps for *sysm* (default :func:`system`), and its
    calibration (``calib_*``, run only when asked for).  *trace* dumps the kernel's ports in the XSI
    run (``<work>/<top>_trace.vcd``)."""
    dag = BuildDag()
    dag.add(StreamKernelCodegenStep(name="codegen"))
    sysm = sysm if sysm is not None else system()
    add_system_steps(dag, sysm, work_dir=work_dir, top=TOP, trace=trace)
    add_calib_steps(dag, sysm, work_dir=work_dir, top=TOP)
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import PLATFORM_ARGS, platform_config, run_dag_cli
    from waveflow.build.composite_gen import DEFAULT_PART, DEFAULT_PERIOD_NS

    run_dag_cli(lambda a: build_dag(trace=a.trace), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead")),
                            (("--trace",), dict(action="store_true",
                             help="trace the kernel's ports in the XSI run")),
                            *PLATFORM_ARGS],
                params_from_args=lambda a: {"synth": a.synth},
                config_from_args=lambda a: platform_config(a, part=DEFAULT_PART,
                                                           clk_freq=1e9 / DEFAULT_PERIOD_NS))
