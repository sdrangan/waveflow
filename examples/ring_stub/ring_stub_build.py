"""ring_stub_build.py -- the ring stub's build DAG (``plans/shared_kernel.md`` Stage 1).

    python -m examples.ring_stub.ring_stub_build --through pysim          # pysim only
    python -m examples.ring_stub.ring_stub_build --through codegen        # + the headers and the tops
    python -m examples.ring_stub.ring_stub_build                          # the composite through compare
    python -m examples.ring_stub.ring_stub_build --through inc_compare    # K alone, at RTL
    python -m examples.ring_stub.ring_stub_build --through post_compare   # Post alone, at RTL
    python -m examples.ring_stub.ring_stub_build --through long_compare   # the composite, 40 jobs

The three bodies are hand-written (``src/``); everything else is generated: the schemas' C++ (framed, for
the internal FIFOs), the UInt64 lane routines, and three free-running tops -- the composite (with its
cycle), K alone and Post alone.  Mux has no harness of its own: alone, the order it interleaves its two
inputs depends on when the stand-ins deliver them, so its recordings would not be comparable; it is
verified inside the composite, whose output order does not depend on timing.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from examples.ring_stub.ring_stub import SCHEMA_CLASSES, Inc, Post, RingStub, UInt64, W
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
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.hw.harness import harness
from waveflow.simulation.simulation import Simulation

HERE = Path(__file__).resolve().parent
WORK_DIR = "xsi_work"
#: The tops: the composite, and K and Post alone.
TOP, INC_TOP, POST_TOP = "ring_stub_top", "ring_inc_top", "ring_post_top"


def gen_headers(root: Path = HERE) -> None:
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
    for cls in SCHEMA_CLASSES:
        dag.add(DataSchemaStep(cls, word_bw_supported=[W], include_dir=INCLUDE_DIR, framed=True))
    dag.add(ArrayUtilsStep(UInt64, [W]))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")


def gen_top(cls, root: Path = HERE) -> str:
    mod = cls(name=cls.cpp_kernel_name, sim=Simulation())
    spec = composite_top_spec(mod, width=W)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    tcl_path(root, spec.top_name).write_text(render_tcl(spec.top_name), encoding="utf-8")
    return spec.top_name


def generate(root: Path = HERE) -> list[str]:
    """Everything before csynth: the headers, and the three tops with their ``.tcl``."""
    gen_headers(root)
    return [gen_top(c, root) for c in (RingStub, Inc, Post)]


@dataclass(kw_only=True)
class RingStubCodegenStep(BuildStep):
    """``codegen``: :func:`generate`.  Never fresh; csynth decides by content."""

    description = "Generate the headers, the lane routines and the three tops."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        generate(root)
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """``codegen``; the composite's harness (no prefix), on the long scenario too (``long_``); K's and
    Post's harnesses (``inc_``, ``post_``)."""
    dag = BuildDag()
    dag.add(RingStubCodegenStep(name="codegen"))
    add_system_steps(dag, harness(RingStub, workload="test", n_cycles=20000), work_dir=work_dir, top=TOP)
    add_system_steps(dag, harness(RingStub, workload="long", n_cycles=60000), work_dir=work_dir, top=TOP,
                     prefix="long_")
    add_system_steps(dag, harness(Inc, workload="test"), work_dir=work_dir, top=INC_TOP, prefix="inc_")
    add_system_steps(dag, harness(Post, workload="test"), work_dir=work_dir, top=POST_TOP, prefix="post_")
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import PLATFORM_ARGS, platform_config, run_dag_cli
    from waveflow.build.composite_gen import DEFAULT_PART, DEFAULT_PERIOD_NS

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0], default_through="compare",
                root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead")),
                            *PLATFORM_ARGS],
                params_from_args=lambda a: {"synth": a.synth},
                config_from_args=lambda a: platform_config(a, part=DEFAULT_PART,
                                                           clk_freq=1e9 / DEFAULT_PERIOD_NS))
