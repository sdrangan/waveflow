"""markov_build.py — the Markov example's build DAG: generate and synthesize the two kernels and the
two bus writers (``plans/mm_credit_stream.md`` Stage 3, ``plans/system_dag.md``).

    python -m examples.markov.markov_build                               # codegen, then csynth what is stale
    python -m examples.markov.markov_build --through codegen             # generate only
    python -m examples.markov.markov_build --status                      # what is stale, and why
    python -m examples.markov.markov_build --through sync_docs_figures   # the docs figure (no toolchain)

Each kernel's body is hand-written (``src/markov_gen_task.h``, ``src/markov_chain_core_task.h``
-- the HLS twins of ``MarkovGen.run_iter`` / ``ChainCore.run_iter``); everything around them is
generated: the command / response structs from :class:`MkvCmd` / :class:`MkvResp`, the lane routines
the bodies pack ``u`` and ``x`` with, the framework's in-band memory writer (copied, fixed), and each
free-running ``ap_ctrl_none`` top from the module's own ports.  The memory-mapped side -- the views,
the bus writers, the crossbar -- is RTL beside the kernels, wired in the XSI gate.

``codegen`` is this example's step; ``csynth`` is the framework's
(:class:`~waveflow.build.system_dag.CsynthTopsStep`): one inner step per top, each re-run only when the
sources its stamp recorded changed, so a ``codegen`` that rewrites identical bytes re-synthesizes
nothing.

``src/`` is the only C++ source here.  ``include/`` and ``gen/`` (with each top's ``.tcl``) are
build output, untracked: delete them and :func:`generate` writes them back (``plans/source_layout.md``).
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
from waveflow.build.credit_hls import copy_credit_header
from waveflow.build.mm_writer_gen import write_writer_project
from waveflow.build.mm_writer_gen import writer_top_name
from waveflow.build.streamutils import MemMgrStep, MemStreamStep, StreamUtilsStep
from waveflow.build.system_dag import CsynthTopsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.hw.mem_stream import MemWCmd
from waveflow.simulation.simulation import Simulation

from examples.markov.markov import DW, QDEPTH, U8, U16, MarkovChain, MarkovGen, MkvCmd, MkvResp

HERE = Path(__file__).resolve().parent
TOPS = (MarkovGen, MarkovChain)
#: The routed link's two bus writers: (mode, maxp).  Framework tops (mm_writer_gen), built here.
WRITERS = (("queue", QDEPTH), ("credit", None))


def gen_headers(root: Path = HERE) -> None:
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))       # the generated tops include memmgr.hpp
    dag.add(MemStreamStep(output_dir=INCLUDE_DIR))    # the fixed in-band writer body
    dag.add(DataSchemaStep(MkvCmd, word_bw_supported=[DW], include_dir=INCLUDE_DIR))
    # MemWCmd and MkvResp ride the chain's framed internal edge (to the in-band memory writer), so
    # their headers need the framed_word methods.
    for cls in (MemWCmd, MkvResp):
        dag.add(DataSchemaStep(cls, word_bw_supported=[DW], include_dir=INCLUDE_DIR, framed=True))
    for elem in (U16, U8):                            # u: four to a word; x: eight
        dag.add(ArrayUtilsStep(elem, [DW]))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")
    # The framework's credit-stream helpers both bodies use (credit::Producer / credit::Consumer).
    copy_credit_header(root / INCLUDE_DIR)


def gen_top(cls, root: Path = HERE) -> Path:
    mod = cls(name=cls.cpp_kernel_name, sim=Simulation())
    spec = composite_top_spec(mod, width=DW)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    tcl_path(root, spec.top_name).write_text(render_tcl(spec.top_name), encoding="utf-8")
    return cpp


def generate(root: Path = HERE) -> list[str]:
    """Everything before csynth -- headers, the two kernel tops, the two bus-writer tops, each with
    its ``.tcl`` -- and the names of the four tops.  Python only, seconds, no toolchain.

    The XSI gate calls this before it asks whether the RTL is stale: ``include/`` and ``gen/`` are
    untracked copies, so only regenerating them makes a pulled change to a framework header, a schema
    or a generator show up as a changed source (``trace_steps.rtl_staleness``)."""
    gen_headers(root)
    names = [gen_top(cls, root).stem for cls in TOPS]
    names += [write_writer_project(root, mode, DW, maxp) for mode, maxp in WRITERS]
    return names


def top_names() -> list[str]:
    """The four HLS tops :func:`generate` writes, in its order -- named as it names them."""
    return ([cls.cpp_kernel_name for cls in TOPS]
            + [writer_top_name(mode, DW, maxp) for mode, maxp in WRITERS])


@dataclass(kw_only=True)
class MarkovCodegenStep(BuildStep):
    """``codegen``: :func:`generate` -- headers, the four tops, their ``.tcl``.  Python only, seconds.

    Never fresh (:meth:`is_fresh`): its inputs are Python and framework headers the DAG cannot see, and
    the rewrite is cheap.  The csynth after it decides by content, so identical bytes re-run nothing."""

    description = "Generate the headers, the two kernel tops and the two bus-writer tops, with their .tcl."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        generate(root)
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


def build_dag() -> BuildDag:
    """``codegen`` -> ``csynth`` (one inner step per top), and the docs figure beside them."""
    from examples.markov.markov_figures import MarkovFiguresStep, SyncDocsFiguresStep

    dag = BuildDag()
    dag.add(MarkovCodegenStep(name="codegen"))
    dag.add(CsynthTopsStep(name="csynth", tops=top_names()))
    dag.add(MarkovFiguresStep(name="markov_figures"))
    dag.add(SyncDocsFiguresStep(name="sync_docs_figures"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_dag, description=__doc__.splitlines()[0], default_through="csynth",
                root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
