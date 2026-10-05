"""markov_build.py — generate and synthesize the two Markov kernels (``plans/mm_credit_stream.md``
Stage 3).

    python -m examples.markov.markov_build            # headers + tops + tcl, then csynth both
    python -m examples.markov.markov_build --no-synth # generate only
    python -m examples.markov.markov_build --figures  # the docs figure (golden model, no toolchain)

Each kernel's body is hand-written (``include/markov_gen_task.h``, ``include/markov_chain_core_task.h``
-- the HLS twins of ``MarkovGen.run_iter`` / ``ChainCore.run_iter``); everything around them is
generated: the command / response structs from :class:`MkvCmd` / :class:`MkvResp`, the lane routines
the bodies pack ``u`` and ``x`` with, the framework's in-band memory writer (copied, fixed), and each
free-running ``ap_ctrl_none`` top from the module's own ports.  The memory-mapped side -- the views,
the bus writers, the crossbar -- is RTL beside the kernels, wired in the XSI gate.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.composite_gen import GEN_DIR, INCLUDE_DIR, composite_top_spec, render_tcl, render_top
from waveflow.build.credit_hls import copy_credit_header
from waveflow.build.mm_writer_gen import write_writer_project
from waveflow.build.streamutils import MemMgrStep, MemStreamStep, StreamUtilsStep
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
    (root / f"{spec.top_name}.tcl").write_text(render_tcl(spec.top_name), encoding="utf-8")
    return cpp


def synth(top: str, root: Path = HERE) -> str:
    from waveflow.build.rtl_digest import write_stamp
    from waveflow.toolchain.toolchain import run_vitis_hls

    r = run_vitis_hls(root / f"{top}.tcl", work_dir=root)
    out = (r.stdout or "") + (r.stderr or "")
    if "WAVEFLOW_CSYNTH_OK" not in out:
        raise RuntimeError(f"csynth of {top} failed:\n{out[-6000:]}")
    write_stamp(root, top)
    return out


def figures(root: Path = HERE) -> None:
    """Render the docs figure from the golden model and promote it into docs/ (two DAG steps)."""
    from examples.markov.markov_figures import MarkovFiguresStep, SyncDocsFiguresStep

    dag = BuildDag()
    dag.add(MarkovFiguresStep(name="markov_figures"))
    dag.add(SyncDocsFiguresStep(name="sync_docs_figures"))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"figure generation failed: {bad}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-synth", action="store_true")
    ap.add_argument("--only", help="csynth just this top")
    ap.add_argument("--figures", action="store_true",
                    help="only render the docs figure (golden model; no toolchain) and sync it")
    a = ap.parse_args()
    if a.figures:
        figures()
        print("figures synced to docs/examples/markov/images/")
        return
    gen_headers()
    names = []
    for cls in TOPS:
        print("generated", gen_top(cls).relative_to(HERE))
        names.append(cls.cpp_kernel_name)
    for mode, maxp in WRITERS:
        names.append(write_writer_project(HERE, mode, DW, maxp))
        print("generated", f"gen/{names[-1]}.cpp")
    if a.no_synth:
        return
    for top in names:
        if a.only and top != a.only:
            continue
        synth(top)
        print(f"csynth {top} OK")


if __name__ == "__main__":
    main()
