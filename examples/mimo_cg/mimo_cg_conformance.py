"""mimo_cg_conformance.py — the Python golden CG vs its C++ reference, bit for bit, in Vitis C-sim.

Step 2.4 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (AC2.3).  Mirrors the fixed-point harness
in ``examples/schemas/fixedpoint/fixedpoint_build.py``: generate → C-sim → compare every bit.

* **Generate.**  For each case set, Python writes ``main.cpp``: a traits struct of
  ``ap_fixed`` typedefs derived from :class:`~examples.mimo_cg.mimo_cg_fixed.CgFormats`
  (registers, plus the exact accumulators from
  :func:`~examples.mimo_cg.mimo_cg_fixed.accumulator_formats`) around the hand-written
  ``cpp/cg_ref.h``.  It also writes ``inputs.txt``: each case's A/M and B/M as the stored
  integers of :func:`~examples.mimo_cg.mimo_cg_fixed.quantize_inputs`, the same integers the
  golden starts from.
* **C-sim.**  ``cpp/run.tcl`` runs Vitis HLS C-simulation for ``xczu48dr-ffvg1517-2-e``.
* **Compare.**  Every stored bit after every iteration: X (re and im), α and β.  If Python and
  C++ ever differ, find which is wrong; never loosen the comparison.

Run from the repo root::

    python -m examples.mimo_cg.mimo_cg_conformance --list
    python -m examples.mimo_cg.mimo_cg_conformance           # every case set (needs Vitis)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from examples.mimo_cg.detectors import mmse_matrix
from examples.mimo_cg.hw.common import render_typedefs
from examples.mimo_cg.mimo_cg_fixed import (
    CgFormats,
    cg_fixed,
    quantize_inputs,
    register,
)
from examples.mimo_cg.mimo_link import Qam, noise_variance, point_rng, rayleigh
from waveflow.toolchain import toolchain
from waveflow.utils.fixputils import Format, to_bits

CPP_DIR = Path(__file__).resolve().parent / "cpp"

#: Random cases per case set (AC2.3 asks for at least 50 per format set).
CASES_PER_SET = 50

#: Format sets: the wide reference, two Phase 3-like widths with guards, and a narrow set
#: with no guards, which saturates and quantizes residuals to zero often.
FORMAT_SETS: dict[str, CgFormats] = {
    "wide": CgFormats.wide(),
    "w18_g6_d4": CgFormats.from_width(18, 6, 4),
    "w12_g4_d4": CgFormats.from_width(12, 4, 4),
    "w10_g0_d0": CgFormats.from_width(10, 0, 0),
}


#: Conformance-only stress formats, not a design point: alpha and beta get 2 and 1 integer bits,
#: so they saturate often at K = 16 (about 6 alpha and 133 beta saturations per case set) and the
#: saturation paths of golden and C++ are compared.  The design formats never saturate them.
STRESS_FORMATS = dataclasses.replace(
    CgFormats.from_width(12, 2, 2), alpha=register(14, 2), beta=register(14, 1)
)


@dataclass(frozen=True)
class CaseSetSpec:
    name: str
    formats: CgFormats
    K: int
    N: int
    nit: int
    explicit: bool
    seed: int


def case_set_specs() -> list[CaseSetSpec]:
    """Every format set in both residual forms at K = 8, the wide set at K = 16, and the
    saturation stress set in both forms at K = 16."""
    specs = []
    for i, (name, fmt) in enumerate(FORMAT_SETS.items()):
        for explicit in (False, True):
            form = "explicit" if explicit else "recurrence"
            specs.append(
                CaseSetSpec(
                    f"{name}_{form}_k8", fmt, 8, 8, 8, explicit, 100 + 2 * i + explicit
                )
            )
    specs.append(
        CaseSetSpec("wide_recurrence_k16", FORMAT_SETS["wide"], 16, 4, 16, False, 120)
    )
    for explicit in (False, True):
        form = "explicit" if explicit else "recurrence"
        specs.append(
            CaseSetSpec(
                f"stress_sat_{form}_k16", STRESS_FORMATS, 16, 4, 16, explicit, 130
            )
        )
    return specs


def _problems(spec: CaseSetSpec) -> list[tuple[np.ndarray, np.ndarray, float]]:
    """``CASES_PER_SET`` random problems ``(A, B, M)``, then one zero-residual problem.

    M cycles through 32, 64, 128; the SNR is uniform in −5…25 dB; the symbols are 16-QAM.  The
    zero-residual problem is the first one with column 2 of B set to zero, so that column's
    rᴴr and pᴴAp are exactly zero and both divisions take the zero guard.
    """
    qam = Qam(16)
    out = []
    for c in range(CASES_PER_SET):
        M = (32, 64, 128)[c % 3]
        rng = point_rng(60, spec.seed, c)
        rho_db = float(rng.uniform(-5.0, 25.0))
        H = rayleigh(rng, (M, spec.K))
        tx = rng.integers(0, 2, size=(spec.N, spec.K * qam.bits_per_symbol))
        sigma2 = noise_variance(rho_db)
        Y = H @ qam.modulate(tx).T + math.sqrt(sigma2) * rayleigh(rng, (M, spec.N))
        out.append((mmse_matrix(H, sigma2), H.conj().T @ Y, float(M)))
    A0, B0, M0 = out[0]
    B_zero = B0.copy()
    B_zero[:, 2] = 0
    out.append((A0, B_zero, M0))
    return out


def render_main(spec: CaseSetSpec, n_cases: int) -> str:
    """The C-sim testbench: traits typedefs around ``cg_ref.h``, reading and writing stored bits."""
    typedefs = render_typedefs(spec.formats, spec.K)
    K, N, NIT = spec.K, spec.N, spec.nit
    return f"""// GENERATED by examples/mimo_cg/mimo_cg_conformance.py -- do not edit.
#include <ap_fixed.h>
#include <ap_int.h>
#include <fstream>
#include "cg_ref.h"

struct T {{
    static const int K = {K}, N = {N}, NIT = {NIT};
    static const bool EXPLICIT = {"true" if spec.explicit else "false"};
{typedefs}
}};

template <class F> static F from_bits(unsigned long long v) {{
    F f;
    f.range(F::width - 1, 0) = ap_uint<F::width>(v);
    return f;
}}
template <class F> static unsigned long long bits(const F& f) {{
    return (unsigned long long)f.range(F::width - 1, 0);
}}

static T::a_t a_re[{K}][{K}], a_im[{K}][{K}];
static T::b_t b_re[{K}][{N}], b_im[{K}][{N}];
static T::x_t x_re[{NIT}][{K}][{N}], x_im[{NIT}][{K}][{N}];
static T::alpha_t alpha[{NIT}][{N}];
static T::beta_t beta[{NIT}][{N}];

int main(int argc, char** argv) {{
    std::ifstream in(argv[1]);
    std::ofstream out(argv[2]);
    unsigned long long v;
    for (int c = 0; c < {n_cases}; ++c) {{
        for (int i = 0; i < {K}; ++i) for (int j = 0; j < {K}; ++j) {{ in >> v; a_re[i][j] = from_bits<T::a_t>(v); }}
        for (int i = 0; i < {K}; ++i) for (int j = 0; j < {K}; ++j) {{ in >> v; a_im[i][j] = from_bits<T::a_t>(v); }}
        for (int i = 0; i < {K}; ++i) for (int j = 0; j < {N}; ++j) {{ in >> v; b_re[i][j] = from_bits<T::b_t>(v); }}
        for (int i = 0; i < {K}; ++i) for (int j = 0; j < {N}; ++j) {{ in >> v; b_im[i][j] = from_bits<T::b_t>(v); }}
        cg_ref<T>(a_re, a_im, b_re, b_im, x_re, x_im, alpha, beta);
        for (int it = 0; it < {NIT}; ++it) {{
            for (int i = 0; i < {K}; ++i) for (int j = 0; j < {N}; ++j) out << bits(x_re[it][i][j]) << "\\n";
            for (int i = 0; i < {K}; ++i) for (int j = 0; j < {N}; ++j) out << bits(x_im[it][i][j]) << "\\n";
            for (int j = 0; j < {N}; ++j) out << bits(alpha[it][j]) << "\\n";
            for (int j = 0; j < {N}; ++j) out << bits(beta[it][j]) << "\\n";
        }}
    }}
    return 0;
}}
"""


def _bits(stored: np.ndarray, fmt: Format) -> list[int]:
    return [
        int(b) for b in np.atleast_1d(to_bits(np.asarray(stored).reshape(-1), fmt.W))
    ]


def build_case_set(spec: CaseSetSpec) -> dict:
    """Inputs, testbench and golden bits for one case set."""
    f = spec.formats
    inputs: list[int] = []
    expected: list[int] = []
    labels: list[str] = []
    problems = _problems(spec)
    for c, (A, B, M) in enumerate(problems):
        ar, ai, br, bi = quantize_inputs(A, B, f, scale=M)
        inputs += _bits(ar, f.A) + _bits(ai, f.A) + _bits(br, f.B) + _bits(bi, f.B)
        regs: dict[int, dict] = {}
        xs = cg_fixed(
            A, B, spec.nit, f, scale=M, explicit_residual=spec.explicit,
            iterates=range(1, spec.nit + 1), on_iteration=lambda n, r, regs=regs: regs.__setitem__(n, r),
        )  # fmt: skip
        for n in range(1, spec.nit + 1):
            for part, arr in (("X.re", xs[n].re), ("X.im", xs[n].im)):
                expected += _bits(arr, f.X)
                labels += [f"case {c} it {n} {part}[{i}]" for i in range(arr.size)]
            for name, fmt in (("alpha", f.alpha), ("beta", f.beta)):
                expected += _bits(regs[n][name][0], fmt)
                labels += [f"case {c} it {n} {name}[{j}]" for j in range(spec.N)]
    return {
        "name": spec.name,
        "spec": spec,
        "n_cases": len(problems),
        "main": render_main(spec, len(problems)),
        "inputs": "\n".join(map(str, inputs)) + "\n",
        "expected": expected,
        "labels": labels,
    }


def gen_case_set_sources(case_set: dict, work_dir: Path) -> Path:
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "main.cpp").write_text(case_set["main"], encoding="utf-8")
    (work_dir / "inputs.txt").write_text(case_set["inputs"], encoding="utf-8")
    (work_dir / "expected.json").write_text(
        json.dumps({"name": case_set["name"], "expected": case_set["expected"]}),
        encoding="utf-8",
    )
    shutil.copy(CPP_DIR / "cg_ref.h", work_dir / "cg_ref.h")
    shutil.copy(CPP_DIR / "run.tcl", work_dir / "run.tcl")
    return work_dir


def csim_and_compare(case_set: dict, work_dir: Path) -> dict:
    """Run C-sim in ``work_dir`` and compare every output bit with the golden."""
    work_dir = Path(work_dir)
    toolchain.run_vitis_hls(
        work_dir / "run.tcl", work_dir=work_dir, capture_output=True
    )
    got = [
        int(t) for t in (work_dir / "outputs.txt").read_text(encoding="utf-8").split()
    ]
    exp = case_set["expected"]
    mism = [
        {"where": case_set["labels"][i], "expected": e, "csim": g}
        for i, (e, g) in enumerate(zip(exp, got, strict=False))
        if e != g
    ]
    return {
        "name": case_set["name"],
        "n_bits_words": len(exp),
        "count_ok": len(got) == len(exp),
        "mismatches": mism,
        "exact": len(got) == len(exp) and not mism,
    }


def conformance_for_case_set(case_set: dict, work_dir: Path) -> dict:
    gen_case_set_sources(case_set, work_dir)
    return csim_and_compare(case_set, work_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--list", action="store_true", help="List the case sets and exit."
    )
    parser.add_argument("--work", default="results/conformance", help="Work directory.")
    args = parser.parse_args()
    specs = case_set_specs()
    if args.list:
        for s in specs:
            print(f"{s.name}: K={s.K} N={s.N} nit={s.nit} cases={CASES_PER_SET}+1")
        return
    root = Path(__file__).resolve().parent / args.work
    for spec in specs:
        res = conformance_for_case_set(build_case_set(spec), root / spec.name)
        status = "EXACT" if res["exact"] else f"{len(res['mismatches'])} MISMATCHES"
        print(f"{spec.name}: {res['n_bits_words']} words, {status}")


if __name__ == "__main__":
    main()
