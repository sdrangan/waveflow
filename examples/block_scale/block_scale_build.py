"""BuildDag for the block_scale example — Python golden vs Vitis, bit-exact.

The smallest *block* (load–compute–store) custom-hook flow: generate the m_axi
kernel + testbench from :class:`BlockScale` / :class:`BlockScaleTBHls`,
run the SimPy golden parity check, then Vitis C-sim and RTL co-simulation, each
checked bit-for-bit against the numpy golden ``block_affine``.

CLI::

    python block_scale_build.py --through gen      # generate include/ + gen/
    python block_scale_build.py --through py_sim    # + SimPy golden parity (no Vitis)
    python block_scale_build.py --through csim      # + Vitis C-sim vs golden (Vitis)
    python block_scale_build.py --through cosim     # + RTL co-sim vs golden (Vitis)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.build.hwgen import header_to_cpp, kernel_to_cpp, tb_files_to_str
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep
from waveflow.hw.arrayutils import (
    ArrayUtilsStep, read_uint32_file, write_uint32_file,
)
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.toolchain import toolchain

try:
    from examples.block_scale.block_scale import (
        A, B, DEFAULT_N, INCLUDE_DIR, BlockCmd, BlockScale, BlockScaleTBHls,
        Int32, SCHEMA_CLASSES, block_affine, run_sim,
    )
except ModuleNotFoundError:  # direct execution from the example dir
    from block_scale import (  # type: ignore[no-redef]
        A, B, DEFAULT_N, INCLUDE_DIR, BlockCmd, BlockScale, BlockScaleTBHls,
        Int32, SCHEMA_CLASSES, block_affine, run_sim,
    )

_SOURCE_DIR = Path(__file__).resolve().parent
WORD_BW_SUPPORTED = [32]
DEFAULT_SEED = 7


def _gen_x(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(-100, 100, size=DEFAULT_N).astype(np.int32)


def generate_vitis_sources(work_dir: Path) -> Path:
    """Generate ``include/`` headers + ``gen/block_scale.{cpp,hpp}`` + TB."""
    cfg = BuildConfig(root_dir=work_dir)
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir="include"))
    for cls in SCHEMA_CLASSES:
        dag.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED, include_dir=INCLUDE_DIR))
    dag.add(ArrayUtilsStep(Int32, WORD_BW_SUPPORTED))
    dag.add(MemMgrStep(output_dir="include"))
    dag.run(cfg)
    gen = work_dir / "gen"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "block_scale.cpp").write_text(kernel_to_cpp(BlockScale), encoding="utf-8")
    (gen / "block_scale.hpp").write_text(header_to_cpp(BlockScale), encoding="utf-8")
    for fname, content in tb_files_to_str(BlockScaleTBHls).items():
        (gen / fname).write_text(content, encoding="utf-8")
    return gen


@dataclass(kw_only=True)
class GenSourcesStep(BuildStep):
    description = "Generate include/ headers + gen/block_scale.{cpp,hpp} + gen/block_scale_tb.cpp."
    consumes = ["block_scale_source"]
    produces = {
        "include_dir": Path("include"),
        "kernel_cpp": Path("gen/block_scale.cpp"),
        "kernel_hpp": Path("gen/block_scale.hpp"),
        "tb_cpp": Path("gen/block_scale_tb.cpp"),
    }

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        generate_vitis_sources(config.root_dir)
        gen = config.root_dir / "gen"
        return {
            "include_dir": config.root_dir / "include",
            "kernel_cpp": gen / "block_scale.cpp",
            "kernel_hpp": gen / "block_scale.hpp",
            "tb_cpp": gen / "block_scale_tb.cpp",
        }


@dataclass(kw_only=True)
class BuildInputsStep(BuildStep):
    description = "Write the command (cmd.bin), the operand block (x_array.bin), and the golden y."
    consumes = ["block_scale_source"]
    produces = {"data_dir": Path("data"), "golden_y": Path("results/golden_y.json")}
    params = {"seed": DEFAULT_SEED}

    def run(self, config: BuildConfig, seed, **_) -> dict[str, Any]:
        data_dir = config.root_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        x = _gen_x(seed)
        # cmd carries the block length; addresses are placeholders the TB
        # overwrites via alloc (so the command stays parametric in n).
        BlockCmd(n=DEFAULT_N, x_addr=0, y_addr=0).write_uint32_file(str(data_dir / "cmd.bin"))
        write_uint32_file(x, elem_type=Int32, file_path=data_dir / "x_array.bin", nwrite=DEFAULT_N)
        gold = block_affine(x)
        results = config.root_dir / "results"
        results.mkdir(parents=True, exist_ok=True)
        golden_path = results / "golden_y.json"
        golden_path.write_text(json.dumps({"x": x.tolist(), "y": gold.tolist()}, indent=2),
                               encoding="utf-8")
        return {"data_dir": data_dir, "golden_y": golden_path}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    description = "Run the SimPy block_scale model and record golden parity."
    consumes = ["block_scale_source", "golden_y"]
    produces = {"sim_summary": Path("results/sim_summary.json")}
    params = {"seed": DEFAULT_SEED}

    def run(self, config: BuildConfig, seed, **_) -> dict[str, Any]:
        x = _gen_x(seed)
        y = run_sim(x)
        gold = block_affine(x)
        passed = bool(np.array_equal(y, gold))
        out = config.root_dir / "results" / "sim_summary.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"y": y.tolist(), "expected": gold.tolist(),
                                   "passed": passed}, indent=2), encoding="utf-8")
        if not passed:
            raise RuntimeError(f"SimPy golden parity failed: {y.tolist()} != {gold.tolist()}")
        return {"sim_summary": out}


def _check_output(config: BuildConfig, seed: int) -> tuple[bool, str]:
    data_dir = config.root_dir / "data"
    y = np.asarray(read_uint32_file(str(data_dir / "y_array.bin"), elem_type=Int32, shape=DEFAULT_N),
                   dtype=np.int32)
    gold = block_affine(_gen_x(seed))
    if not np.array_equal(y, gold):
        return False, f"y {y.tolist()} != golden {gold.tolist()}"
    return True, f"y={y.tolist()} matches golden"


def _run_tcl(config: BuildConfig, *, do_cosim: bool, trace_level: str, live_output: bool) -> None:
    toolchain.run_vitis_hls(
        config.root_dir / "run.tcl", work_dir=config.root_dir,
        capture_output=not live_output,
        env={
            "WAVEFLOW_BLOCK_SCALE_COSIM": "1" if do_cosim else "0",
            "WAVEFLOW_BLOCK_SCALE_TRACE_LEVEL": trace_level,
        },
    )


@dataclass(kw_only=True)
class CsimStep(BuildStep):
    description = "Vitis C-simulation of the block_scale kernel (vs the numpy golden)."
    consumes = ["kernel_cpp", "tb_cpp", "include_dir", "run_tcl", "data_dir"]
    produces = {"csim_verdict": Path("results/csim_verdict.json")}
    params = {"seed": DEFAULT_SEED, "live_output": False}

    def run(self, config: BuildConfig, seed, live_output, **_) -> dict[str, Any]:
        _run_tcl(config, do_cosim=False, trace_level="none", live_output=live_output)
        ok, detail = _check_output(config, seed)
        if not ok:
            raise RuntimeError(f"C-sim mismatch: {detail}")
        out = config.root_dir / "results" / "csim_verdict.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"passed": True, "detail": detail}, indent=2), encoding="utf-8")
        return {"csim_verdict": out}


@dataclass(kw_only=True)
class CosimStep(BuildStep):
    description = "Vitis C-synth + RTL co-simulation of the block_scale kernel (vs the golden)."
    consumes = ["kernel_cpp", "tb_cpp", "include_dir", "run_tcl", "csim_verdict"]
    produces = {"cosim_verdict": Path("results/cosim_verdict.json")}
    params = {"seed": DEFAULT_SEED, "trace_level": "none", "live_output": False}

    def run(self, config: BuildConfig, seed, trace_level, live_output, **_) -> dict[str, Any]:
        _run_tcl(config, do_cosim=True, trace_level=trace_level, live_output=live_output)
        ok, detail = _check_output(config, seed)
        if not ok:
            raise RuntimeError(f"Cosim mismatch: {detail}")
        out = config.root_dir / "results" / "cosim_verdict.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"passed": True, "detail": detail}, indent=2), encoding="utf-8")
        return {"cosim_verdict": out}


def build_block_scale_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="block_scale_source", path=_SOURCE_DIR / "block_scale.py"))
    dag.add(SourceStep(artifact="run_tcl", path=_SOURCE_DIR / "run.tcl"))
    dag.add(GenSourcesStep(name="gen"))
    dag.add(BuildInputsStep(name="build_inputs"))
    dag.add(PySimStep(name="py_sim"))
    dag.add(CsimStep(name="csim"))
    dag.add(CosimStep(name="cosim"))
    return dag


def main() -> None:
    run_dag_cli(
        build_block_scale_dag,
        description="Run the block_scale example.",
        default_through="py_sim",
        root_dir=_SOURCE_DIR,
        extra_args=[
            (("--seed",), {"type": int, "default": DEFAULT_SEED}),
            (("--trace-level",), {"default": "none", "choices": ["none", "port", "all"]}),
            (("--live-output",), {"action": "store_true"}),
        ],
        params_from_args=lambda a: {
            "seed": a.seed, "trace_level": a.trace_level, "live_output": a.live_output,
        },
    )


if __name__ == "__main__":
    main()
