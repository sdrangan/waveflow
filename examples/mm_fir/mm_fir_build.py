"""mm_fir_build.py — generate and synthesize the mm_fir kernel (rung 3 of plans/mm_slave_adaptor.md).

    python -m examples.mm_fir.mm_fir_build            # headers + top + tcl, then csynth
    python -m examples.mm_fir.mm_fir_build --no-synth # generate only

The kernel's body is hand-written (``include/mm_fir_task.h``, the HLS twin of ``MmFir.run_iter``);
everything around it is generated: the config / status structs from :class:`FirCfg` /
:class:`FirStatus` (so neither side hand-packs a word), and the free-running ``ap_ctrl_none`` top from
the module's own ports via :func:`~waveflow.build.composite_gen.composite_top_spec`.  The memory-mapped
side is not in this top at all -- the adaptor is RTL beside it, wired in the XSI gate
(``tests/examples/test_mm_fir_xsi.py``).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.composite_gen import GEN_DIR, INCLUDE_DIR, composite_top_spec, render_tcl, render_top
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.simulation.simulation import Simulation

from examples.mm_fir.mm_fir import DW, S16, S64, FirCfg, FirCmdHdr, FirRespHdr, FirStatus, MmFir, Taps

HERE = Path(__file__).resolve().parent
TOP = "mm_fir"


def gen_headers(root: Path = HERE) -> None:
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))     # the generated top includes memmgr.hpp
    for cls in (Taps, FirCmdHdr, FirRespHdr, FirCfg, FirStatus):
        dag.add(DataSchemaStep(cls, word_bw_supported=[DW], include_dir=INCLUDE_DIR))
    # The lane routines the body packs samples and results with: int16 four to a word, int64 one.
    for elem in (S16, S64):
        dag.add(ArrayUtilsStep(elem, [DW]))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")


def gen_top(root: Path = HERE) -> Path:
    leaf = MmFir(name=TOP, sim=Simulation())
    spec = composite_top_spec(leaf, width=DW)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (root / f"{spec.top_name}.tcl").write_text(render_tcl(spec.top_name), encoding="utf-8")
    return cpp


def synth(root: Path = HERE) -> None:
    from waveflow.build.rtl_digest import write_stamp
    from waveflow.toolchain.toolchain import run_vitis_hls

    r = run_vitis_hls(root / f"{TOP}.tcl", work_dir=root)
    out = (r.stdout or "") + (r.stderr or "")
    if "WAVEFLOW_CSYNTH_OK" not in out:
        raise RuntimeError(f"csynth of {TOP} failed:\n{out[-4000:]}")
    write_stamp(root, TOP)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-synth", action="store_true")
    a = ap.parse_args()
    gen_headers()
    print("generated", gen_top().relative_to(HERE))
    if not a.no_synth:
        synth()
        print("csynth OK")


if __name__ == "__main__":
    main()
