"""ltbf_conformance.py -- the fixed-point LTBF CG vs its C++ reference, bit for bit, in Vitis C-sim.

Step 2.3 of ``plans/mimo_cg/ltbf_cg_dse.md`` (AC3), on ``examples/mimo_cg/mimo_cg_conformance.py``'s
pattern: generate, C-sim, compare every bit.

* **Generate.**  For each case set, Python writes ``main.cpp``: a traits struct of ``ap_fixed``
  typedefs derived from :class:`~examples.ltbf_cg.ltbf_fixed.LtbfFormats` (registers, plus the
  exact accumulators) around the hand-written ``src/ltbf_ref.h``, and ``inputs.txt``: each
  case's ``H_alpha`` and ``B`` as the stored integers :func:`~examples.ltbf_cg.ltbf_fixed.solve_fixed`
  starts from.
* **C-sim.**  ``src/run.tcl`` runs Vitis HLS C-simulation for ``xczu48dr-ffvg1517-2-e``.
* **Compare.**  Every stored bit after every iteration: ``X`` (re and im), ``alpha`` and ``beta``.
  If Python and C++ ever differ, find which is wrong; never loosen the comparison.

A case set is one (regime, ``Q`` form, size, format); each holds :data:`CASES_PER_SET` random
problems with the link model's structure (per-UE gains over the SINR range, SRS noise) and one
zero-divisor case (column 2 of ``B`` zeroed: ``rz = ps = 0`` takes both guards).  The formats are
:func:`~examples.ltbf_cg.ltbf_fixed.formats_for` over the set's own range profile; the stress set
shrinks the profile so the registers saturate.

Run from the repo root::

    python -m examples.ltbf_cg.ltbf_conformance --list
    python -m examples.ltbf_cg.ltbf_conformance           # every case set (needs Vitis)
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import numpy as np

from examples.ltbf_cg import ltbf_fixed as lf
from examples.ltbf_cg import ltbf_link as lk
from examples.mimo_cg.hw.common import ap_type
from waveflow.linalg import cg as lcg
from waveflow.linalg.matmul import acc_format
from waveflow.toolchain import toolchain
from waveflow.utils.fixputils import Format, to_bits

SRC_DIR = Path(__file__).resolve().parent / "src"

#: Random problems per case set (AC3 asks for at least 20 per regime and form); plus one
#: zero-divisor case.
CASES_PER_SET = 20
#: Iterations per case: every t of the sweep.
NIT = 10
#: The column of ``B`` the zero-divisor case zeroes.
ZERO_COL = 2


@dataclass(frozen=True)
class CaseSetSpec:
    name: str
    regime: int  # 1: B = own SRS blocks; 2: B = I
    form: str  # "explicit" | "factored"
    n_rx: int
    n_conn: int  # connected UEs: H_alpha has 8 n_conn columns
    n_own: int  # own UEs: B has 8 n_own columns in regime 1
    W: int
    guard: int
    seed: int
    range_scale: float = 1.0  # < 1 shrinks the profile: the stress set saturates

    @property
    def cols(self) -> int:
        return lk.N_SRS * self.n_conn

    @property
    def m(self) -> int:
        return lk.N_SRS * self.n_own if self.regime == 1 else self.n_rx


def case_set_specs() -> list[CaseSetSpec]:
    """Both regimes x both forms at N_rx = 16 and 16 bits, then larger, wider, narrower and
    saturating sets (N_rx <= 64)."""
    specs = [
        CaseSetSpec(
            f"r{reg}_{form}_n16_w16", reg, form, 16, 6, 2, 16, 1, 100 + 10 * reg + i
        )
        for reg in (1, 2)
        for i, form in enumerate(("explicit", "factored"))
    ]
    specs += [
        CaseSetSpec("r1_factored_n64_w12", 1, "factored", 64, 30, 2, 12, 1, 201),
        CaseSetSpec("r2_explicit_n32_w24", 2, "explicit", 32, 12, 2, 24, 1, 202),
        CaseSetSpec("r1_explicit_n64_w10_g0", 1, "explicit", 64, 10, 2, 10, 0, 203),
        CaseSetSpec("r2_factored_n32_w10_g0", 2, "factored", 32, 12, 2, 10, 0, 204),
        CaseSetSpec(
            "r1_factored_n16_w12_sat", 1, "factored", 16, 6, 2, 12, 0, 205, 0.125
        ),
    ]
    return specs


def _random_problem(
    spec: CaseSetSpec, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """``(H_alpha, B)`` with the link model's structure: per-UE SNR in [-6, 14] dB over the array,
    SRS noise at the nominal processing gain, ``H_alpha = [H_1 .. H_U] / sqrt(N_SRS)``.
    """
    snr = 10.0 ** (0.1 * rng.uniform(-6.0, 14.0, size=spec.n_conn))
    shape = (spec.n_rx, lk.N_SRS)
    sigma2 = 10.0 ** (-0.1 * lk.G_SRS_DB)
    blocks = []
    for g in snr:
        h = np.sqrt(g / spec.n_rx / 2) * (
            rng.standard_normal(shape) + 1j * rng.standard_normal(shape)
        )
        h += np.sqrt(sigma2 / 2) * (
            rng.standard_normal(shape) + 1j * rng.standard_normal(shape)
        )
        blocks.append(h)
    ha = np.hstack(blocks) / np.sqrt(lk.N_SRS)
    if spec.regime == 1:
        b = np.hstack(blocks[: spec.n_own])
    else:
        b = np.eye(spec.n_rx, dtype=complex)
    return ha, b


def problems(spec: CaseSetSpec) -> list[tuple[np.ndarray, np.ndarray]]:
    """The set's random problems, then its zero-divisor case."""
    rng = np.random.default_rng(spec.seed)
    out = [_random_problem(spec, rng) for _ in range(CASES_PER_SET)]
    ha, b = _random_problem(spec, rng)
    b = b.copy()
    b[:, ZERO_COL] = 0
    out.append((ha, b))
    return out


@cache
def case_set_formats(spec: CaseSetSpec) -> lf.LtbfFormats:
    """``formats_for(W, profile, guard)`` over the set's own problems."""
    ranges = {k: 0.0 for k in lf.VECTORS + lf.SCALARS}
    for ha, b in problems(spec):
        for k, v in lf.profile_ranges(ha, b, NIT).items():
            ranges[k] = max(ranges[k], v)
    ranges = {k: v * spec.range_scale for k, v in ranges.items()}
    f = lf.formats_for(spec.W, ranges, spec.guard)
    lf.check_widths(f, spec.n_rx, spec.cols)
    return f


def type_map(f: lf.LtbfFormats, n_rx: int, cols: int) -> dict[str, Format]:
    """Every register and exact-accumulator type :file:`ltbf_ref.h` needs, by C++ name."""
    c = f.cg
    acc = lcg.accumulator_formats(c, n_rx)
    return {
        "h_t": f.H, "q_t": f.Q, "t_t": f.T, "b_t": c.B, "p_t": c.P, "r_t": c.R, "s_t": c.S,
        "x_t": c.X, "ps_t": c.ps, "rz_t": c.rz, "alpha_t": c.alpha, "beta_t": c.beta,
        "rzw_t": acc["rzw"],
        "acc_hh_t": acc_format(f.H, f.H, cols), "acc_qp_t": acc_format(f.Q, c.P, n_rx),
        "acc_hp_t": acc_format(f.H, c.P, n_rx), "acc_ht_t": acc_format(f.H, f.T, cols),
        "dot_ps_t": acc["dot_ps"], "dot_rz_t": acc["dot_rz"],
    }  # fmt: skip


def render_main(spec: CaseSetSpec, f: lf.LtbfFormats, n_cases: int) -> str:
    """The C-sim testbench: traits typedefs around ``ltbf_ref.h``, reading and writing stored bits."""
    typedefs = "\n".join(
        f"    typedef {ap_type(v)} {k};"
        for k, v in type_map(f, spec.n_rx, spec.cols).items()
    )
    N, C, M = spec.n_rx, spec.cols, spec.m
    return f"""// GENERATED by examples/ltbf_cg/ltbf_conformance.py -- do not edit.
#include <ap_fixed.h>
#include <ap_int.h>
#include <fstream>
#include "ltbf_ref.h"

struct Cfg {{
    static const int N = {N}, COLS = {C}, M = {M}, NIT = {NIT};
    static const bool EXPLICIT = {"true" if spec.form == "explicit" else "false"};
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

static Cfg::h_t h_re[{N}][{C}], h_im[{N}][{C}];
static Cfg::b_t b_re[{N}][{M}], b_im[{N}][{M}];
static Cfg::x_t x_re[{NIT}][{N}][{M}], x_im[{NIT}][{N}][{M}];
static Cfg::alpha_t alpha[{NIT}][{M}];
static Cfg::beta_t beta[{NIT}][{M}];

int main(int argc, char** argv) {{
    std::ifstream in(argv[1]);
    std::ofstream out(argv[2]);
    unsigned long long v;
    for (int c = 0; c < {n_cases}; ++c) {{
        for (int i = 0; i < {N}; ++i) for (int j = 0; j < {C}; ++j) {{ in >> v; h_re[i][j] = from_bits<Cfg::h_t>(v); }}
        for (int i = 0; i < {N}; ++i) for (int j = 0; j < {C}; ++j) {{ in >> v; h_im[i][j] = from_bits<Cfg::h_t>(v); }}
        for (int i = 0; i < {N}; ++i) for (int j = 0; j < {M}; ++j) {{ in >> v; b_re[i][j] = from_bits<Cfg::b_t>(v); }}
        for (int i = 0; i < {N}; ++i) for (int j = 0; j < {M}; ++j) {{ in >> v; b_im[i][j] = from_bits<Cfg::b_t>(v); }}
        ltbf_ref<Cfg>(h_re, h_im, b_re, b_im, x_re, x_im, alpha, beta);
        for (int it = 0; it < {NIT}; ++it) {{
            for (int i = 0; i < {N}; ++i) for (int j = 0; j < {M}; ++j) out << bits(x_re[it][i][j]) << "\\n";
            for (int i = 0; i < {N}; ++i) for (int j = 0; j < {M}; ++j) out << bits(x_im[it][i][j]) << "\\n";
            for (int j = 0; j < {M}; ++j) out << bits(alpha[it][j]) << "\\n";
            for (int j = 0; j < {M}; ++j) out << bits(beta[it][j]) << "\\n";
        }}
    }}
    return 0;
}}
"""


def _bits(stored: np.ndarray, fmt: Format) -> list[int]:
    return [
        int(b) for b in np.atleast_1d(to_bits(np.asarray(stored).reshape(-1), fmt.W))
    ]


def golden(spec: CaseSetSpec, ha: np.ndarray, b: np.ndarray) -> dict[int, dict]:
    """The stored registers after each iteration: ``{n: {"xr", "xi", "alpha", "beta"}}``."""
    regs: dict[int, dict] = {}

    def hook(n, state, scalars):
        regs[n] = {
            "xr": state.xr, "xi": state.xi,
            "alpha": scalars["alpha"][0], "beta": scalars["beta"][0],
        }  # fmt: skip

    lf.solve_fixed(ha, b, case_set_formats(spec), NIT, spec.form, on_iteration=hook)
    return regs


def build_case_set(spec: CaseSetSpec) -> dict:
    """Inputs, testbench and golden bits for one case set."""
    f = case_set_formats(spec)
    c = f.cg
    inputs: list[int] = []
    expected: list[int] = []
    labels: list[str] = []
    probs = problems(spec)
    for k, (ha, b) in enumerate(probs):
        hr, hi = lf.to_stored(ha, f.H)
        br, bi = lf.to_stored(b, c.B)
        inputs += _bits(hr, f.H) + _bits(hi, f.H) + _bits(br, c.B) + _bits(bi, c.B)
        regs = golden(spec, ha, b)
        for n in range(1, NIT + 1):
            for part in ("xr", "xi"):
                expected += _bits(regs[n][part], c.X)
                labels += [
                    f"case {k} it {n} {part}[{i}]" for i in range(regs[n][part].size)
                ]
            for name, fmt in (("alpha", c.alpha), ("beta", c.beta)):
                expected += _bits(regs[n][name], fmt)
                labels += [f"case {k} it {n} {name}[{j}]" for j in range(spec.m)]
    return {
        "name": spec.name,
        "spec": spec,
        "n_cases": len(probs),
        "main": render_main(spec, f, len(probs)),
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
    shutil.copy(SRC_DIR / "ltbf_ref.h", work_dir / "ltbf_ref.h")
    shutil.copy(SRC_DIR / "run.tcl", work_dir / "run.tcl")
    return work_dir


def csim_and_compare(case_set: dict, work_dir: Path) -> dict:
    """Run C-sim in ``work_dir`` and compare every output bit with the golden."""
    work_dir = Path(work_dir)
    toolchain.run_vitis_hls(
        work_dir / "run.tcl", work_dir=work_dir, capture_output=True
    )
    out = work_dir / "outputs.txt"
    got = (
        [int(t) for t in out.read_text(encoding="utf-8").split()]
        if out.is_file()
        else []
    )
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
    parser.add_argument(
        "--only", default=None, help="Run only the case set of this name."
    )
    parser.add_argument("--work", default="results/conformance", help="Work directory.")
    args = parser.parse_args()
    specs = case_set_specs()
    if args.list:
        for s in specs:
            print(
                f"{s.name}: N_rx={s.n_rx} cols={s.cols} M={s.m} nit={NIT} "
                f"cases={CASES_PER_SET}+1 {case_set_formats(s).name}"
            )
        return
    root = Path(__file__).resolve().parent / args.work
    for spec in specs:
        if args.only and spec.name != args.only:
            continue
        res = conformance_for_case_set(build_case_set(spec), root / spec.name)
        status = "EXACT" if res["exact"] else f"{len(res['mismatches'])} MISMATCHES"
        print(f"{spec.name}: {res['n_bits_words']} words, {status}", flush=True)


if __name__ == "__main__":
    main()
