"""basic_vec — the vectorization front-door: one MAC, ``y = a*b + c``, bit-exact.

For each numeric kind (int / float / fixed) we compute the **same** elementwise MAC
two ways and assert the raw bits match:

* **Python golden** — the vectorized type-preserving operators on `DataArray`
  (`a * b + c`), then an explicit `quantize` for the fixed case. No per-element loop.
* **Vitis kernel** — the corresponding vectorized C++ (`kernels.py`), run in C-sim.

This is the minimal, teaching version of the conformance idea (the rigorous
all-modes/widths sweep is `examples/schemas/fixedpoint`); it shares the machinery —
the `BuildDag` + :func:`run_dag_cli` + gen→csim→compare-bits pattern.

CLI::

    python basic_vec_build.py --through gen        # write kernels + vectors (no Vitis)
    python basic_vec_build.py --through run        # the bit-exact csim conformance (Vitis)
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.hw.dataschema import DataArray, FloatField, IntField
from waveflow.hw.fixpoint import FixedField, from_real, quantize
from waveflow.toolchain import toolchain
from waveflow.utils.fixputils import to_bits

try:
    from examples.basic_vec.kernels import render_fixed_mac, render_float_mac, render_int_mac
except ModuleNotFoundError:  # direct execution from the example dir
    from kernels import render_fixed_mac, render_float_mac, render_int_mac  # type: ignore[no-redef]

_SOURCE_DIR = Path(__file__).resolve().parent


def _int_bits(da: DataArray, W: int) -> list[int]:
    return [int(b) for b in np.atleast_1d(to_bits(da.val, W))]


def _f32_bits(da: DataArray) -> list[int]:
    return [int(u) for u in da.val.view(np.uint32)]


def _case(name, kernel, a, b, c, expected) -> dict:
    return {"name": name, "kernel": kernel, "a": a, "b": b, "c": c, "expected": expected}


def build_cases() -> list[dict]:
    """The three MAC cases. Each computes its Python golden with the operators, derives
    the result type, and renders the matching kernel."""
    cases: list[dict] = []

    # --- integer: a*b + c, growth-aware (result width derived by the operators) ---
    I8 = IntField.specialize(8, True)

    def ia(vals):
        return DataArray.specialize(I8, max_shape=(len(vals),))(vals)

    a, b, c = ia([3, -4, 5, 7]), ia([6, 7, -8, 2]), ia([1, -1, 2, -3])
    y = a * b + c                                          # IntField<17>
    wy = y.element_type.get_bitwidth()
    cases.append(_case("int_mac", render_int_mac(8, 8, 8, wy),
                       _int_bits(a, 8), _int_bits(b, 8), _int_bits(c, 8), _int_bits(y, wy)))

    # --- float: a*b + c, numpy float32 passthrough (two roundings) ---
    F32 = FloatField.specialize(32)

    def fa(vals):
        return DataArray.specialize(F32, max_shape=(len(vals),))(np.array(vals, dtype=np.float32))

    fa_, fb_, fc_ = fa([1.5, 2.5, -3.0]), fa([2.0, -1.5, 0.5]), fa([0.25, 1.0, -0.5])
    fy = fa_ * fb_ + fc_
    cases.append(_case("float_mac", render_float_mac(),
                       _f32_bits(fa_), _f32_bits(fb_), _f32_bits(fc_), _f32_bits(fy)))

    # --- fixed: full-precision a*b + c, then one explicit quantize to the working fmt ---
    Q = FixedField.specialize(8, 4)
    qa = from_real([1.5, -2.0, 0.5], Q)
    qb = from_real([2.0, 1.5, -1.0], Q)
    qc = from_real([0.5, 0.25, -0.5], Q)
    qy = quantize(qa * qb + qc, Q)
    cases.append(_case("fixed_mac",
                       render_fixed_mac(Q.cpp_type, 8, Q.cpp_type, 8, Q.cpp_type, 8, Q.cpp_type, 8),
                       _int_bits(qa, 8), _int_bits(qb, 8), _int_bits(qc, 8), _int_bits(qy, 8)))
    return cases


def gen_case_sources(case: dict, work_dir: Path) -> Path:
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "kernel.cpp").write_text(case["kernel"], encoding="utf-8")
    for key in ("a", "b", "c"):
        (work_dir / f"in_{key}.txt").write_text(
            "\n".join(str(v) for v in case[key]) + "\n", encoding="utf-8")
    (work_dir / "expected.json").write_text(
        json.dumps({"name": case["name"], "expected": case["expected"]}, indent=2), encoding="utf-8")
    shutil.copy(_SOURCE_DIR / "run.tcl", work_dir / "run.tcl")
    return work_dir


def csim_and_compare(work_dir: Path, *, live_output: bool = False) -> dict:
    work_dir = Path(work_dir)
    expected = json.loads((work_dir / "expected.json").read_text(encoding="utf-8"))
    toolchain.run_vitis_hls(work_dir / "run.tcl", work_dir=work_dir, capture_output=not live_output)
    vitis = [int(t) for t in (work_dir / "out_bits.txt").read_text(encoding="utf-8").split()]
    exp = expected["expected"]
    mism = [{"i": i, "expected": e, "vitis": g} for i, (e, g) in enumerate(zip(exp, vitis)) if e != g]
    return {"name": expected["name"], "count_ok": len(vitis) == len(exp),
            "mismatches": mism, "exact": len(vitis) == len(exp) and not mism}


def conformance_for_case(case: dict, work_dir: Path, *, live_output: bool = False) -> dict:
    gen_case_sources(case, work_dir)
    return csim_and_compare(work_dir, live_output=live_output)


@dataclass(kw_only=True)
class GenStep(BuildStep):
    description = "Generate the int/float/fixed MAC kernels + input vectors + Python golden bits."
    consumes = ["basic_vec_source", "kernels_source", "run_tcl"]
    produces = {"basic_vec_gen": Path("gen")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        gen = config.root_dir / "gen"
        gen.mkdir(parents=True, exist_ok=True)
        for case in build_cases():
            gen_case_sources(case, gen / case["name"])
        return {"basic_vec_gen": gen}


@dataclass(kw_only=True)
class RunStep(BuildStep):
    description = "Per kind: Vitis csim, assert Vitis bits == Python operator bits exactly."
    consumes = ["basic_vec_gen"]
    produces = {"report": Path("results/basic_vec_report.json")}
    params = {"live_output": False}

    def run(self, config: BuildConfig, live_output, **_) -> dict:
        gen = config.root_dir / "gen"
        results = [csim_and_compare(gen / case["name"], live_output=live_output)
                   for case in build_cases()]
        report = config.root_dir / "results" / "basic_vec_report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        n_exact = sum(r["exact"] for r in results)
        report.write_text(json.dumps(
            {"n_cases": len(results), "n_exact": n_exact, "all_exact": n_exact == len(results),
             "results": results}, indent=2), encoding="utf-8")
        failed = [r for r in results if not r["exact"]]
        if failed:
            raise RuntimeError(f"STOP — Vitis disagreed with the Python golden: {failed[0]}")
        return {"report": report}


def build_basic_vec_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="basic_vec_source", path=_SOURCE_DIR / "basic_vec_build.py"))
    dag.add(SourceStep(artifact="kernels_source", path=_SOURCE_DIR / "kernels.py"))
    dag.add(SourceStep(artifact="run_tcl", path=_SOURCE_DIR / "run.tcl"))
    dag.add(GenStep(name="gen"))
    dag.add(RunStep(name="run"))
    return dag


def main() -> None:
    run_dag_cli(
        build_basic_vec_dag,
        description="basic_vec — vectorized Python golden vs vectorized Vitis (int/float/fixed MAC).",
        default_through="gen",
        root_dir=_SOURCE_DIR,
        extra_args=[(("--live-output",), {"action": "store_true"})],
        params_from_args=lambda a: {"live_output": a.live_output},
    )


if __name__ == "__main__":
    main()
