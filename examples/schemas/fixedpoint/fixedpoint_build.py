"""Fixed-point conformance harness — Python vs Vitis ap_fixed, bit-exact.

The Phase-4 milestone: for each curated case, run the operation in Vitis C-sim
(``ap_fixed`` kernels from :mod:`kernels`) and assert the emitted stored bits equal
the bits the Python integer-backed model (``fixputils`` / ``DataArray[FixedField]``)
produces — **bit-for-bit, zero LSB disagreement**.  Covers (a) **quantization**
(reals → format, curated configs × modes) and (b) **vector arithmetic** — ``mult``,
``add``, ``quantize`` (requantize), and a **sum-of-products** — the full-precision
intermediate + quantize-on-assign model vs the generated kernels.

If Python and Vitis ever differ the Python model is wrong, not Vitis: fix it, never
loosen the comparison.  Built on the shared ``BuildDag`` + :func:`run_dag_cli`
pattern; factored so ``ComplexField`` reuses the same gen → csim → compare-bits rig.

CLI::

    python fixedpoint_build.py --through gen_conformance    # write kernels + vectors (no Vitis)
    python fixedpoint_build.py --through run_conformance     # the full csim conformance (Vitis)
    python fixedpoint_build.py --list-steps
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.hw.fixpoint import FixedField, add, fixed_sum, from_real, mult, quantize
from waveflow.toolchain import toolchain
from waveflow.utils.fixputils import OMode, QMode, to_bits

try:
    from examples.schemas.fixedpoint.kernels import (
        render_binop, render_dot, render_quantize_real, render_requant,
    )
except ModuleNotFoundError:  # direct execution from the example dir
    from kernels import (  # type: ignore[no-redef]
        render_binop, render_dot, render_quantize_real, render_requant,
    )

_SOURCE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class FixedConfig:
    name: str
    W: int
    int_bits: int
    signed: bool = True
    q_mode: QMode = QMode.AP_TRN
    o_mode: OMode = OMode.AP_WRAP

    @property
    def fixed_cls(self) -> type[FixedField]:
        return FixedField.specialize(self.W, self.int_bits, self.signed, self.q_mode, self.o_mode)

    @property
    def cpp_type(self) -> str:
        return self.fixed_cls.cpp_type


def _modes(name, W, I, signed):  # noqa: E741 — one FixedConfig per (Q, O)
    return [
        FixedConfig(f"{name}_trn_wrap", W, I, signed, QMode.AP_TRN, OMode.AP_WRAP),
        FixedConfig(f"{name}_rnd_wrap", W, I, signed, QMode.AP_RND, OMode.AP_WRAP),
        FixedConfig(f"{name}_trn_sat", W, I, signed, QMode.AP_TRN, OMode.AP_SAT),
        FixedConfig(f"{name}_rnd_sat", W, I, signed, QMode.AP_RND, OMode.AP_SAT),
    ]


QUANT_CONFIGS: list[FixedConfig] = [
    *_modes("s4_2", 4, 2, True), *_modes("s8_4", 8, 4, True), *_modes("u8_4", 8, 4, False),
    *_modes("s16_8", 16, 8, True), *_modes("s8_8", 8, 8, True), *_modes("s8_0", 8, 0, True),
]


def quant_values(cfg: FixedConfig) -> list[float]:
    """Edge sweep (exactly-representable doubles): exact, rounding midpoints,
    min/max overflow, negatives, unsigned-negative inputs."""
    lsb = 2.0 ** (-(cfg.W - cfg.int_bits))
    if cfg.signed:
        hi, lo = ((1 << (cfg.W - 1)) - 1) * lsb, -(1 << (cfg.W - 1)) * lsb
    else:
        hi, lo = ((1 << cfg.W) - 1) * lsb, 0.0
    vals = [0.0, lsb, -lsb, 2 * lsb, -2 * lsb, 0.25 * lsb, -0.25 * lsb, 0.5 * lsb,
            -0.5 * lsb, 0.75 * lsb, -0.75 * lsb, 1.5 * lsb, -1.5 * lsb,
            hi, hi + 0.5 * lsb, hi + lsb, 8 * hi, lo, lo - 0.5 * lsb, lo - lsb, -8 * hi]
    if not cfg.signed:
        vals += [-0.5 * lsb, -lsb, -2.0]
    return vals


# --- case construction --------------------------------------------------------
def _reals_text(values) -> str:
    return "\n".join(f"{float(v):.17g}" for v in values) + "\n"


def _bits_text(da, W: int) -> str:
    bits = np.atleast_1d(to_bits(da.val, W))
    return "\n".join(str(int(b)) for b in bits) + "\n"


def _expected(da, W: int) -> list[int]:
    return [int(b) for b in np.atleast_1d(to_bits(da.val, W))]


def _case(name, kernel, in_a, in_b, expected) -> dict:
    return {"name": name, "kernel": kernel, "in_a": in_a, "in_b": in_b, "expected": expected}


def build_cases() -> list[dict]:
    cases: list[dict] = []

    # (a) quantization: reals -> format, every curated config x mode
    for cfg in QUANT_CONFIGS:
        vals = quant_values(cfg)
        cases.append(_case(
            f"quant_{cfg.name}",
            render_quantize_real(cfg.cpp_type, cfg.W),
            _reals_text(vals), "",
            _expected(from_real(vals, cfg.fixed_cls), cfg.W)))

    # (b) arithmetic. Operand vectors are exactly representable in their formats.
    S8_4 = FixedConfig("s8_4", 8, 4)
    S8_2 = FixedConfig("s8_2", 8, 2)
    S4_2 = FixedConfig("s4_2", 4, 2)
    U8_4 = FixedConfig("u8_4", 8, 4, signed=False)
    ra = [1.5, -2.0, 0.5, 7.0, -3.5, 0.0, 7.9375, -8.0]
    rb = [2.0, 1.5, -1.0, 1.0, -2.0, 0.5, 1.0, 1.0]

    # mult: target = the exact product format (no quantization)
    for name, A, B, va, vb in [
        ("mult_s8_4", S8_4, S8_4, ra, rb),
        ("mult_s4_2_x_s8_4", S4_2, S8_4, [1.5, -2.0, 0.5, 1.75], [2.0, 1.5, -1.0, 4.0]),
        ("mult_u8_4", U8_4, U8_4, [1.5, 0.5, 7.0, 0.0625], [2.0, 1.0, 1.5, 16.0]),
    ]:
        a, b = from_real(va, A.fixed_cls), from_real(vb, B.fixed_cls)
        prod = mult(a, b)
        pf = prod.element_type
        cases.append(_case(
            name,
            render_binop("*", A.cpp_type, A.W, B.cpp_type, B.W, pf.cpp_type, pf.bitwidth),
            _bits_text(a, A.W), _bits_text(b, B.W), _expected(prod, pf.bitwidth)))

    # add: target = the exact aligned-sum format
    for name, A, B, va, vb in [
        ("add_s8_4", S8_4, S8_4, ra, rb),
        ("add_s8_4_x_s8_2", S8_4, S8_2, [1.5, -2.0, 0.5, 7.9375], [1.75, 1.5, -1.0, -2.0]),
    ]:
        a, b = from_real(va, A.fixed_cls), from_real(vb, B.fixed_cls)
        s = add(a, b)
        sf = s.element_type
        cases.append(_case(
            name,
            render_binop("+", A.cpp_type, A.W, B.cpp_type, B.W, sf.cpp_type, sf.bitwidth),
            _bits_text(a, A.W), _bits_text(b, B.W), _expected(s, sf.bitwidth)))

    # quantize (requantize a full-precision product down to s8_4) x all 4 modes
    a, b = from_real(ra, S8_4.fixed_cls), from_real(rb, S8_4.fixed_cls)
    prod = mult(a, b)                       # s16_8
    pf = prod.element_type
    for q in (QMode.AP_TRN, QMode.AP_RND):
        for o in (OMode.AP_WRAP, OMode.AP_SAT):
            target = FixedField.specialize(8, 4, q_mode=q, o_mode=o)
            pyq = quantize(prod, target)
            cases.append(_case(
                f"quant_prod_to_s8_4_{q.value[3:].lower()}_{o.value[3:].lower()}",
                render_requant(pf.cpp_type, pf.bitwidth, target.cpp_type, target.bitwidth),
                _bits_text(prod, pf.bitwidth), "", _expected(pyq, target.bitwidth)))

    # sum-of-products: s24_12 . s24_12 (N=16) -> full-precision acc -> quantize to s24_12
    S24_12 = FixedConfig("s24_12", 24, 12)
    rng = np.random.default_rng(7)
    n = 16
    dva = (rng.integers(-(1 << 23), 1 << 23, size=n) * 2.0 ** -12).astype(np.float64)
    dvb = (rng.integers(-(1 << 23), 1 << 23, size=n) * 2.0 ** -12).astype(np.float64)
    a, b = from_real(dva, S24_12.fixed_cls), from_real(dvb, S24_12.fixed_cls)
    acc = fixed_sum(mult(a, b))             # s52_28
    af = acc.element_type
    target = S24_12.fixed_cls
    pyq = quantize(acc, target)
    cases.append(_case(
        "dot_s24_12_n16",
        render_dot(S24_12.cpp_type, 24, S24_12.cpp_type, 24, af.cpp_type,
                   target.cpp_type, target.bitwidth),
        _bits_text(a, 24), _bits_text(b, 24), _expected(pyq, target.bitwidth)))

    return cases


# --- single-case driver (reused by the step loop AND the conformance test) ----
def gen_case_sources(case: dict, work_dir: Path) -> Path:
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "kernel.cpp").write_text(case["kernel"], encoding="utf-8")
    (work_dir / "in_a.txt").write_text(case["in_a"], encoding="utf-8")
    (work_dir / "in_b.txt").write_text(case["in_b"], encoding="utf-8")
    (work_dir / "expected.json").write_text(
        json.dumps({"name": case["name"], "expected": case["expected"]}, indent=2),
        encoding="utf-8")
    shutil.copy(_SOURCE_DIR / "run.tcl", work_dir / "run.tcl")
    return work_dir


def csim_and_compare(work_dir: Path, *, live_output: bool = False) -> dict:
    work_dir = Path(work_dir)
    expected = json.loads((work_dir / "expected.json").read_text(encoding="utf-8"))
    toolchain.run_vitis_hls(work_dir / "run.tcl", work_dir=work_dir,
                            capture_output=not live_output)
    vitis = [int(tok) for tok in (work_dir / "out_bits.txt").read_text(encoding="utf-8").split()]
    exp = expected["expected"]
    mism = [{"i": i, "expected": e, "vitis": g}
            for i, (e, g) in enumerate(zip(exp, vitis)) if e != g]
    return {"name": expected["name"], "n": len(exp),
            "count_ok": len(vitis) == len(exp), "mismatches": mism,
            "exact": len(vitis) == len(exp) and not mism}


def conformance_for_case(case: dict, work_dir: Path, *, live_output: bool = False) -> dict:
    gen_case_sources(case, work_dir)
    return csim_and_compare(work_dir, live_output=live_output)


# --- BuildDag steps -----------------------------------------------------------
@dataclass(kw_only=True)
class GenConformanceStep(BuildStep):
    description = "Generate the per-case ap_fixed kernels + input vectors + Python golden bits."
    consumes = ["fixedpoint_source", "run_tcl", "kernels_source"]
    produces = {"conformance_gen": Path("gen")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        gen = config.root_dir / "gen"
        gen.mkdir(parents=True, exist_ok=True)
        for case in build_cases():
            gen_case_sources(case, gen / case["name"])
        return {"conformance_gen": gen}


@dataclass(kw_only=True)
class RunConformanceStep(BuildStep):
    description = "Per case: Vitis csim, assert Vitis bits == Python bits exactly (quant + arithmetic)."
    consumes = ["conformance_gen"]
    produces = {"conformance_report": Path("results/conformance_report.json")}
    params = {"live_output": False}

    def run(self, config: BuildConfig, live_output, **_) -> dict:
        gen = config.root_dir / "gen"
        results = [csim_and_compare(gen / case["name"], live_output=live_output)
                   for case in build_cases()]
        report_path = config.root_dir / "results" / "conformance_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        n_exact = sum(r["exact"] for r in results)
        report_path.write_text(json.dumps(
            {"n_cases": len(results), "n_exact": n_exact,
             "all_exact": n_exact == len(results), "results": results}, indent=2),
            encoding="utf-8")
        failed = [r for r in results if not r["exact"]]
        if failed:
            raise RuntimeError(
                f"STOP — Vitis disagreed with the Python model on {len(failed)}/{len(results)} "
                "cases. The Python model is wrong, not Vitis; fix it, do not loosen the "
                f"comparison. First failure: {failed[0]}")
        return {"conformance_report": report_path}


def build_fixedpoint_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="fixedpoint_source", path=_SOURCE_DIR / "fixedpoint_build.py"))
    dag.add(SourceStep(artifact="kernels_source", path=_SOURCE_DIR / "kernels.py"))
    dag.add(SourceStep(artifact="run_tcl", path=_SOURCE_DIR / "run.tcl"))
    dag.add(GenConformanceStep(name="gen_conformance"))
    dag.add(RunConformanceStep(name="run_conformance"))
    return dag


def main() -> None:
    run_dag_cli(
        build_fixedpoint_dag,
        description="Fixed-point (ap_fixed) Python-vs-Vitis bit-exact conformance (quant + arithmetic).",
        default_through="gen_conformance",
        root_dir=_SOURCE_DIR,
        extra_args=[(("--live-output",), {"action": "store_true"})],
        params_from_args=lambda a: {"live_output": a.live_output},
    )


if __name__ == "__main__":
    main()
