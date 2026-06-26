"""fir_build.py — generate + Vitis-validate the free-running streaming FIR kernel.

Stage A of ``plans/fir_freerun_integration.md``: the top is **generated** (the ``ap_ctrl_hs``
poly-style wrapper calling the ``fir_impl::pipeline`` hook), mirroring ``poly_build.py``:

  * ``gen-include``  — schema headers (FIRCmd/FIRResp/FirMeta + the enums) + the Float32
                       array-utils, into ``include/`` (root, resolved by the ``-I.`` cflag).
  * ``HlsCodegenStep(comp_class=FIRAccel, impl_dir=".")`` — ``gen/fir.{hpp,cpp}`` + the sticky
                       hand-written ``fir_pipeline_impl.tpp`` (the DATAFLOW hook).
  * ``pysim``        — the functional Stage-A sim, bit-exact vs the shared golden.

The RTL gates (csim + cosim of the generated top vs the golden, per-job period) are driven by
``run_fir.py`` (codegen once, then ``run.tcl`` per scenario).  The static ``fir_top.cpp`` /
``fir.hpp`` / ``fir_tb.cpp`` / per-row ``fir_dataflow.tpp`` are retired (the top is generated;
``fir_pipeline_impl.tpp`` is the hook; ``fir_tb.cpp`` is a thin hand-written TB of the gen top).

Run with the project venv::

    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_build.py --list-steps
    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_build.py --through pysim
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep  # noqa: E402
from waveflow.build.hwcodegen_steps import HlsCodegenStep  # noqa: E402
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep  # noqa: E402
from waveflow.hw.arrayutils import ArrayUtilsStep  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402

try:
    from examples.rowwise_fir.fir import FIRAccel, Float32, SCHEMA_CLASSES, WORD_BW_SUPPORTED
except ModuleNotFoundError:
    sys.path.insert(0, str(HERE))
    from fir import FIRAccel, Float32, SCHEMA_CLASSES, WORD_BW_SUPPORTED  # type: ignore[no-redef]

INCLUDE_DIR = "include"


@dataclass(kw_only=True)
class HlsGenIncludeStep(BuildStep):
    description = "Generate schema headers + Float32 array-utils for the Vitis flow."
    consumes = ["fir_source"]
    params = {}
    include_dir: str = INCLUDE_DIR

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"include_dir": Path(self.include_dir)}

    def run(self, config: BuildConfig, **_) -> dict:
        inner = BuildDag()
        inner.add(StreamUtilsStep(output_dir=self.include_dir))
        inner.add(MemMgrStep(output_dir=self.include_dir))   # memmgr.hpp (m_axi byte<->word)
        for cls in SCHEMA_CLASSES:
            inner.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED,
                                     include_dir=self.include_dir))
        inner.add(ArrayUtilsStep(Float32, WORD_BW_SUPPORTED))
        results = inner.run(config, force=True)
        failed = [n for n, r in results.items() if not r.success]
        if failed:
            raise RuntimeError(f"gen-include failed: {failed}")
        return {"include_dir": config.root_dir / self.include_dir}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    description = "Run the Stage-A functional sim and check Y bit-exact vs the shared golden."
    consumes = ["fir_source"]
    produces = {"pysim_summary": Path("results/pysim_summary.json")}
    params = {"seed": 0}

    def run(self, config: BuildConfig, seed, **_) -> dict[str, Any]:
        from examples.rowwise_fir.fir_sim import FIRSim, check_golden, make_specs
        specs = make_specs([(4, 64), (2, 48), (3, 32)], seed=seed)
        ok = check_golden(specs, FIRSim(specs).run())
        out = config.root_dir / "results" / "pysim_summary.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"bit_exact": bool(ok),
                                   "shapes": [[s.n_rows, s.n_cols] for s in specs]},
                                  indent=2) + "\n", encoding="utf-8")
        if not ok:
            raise RuntimeError("FIR pysim golden mismatch")
        return {"pysim_summary": out}


def generate() -> dict[str, Path]:
    """Run gen-include + HlsCodegenStep into the source tree (no Vitis).  Returns key paths."""
    dag = BuildDag()
    dag.add(SourceStep(artifact="fir_source", path=HERE / "fir.py"))
    dag.add(HlsGenIncludeStep(name="gen_include"))
    dag.add(HlsCodegenStep(name="gen_kernel", comp_class=FIRAccel,
                           source_artifact="fir_source", output_dir="gen", impl_dir="."))
    config = BuildConfig(root_dir=HERE, params={})
    results = dag.run(config, through="gen_kernel", force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"codegen failed: {failed}")
    return {"gen_dir": HERE / "gen", "include_dir": HERE / INCLUDE_DIR}


def build_fir_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="fir_source", path=HERE / "fir.py"))
    dag.add(HlsGenIncludeStep(name="gen_include"))
    dag.add(HlsCodegenStep(name="gen_kernel", comp_class=FIRAccel,
                           source_artifact="fir_source", output_dir="gen", impl_dir="."))
    dag.add(PySimStep(name="pysim"))
    return dag


def main() -> None:
    ap = argparse.ArgumentParser(description="Free-running FIR build (Stage A).")
    ap.add_argument("--through", default="pysim")
    ap.add_argument("--list-steps", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    dag = build_fir_dag()
    if args.list_steps:
        for n in dag.step_names():
            print(n)
        return
    config = BuildConfig(root_dir=HERE, params={"seed": args.seed})
    dag.run(config, through=args.through,
            on_step_begin=lambda s, will, p: print(f"{s.name}: {'RUN' if will else 'skip'}"),
            on_step_end=lambda s, r: print(f"  {'PASS' if r.success else 'FAIL: ' + str(r.message)}"))


if __name__ == "__main__":
    main()
