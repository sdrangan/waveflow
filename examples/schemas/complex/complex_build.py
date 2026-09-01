"""Complex conformance harness -- Python ``ComplexField`` vs Vitis complex, bit-exact.

For each curated case, run the complex op in Vitis C-sim and assert the emitted **packed
words** equal the words the Python ``DataArray[ComplexField]`` model produces -- bit-for-bit,
zero LSB disagreement -- per inner:

- **fixed** -> ``std::complex<ap_fixed>``  (signed cmult/cadd/csub/conj, unsigned cadd; the
  serialization round-trip).
- **int**   -> the Waveflow ``wf_cint`` struct (s8 / s16: cmult/cadd/csub/conj + round-trip).
- **float** -> ``std::complex<float>`` / ``std::complex<double>``  (round-trip + the
  complex-multiply edge + cadd/csub/conj).

This is the **migrated** harness (plans/complex_serialization.md Phase 3): the operand /
result vectors are the **generated serialization** (:func:`arrayutils.write_array` /
:func:`read_array` over ``DataArray[ComplexField]``, replacing the old hand-interleaving), and
the kernel arithmetic is **``complex_utils.hpp``** (replacing the inline formula).  Each
operand / result is (de)serialized at ``word_bw = its element bitwidth`` (<=64) so the packing
is one element per word -- exactly what ``write_array`` produces -- and the kernel uses the
generated ``<type>_array_utils::read_array`` / ``write_array`` to match it on the C++ side.

If Python and Vitis ever differ the Python model is the spec: fix the codegen / kernel, never
loosen the compare.  Built on the shared ``BuildDag`` + :func:`run_dag_cli` rig.

CLI::

    python complex_build.py --through gen_conformance   # write kernels + headers + vectors
    python complex_build.py --through run_conformance    # the full csim conformance (Vitis)
    python complex_build.py --list-steps
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import (
    _array_utils_filename, _array_utils_namespace, gen_array_utils, get_nwords, write_array,
)
from waveflow.hw.complexfield import ComplexField, cadd, cmult, conj, cquantize, csub
from waveflow.hw.dataschema import DataArray, FloatField, IntField
from waveflow.hw.fixpoint import FixedField
from waveflow.toolchain import toolchain
from waveflow.utils import complexutils as cx
from waveflow.utils.fixputils import OMode, QMode, quantize_real

try:
    from examples.schemas.complex.kernels import render_kernel
except ModuleNotFoundError:  # direct execution from the example dir
    from kernels import render_kernel  # type: ignore[no-redef]

_SOURCE_DIR = Path(__file__).resolve().parent
_BUILD_DIR = Path(__file__).resolve().parents[3] / "waveflow" / "build"   # complex_utils.hpp / wf_cint.h


# --- config -------------------------------------------------------------------
@dataclass(frozen=True)
class ComplexConfig:
    name: str
    kind: str                       # "fixed" | "int" | "float"
    W: int = 0
    int_bits: int = 0
    signed: bool = True
    q_mode: QMode = QMode.AP_TRN
    o_mode: OMode = OMode.AP_WRAP
    fbw: int = 0                    # float bitwidth (32/64)

    @property
    def inner_cls(self):
        if self.kind == "fixed":
            return FixedField.specialize(self.W, self.int_bits, self.signed, self.q_mode, self.o_mode)
        if self.kind == "int":
            return IntField.specialize(self.W, self.signed)
        return FloatField.specialize(self.fbw)

    @property
    def complex_cls(self):
        return ComplexField.specialize(self.inner_cls)


# curated signed fixed configs (2W+1 <= 64 for all -> cmult ok)
FIXED_SIGNED = [
    ComplexConfig("s4_2", "fixed", 4, 2), ComplexConfig("s8_4", "fixed", 8, 4),
    ComplexConfig("s16_8", "fixed", 16, 8), ComplexConfig("s8_8", "fixed", 8, 8),
    ComplexConfig("s8_0", "fixed", 8, 0),
]
FIXED_UNSIGNED = ComplexConfig("u8_4", "fixed", 8, 4, signed=False)
INTS = [ComplexConfig("i8", "int", 8), ComplexConfig("i16", "int", 16)]
FLOATS = [ComplexConfig("f32", "float", fbw=32), ComplexConfig("f64", "float", fbw=64)]


# --- value helpers ------------------------------------------------------------
def _stored_codes(W: int, signed: bool, n: int = 6) -> np.ndarray:
    lo, hi = (-(1 << (W - 1)), (1 << (W - 1)) - 1) if signed else (0, (1 << W) - 1)
    return np.linspace(lo, hi, n).astype(np.int64)


def _da(cf, val) -> DataArray:
    arr = np.asarray(val)
    return DataArray.specialize(cf, max_shape=(arr.shape[0],))(arr)


def _fixed_int_operand(cfg: ComplexConfig, seed: int) -> DataArray:
    fmt = cfg.inner_cls.get_format() if cfg.kind == "fixed" else cx.int_format(cfg.W, cfg.signed)
    re = _stored_codes(cfg.W, cfg.signed)
    im = np.roll(_stored_codes(cfg.W, cfg.signed), seed)
    return _da(cfg.complex_cls, cx.make_complex(re, im, fmt))


def _float_operand(cfg: ComplexConfig, vals) -> DataArray:
    dt = np.complex128 if cfg.fbw == 64 else np.complex64
    return _da(cfg.complex_cls, np.asarray(vals, dtype=dt))


def _roundtrip_pairs(cfg: ComplexConfig):
    """(re, im) real pairs for the round-trip: exact, rounding midpoints, overflow."""
    lsb = 2.0 ** (-(cfg.W - cfg.int_bits))
    if cfg.signed:
        hi, lo = ((1 << (cfg.W - 1)) - 1) * lsb, -(1 << (cfg.W - 1)) * lsb
    else:
        hi, lo = ((1 << cfg.W) - 1) * lsb, 0.0
    vals = [0.0, lsb, -lsb, 0.5 * lsb, -0.5 * lsb, 1.5 * lsb, hi, hi + 0.5 * lsb, lo, lo - lsb]
    return [(vals[i], vals[-1 - i]) for i in range(len(vals))]


def _roundtrip_operand(cfg: ComplexConfig) -> DataArray:
    """The round-trip operand: the curated quantization-edge values, **quantized** into the
    inner format (so the serialization round-trip carries the exact stored representation)."""
    if cfg.kind == "float":
        pairs = [(1.1, -2.2), (3.3, 0.4), (-5.5, 6.6), (0.0, -1.0)]
        dt = np.complex128 if cfg.fbw == 64 else np.complex64
        return _da(cfg.complex_cls, np.asarray([complex(r, i) for r, i in pairs], dtype=dt))
    if cfg.kind == "int":
        return _fixed_int_operand(cfg, 2)
    fmt = cfg.inner_cls.get_format()
    pairs = _roundtrip_pairs(cfg)
    re_q = quantize_real(np.array([r for r, _ in pairs]), fmt)
    im_q = quantize_real(np.array([i for _, i in pairs]), fmt)
    return _da(cfg.complex_cls, cx.make_complex(re_q, im_q, fmt))


# --- one case -----------------------------------------------------------------
def _wbw(cf) -> int:
    """word_bw = element bitwidth (<=64) so pf=1, one element per word -- the layout
    ``write_array`` produces; 64 for the 128-bit float64 element (2 words/element)."""
    bw = cf.get_bitwidth()
    return bw if bw <= 64 else 64


@dataclass
class Case:
    name: str
    op: str                          # roundtrip | cmult | cadd | csub | conj | cquantize
    a: DataArray
    golden: DataArray
    b: DataArray | None = None
    expected: list = field(default_factory=list)

    @property
    def in_cf(self):
        return type(self.a).element_type

    @property
    def out_cf(self):
        return type(self.golden).element_type

    @property
    def binary(self) -> bool:
        return self.op in ("cmult", "cadd", "csub")


_OPS = {"cmult": cmult, "cadd": cadd, "csub": csub, "conj": conj}


def _mk_case(name: str, op: str, a: DataArray, b: DataArray | None = None,
             target=None) -> Case:
    if op == "roundtrip":
        golden = a
    elif op == "cquantize":
        golden = cquantize(a, target)
    elif op == "conj":
        golden = conj(a)
    else:
        golden = _OPS[op](a, b)
    wbo = _wbw(type(golden).element_type)
    expected = [int(w) for w in np.asarray(write_array(golden, word_bw=wbo)).ravel()]
    return Case(name=name, op=op, a=a, b=b, golden=golden, expected=expected)


def build_cases() -> list[dict]:
    cases: list[Case] = []

    # ---- fixed (signed): round-trip + cmult/cadd/csub/conj ----
    for cfg in FIXED_SIGNED:
        a, b = _fixed_int_operand(cfg, 2), _fixed_int_operand(cfg, 4)
        cases.append(_mk_case(f"roundtrip_{cfg.name}", "roundtrip", _roundtrip_operand(cfg)))
        for op in ("cmult", "cadd", "csub"):
            cases.append(_mk_case(f"{op}_{cfg.name}", op, a, b))
        cases.append(_mk_case(f"conj_{cfg.name}", "conj", a))

    # ---- fixed (unsigned): round-trip + cadd only (cmult/conj are signed-only) ----
    cfg = FIXED_UNSIGNED
    a, b = _fixed_int_operand(cfg, 2), _fixed_int_operand(cfg, 4)
    cases.append(_mk_case(f"roundtrip_{cfg.name}", "roundtrip", _roundtrip_operand(cfg)))
    cases.append(_mk_case(f"cadd_{cfg.name}", "cadd", a, b))

    # ---- cquantize: requantize a wide complex operand into a narrower declared format ----
    # Exercises both QModes x both OModes, and the wide->narrow path the FFT butterfly needs.
    for q in (QMode.AP_TRN, QMode.AP_RND):
        for o in (OMode.AP_WRAP, OMode.AP_SAT):
            wide = ComplexConfig("q_src", "fixed", W=24, int_bits=8, signed=True,
                                 q_mode=QMode.AP_TRN, o_mode=OMode.AP_WRAP)
            narrow = ComplexConfig(f"q_{q.name.lower()}_{o.name.lower()}", "fixed", W=12,
                                   int_bits=4, signed=True, q_mode=q, o_mode=o)
            src = _fixed_int_operand(wide, 3)
            cases.append(_mk_case(f"cquantize_{narrow.name}", "cquantize", src,
                                  target=narrow.complex_cls))

    # ---- int (signed s8 / s16): round-trip + cmult/cadd/csub/conj ----
    for cfg in INTS:
        a, b = _fixed_int_operand(cfg, 2), _fixed_int_operand(cfg, 4)
        cases.append(_mk_case(f"roundtrip_{cfg.name}", "roundtrip", _roundtrip_operand(cfg)))
        for op in ("cmult", "cadd", "csub"):
            cases.append(_mk_case(f"{op}_{cfg.name}", op, a, b))
        cases.append(_mk_case(f"conj_{cfg.name}", "conj", a))

    # ---- float (f32 / f64): round-trip + cmult (the edge) + cadd/csub/conj ----
    # Rounding-TRIGGERING random operands (fixed seed): products are non-exact, so this
    # genuinely exercises the complex-multiply edge (the explicit naive formula in
    # complex_utils.hpp, bit-exact with cmult_float; numpy's FMA `*` would diverge).
    rng = np.random.default_rng(20240607)
    fa_vals = rng.standard_normal(64) + 1j * rng.standard_normal(64)
    fb_vals = rng.standard_normal(64) + 1j * rng.standard_normal(64)
    for cfg in FLOATS:
        a, b = _float_operand(cfg, fa_vals), _float_operand(cfg, fb_vals)
        cases.append(_mk_case(f"roundtrip_{cfg.name}", "roundtrip", _roundtrip_operand(cfg)))
        for op in ("cmult", "cadd", "csub"):
            cases.append(_mk_case(f"{op}_{cfg.name}", op, a, b))
        cases.append(_mk_case(f"conj_{cfg.name}", "conj", a))

    return [_case_dict(c) for c in cases]


def _case_dict(c: Case) -> dict:
    return {"name": c.name, "case": c, "expected": c.expected}


# --- per-case source generation (headers + kernel + vectors) ------------------
def _words_text(da: DataArray, word_bw: int) -> str:
    words = np.asarray(write_array(da, word_bw=word_bw)).ravel()
    return "\n".join(str(int(w)) for w in words) + "\n"


def gen_case_sources(case_dict: dict, work_dir: Path) -> Path:
    """Generate one case's dir: the generated headers (streamutils + wf_cint + complex_utils
    + the in/out array-utils), the kernel, the packed-word input vectors, the golden bits."""
    c: Case = case_dict["case"]
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    in_cf, out_cf = c.in_cf, c.out_cf
    wbi, wbo = _wbw(in_cf), _wbw(out_cf)
    n = int(np.asarray(c.a.val).shape[0])

    # generated support headers
    cfg = BuildConfig(root_dir=work_dir)
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir="include"))
    dag.run(cfg)
    gen_array_utils(in_cf, [wbi], cfg=cfg, streamutils_dir="include")
    if out_cf is not in_cf:
        gen_array_utils(out_cf, [wbo], cfg=cfg, streamutils_dir="include")
    for h in ("complex_utils.hpp", "wf_cint.h"):
        shutil.copy(_BUILD_DIR / h, work_dir / h)

    kernel = render_kernel(
        op=c.op, in_cpp=in_cf.cpp_type, out_cpp=out_cf.cpp_type,
        in_ns=_array_utils_namespace(in_cf), out_ns=_array_utils_namespace(out_cf),
        in_hdr=_array_utils_filename(in_cf), out_hdr=_array_utils_filename(out_cf),
        wbi=wbi, wbo=wbo, n=n,
        nwa=get_nwords(in_cf, wbi, n), nwy=get_nwords(out_cf, wbo, n), binary=c.binary,
    )
    (work_dir / "kernel.cpp").write_text(kernel, encoding="utf-8")
    (work_dir / "in_a.txt").write_text(_words_text(c.a, wbi), encoding="utf-8")
    (work_dir / "in_b.txt").write_text(
        _words_text(c.b, wbi) if c.binary else "0\n", encoding="utf-8")
    (work_dir / "expected.json").write_text(
        json.dumps({"name": c.name, "expected": c.expected}, indent=2), encoding="utf-8")
    shutil.copy(_SOURCE_DIR / "run.tcl", work_dir / "run.tcl")
    return work_dir


def csim_and_compare(work_dir: Path, *, live_output: bool = False) -> dict:
    work_dir = Path(work_dir)
    expected = json.loads((work_dir / "expected.json").read_text(encoding="utf-8"))
    toolchain.run_vitis_hls(work_dir / "run.tcl", work_dir=work_dir, capture_output=not live_output)
    vitis = [int(tok) for tok in (work_dir / "out_bits.txt").read_text(encoding="utf-8").split()]
    exp = expected["expected"]
    mism = [{"i": i, "expected": e, "vitis": g}
            for i, (e, g) in enumerate(zip(exp, vitis)) if e != g]
    return {"name": expected["name"], "n": len(exp),
            "count_ok": len(vitis) == len(exp), "mismatches": mism,
            "exact": len(vitis) == len(exp) and not mism}


def conformance_for_case(case_dict: dict, work_dir: Path, *, live_output: bool = False) -> dict:
    gen_case_sources(case_dict, work_dir)
    return csim_and_compare(work_dir, live_output=live_output)


# --- BuildDag steps -----------------------------------------------------------
@dataclass(kw_only=True)
class GenConformanceStep(BuildStep):
    description = "Generate the per-case headers (generated serialization) + complex_utils kernel + vectors."
    consumes = ["complex_source", "run_tcl", "kernels_source"]
    produces = {"conformance_gen": Path("gen")}
    params: dict = field(default_factory=dict)

    def run(self, config: BuildConfig, **_) -> dict:
        gen = config.root_dir / "gen"
        gen.mkdir(parents=True, exist_ok=True)
        for case in build_cases():
            gen_case_sources(case, gen / case["name"])
        return {"conformance_gen": gen}


@dataclass(kw_only=True)
class RunConformanceStep(BuildStep):
    description = "Per case: Vitis csim, assert Vitis words == Python write_array words exactly."
    consumes = ["conformance_gen"]
    produces = {"conformance_report": Path("results/conformance_report.json")}
    params: dict = field(default_factory=lambda: {"live_output": False})

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
                "cases. The Python model is the spec; fix the codegen/kernel, do not loosen the "
                f"comparison. First failure: {failed[0]}")
        return {"conformance_report": report_path}


def build_complex_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="complex_source", path=_SOURCE_DIR / "complex_build.py"))
    dag.add(SourceStep(artifact="kernels_source", path=_SOURCE_DIR / "kernels.py"))
    dag.add(SourceStep(artifact="run_tcl", path=_SOURCE_DIR / "run.tcl"))
    dag.add(GenConformanceStep(name="gen_conformance"))
    dag.add(RunConformanceStep(name="run_conformance"))
    return dag


def main() -> None:
    run_dag_cli(
        build_complex_dag,
        description="Complex (ComplexField) Python-vs-Vitis bit-exact conformance (generated serialization + complex_utils.hpp).",
        default_through="gen_conformance",
        root_dir=_SOURCE_DIR,
        extra_args=[(("--live-output",), {"action": "store_true"})],
        params_from_args=lambda a: {"live_output": a.live_output},
    )


if __name__ == "__main__":
    main()
