#!/usr/bin/env python3
"""Compare the Vitis GEMV outputs against the Python model, bit for bit.

Reads whatever ``results/`` contains and reports honestly on what is missing rather than
pretending a skipped stage passed.  Three DUTs x two Vitis stages, plus C-sim against co-sim:

    output_f32_{csim,cosim}.txt      the 5-arg float overload
    output_ab_{csim,cosim}.txt       the alpha/beta overload
    output_fixed_{csim,cosim}.txt    ap_fixed -- expected to be WRONG, see below

Comparisons are on raw bit patterns, never decimals.  A ``%.17g`` round-trip absorbs exactly the
1-ULP differences this whole exercise exists to detect.

Exit code 0 only if every comparison that could run did run and passed.

    python verify.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MatrixVectorMul_bitexact.wf_gemv.fixed import (
    OMode,
    QMode,
    fixed_format,
    gemv_fixed_as_shipped,
)
from MatrixVectorMul_bitexact.wf_gemv.gemv import f32_bits, gemv, gemv_ab

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def _bits_f32(pattern: int) -> np.float32:
    return np.float32(struct.unpack("<f", struct.pack("<I", int(pattern)))[0])


def _payload(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


# --- inputs -----------------------------------------------------------------------------------
def read_f32_input():
    lines = _payload(HERE / "data" / "input_f32.txt")
    n_case, m, n = (int(v) for v in lines[0].split())
    vals, pos, cases = [int(v) for v in lines[1:]], 0, []
    for _ in range(n_case):
        a = np.array([_bits_f32(v) for v in vals[pos:pos + m * n]],
                     dtype=np.float32).reshape(m, n); pos += m * n
        x = np.array([_bits_f32(v) for v in vals[pos:pos + n]], dtype=np.float32); pos += n
        cases.append((a, x))
    return m, n, cases


def read_ab_input():
    lines = _payload(HERE / "data" / "input_ab.txt")
    n_case, m, n, n_ab = (int(v) for v in lines[0].split())
    vals, pos, cases = [int(v) for v in lines[1:]], 0, []
    for _ in range(n_case):
        a = np.array([_bits_f32(v) for v in vals[pos:pos + m * n]],
                     dtype=np.float32).reshape(m, n); pos += m * n
        x = np.array([_bits_f32(v) for v in vals[pos:pos + n]], dtype=np.float32); pos += n
        y = np.array([_bits_f32(v) for v in vals[pos:pos + m]], dtype=np.float32); pos += m
        cases.append((a, x, y))
    ab = []
    for _ in range(n_ab):
        ab.append((_bits_f32(vals[pos]), _bits_f32(vals[pos + 1]))); pos += 2
    return m, n, cases, ab


def read_fixed_input():
    lines = _payload(HERE / "data" / "input_fixed.txt")
    w, i, n_case, m, n = (int(v) for v in lines[0].split())
    body, cases = lines[1:], []
    for k in range(n_case):
        block = body[k * (m + 1):(k + 1) * (m + 1)]
        mat = np.array([[int(v) for v in row.split()] for row in block[:m]], dtype=np.int64)
        vec = np.array([int(v) for v in block[m].split()], dtype=np.int64)
        cases.append((mat, vec))
    return w, i, m, n, cases


# --- expected values from the model ------------------------------------------------------------
def expected_f32(logp: int) -> dict:
    _m, _n, cases = read_f32_input()
    out = {}
    for k, (a, x) in enumerate(cases):
        for r, v in enumerate(gemv(a, x, par_entries=1 << logp)):
            out[(k, r)] = f32_bits(v)
    return out


def expected_ab(logp: int) -> dict:
    _m, _n, cases, ab = read_ab_input()
    out = {}
    for t, (alpha, beta) in enumerate(ab):
        for k, (a, x, y) in enumerate(cases):
            for r, v in enumerate(gemv_ab(a, x, y, alpha, beta, par_entries=1 << logp)):
                out[(t, k, r)] = f32_bits(v)
    return out


def expected_fixed(logp: int) -> dict:
    w, i, _m, _n, cases = read_fixed_input()
    # The DUT is the UNPATCHED library, so the model to compare against is the as-shipped one.
    # Default template modes: ap_fixed<W,I> is AP_TRN / AP_WRAP.
    fmt = fixed_format(w, i, QMode.AP_TRN, OMode.AP_WRAP)
    # logParEntries is accepted and ignored: dot_dsp accumulates in index order at any stream
    # width, unlike the float path where it sets the tree shape.  Measured, not assumed --
    # tests/test_gemv_fixed.py pins it.
    del logp
    out = {}
    for k, (mat, vec) in enumerate(cases):
        for r, v in enumerate(gemv_fixed_as_shipped(mat, vec, fmt)):
            out[(k, r)] = int(v) & ((1 << w) - 1)
    return out


# --- outputs ------------------------------------------------------------------------------------
def read_output(path: Path, n_key: int, base: int) -> dict:
    rows = {}
    for ln in _payload(path)[1:]:
        parts = ln.split()
        rows[tuple(int(v) for v in parts[:n_key])] = int(parts[n_key], base)
    return rows


def read_logp(path: Path, index: int) -> int | None:
    """The DUT's logParEntries, taken from the output header rather than assumed.

    It is part of the numerical contract on the float path -- five stream widths give four
    distinct answers -- so a model run at the wrong one would silently disagree.

    Returns None for a file that is empty or has a short header.  That is not a hypothetical: an
    aborted ``cosim_design`` leaves a **zero-byte** output behind, and a partly-run Vitis flow is
    the normal state when something has gone wrong.  The script has to say so rather than
    traceback over it.
    """
    rows = _payload(path)
    if not rows:
        return None
    fields = rows[0].split()
    return int(fields[index]) if len(fields) > index else None


def compare(label: str, got: dict, want: dict, fmt_width: int) -> bool:
    missing = set(want) - set(got)
    extra = set(got) - set(want)
    if missing or extra:
        print(f"  {RED}FAIL{RESET} {label}: {len(missing)} expected rows absent, "
              f"{len(extra)} unexpected")
        return False
    bad = [(k, got[k], want[k]) for k in sorted(want) if got[k] != want[k]]
    if bad:
        print(f"  {RED}FAIL{RESET} {label}: {len(bad)}/{len(want)} rows differ")
        for k, g, w in bad[:6]:
            print(f"        {k}: vitis 0x{g:0{fmt_width}x}  model 0x{w:0{fmt_width}x}")
        return False
    print(f"  {GREEN}PASS{RESET} {label}: {len(want)}/{len(want)} rows bit-exact")
    return True


def fma_verdict() -> bool:
    """⚠️ The question this whole package exists to answer: does HLS fuse axpy's multiply-add?

    ``axpy`` writes ``p_alpha * l_realX + l_realY`` as one expression (axpy.hpp:71).  A fused
    multiply-add keeps the product's full precision and rounds once; a separate multiply and add
    round twice.  Native builds differ by 25 of 576 rows depending on the compiler flag, so this
    is not academic -- and only synthesis can say which one the hardware does.

    This reports the verdict AND whether the data could have produced the other one.  A check
    that cannot fail proves nothing: if no row separates the two readings, "matches unfused" is
    an accident of the data rather than a finding, and this says so.
    """
    path = RESULTS / "output_ab_cosim.txt"
    if not path.exists() or not _payload(path):
        print(f"  {YELLOW}SKIP{RESET} FMA verdict: no co-simulation output")
        return True
    m, _n, cases, ab = read_ab_input()
    logp = read_logp(path, 4)
    got = read_output(path, 3, 10)
    unfused = expected_ab(logp)
    fused = {}
    for t, (alpha, beta) in enumerate(ab):
        for k, (a, x, y) in enumerate(cases):
            dot_r = gemv(a, x, par_entries=1 << logp)
            for r in range(m):
                scaled = np.float32(np.float32(beta) * y[r])
                fused[(t, k, r)] = f32_bits(
                    np.float32(np.float64(alpha) * np.float64(dot_r[r]) + np.float64(scaled)))
    sep = sum(fused[k] != unfused[k] for k in unfused)
    hit_u = sum(got[k] == unfused[k] for k in unfused)
    hit_f = sum(got[k] == fused[k] for k in fused)
    n = len(unfused)
    print("\nFMA verdict (the reason this package exists):")
    print(f"  rows on which the two readings differ at all : {sep}/{n}")
    print(f"  RTL matches the UNFUSED (separate mul + add) : {hit_u}/{n}")
    print(f"  RTL matches the FUSED (single rounding)      : {hit_f}/{n}")
    if sep == 0:
        print(f"  {YELLOW}INCONCLUSIVE{RESET}: this data cannot tell the two apart -- "
              f"enlarge it rather than reading anything into the match above.")
        return False
    if hit_u == n and hit_f < n:
        print(f"  {GREEN}=> Vitis HLS does NOT fuse it.{RESET}  The model's unfused reading is "
              f"what the hardware does.")
        return True
    if hit_f == n and hit_u < n:
        print(f"  {RED}=> Vitis HLS DOES fuse it.{RESET}  wf_gemv.gemv.axpy models the wrong "
              f"reading and must be changed.")
        return False
    print(f"  {RED}=> Neither reading matches the RTL.{RESET}  Something else is going on.")
    return False


DUTS = [
    # name, key length, output radix, hex digits, expected-value fn, logP field in the header
    ("f32", 2, 10, 8, expected_f32, 3),
    ("ab", 3, 10, 8, expected_ab, 4),
    ("fixed", 2, 16, 4, expected_fixed, 5),
]


def main() -> int:
    ok, ran, skipped = True, 0, []
    for name, n_key, base, width, expected, logp_field in DUTS:
        print(f"\n{name}:")
        stages, logp = {}, None
        for stage in ("csim", "cosim"):
            path = RESULTS / f"output_{name}_{stage}.txt"
            if not path.exists():
                skipped.append(f"{name}/{stage}")
                print(f"  {YELLOW}SKIP{RESET} {stage}: {path.name} not present")
                continue
            this_logp = read_logp(path, logp_field)
            if this_logp is None:
                ok = False
                print(f"  {RED}FAIL{RESET} {stage}: {path.name} is empty or truncated "
                      f"({path.stat().st_size} bytes) -- the Vitis stage that writes it did not "
                      f"finish.  Check logs/run.log.")
                continue
            logp = this_logp
            stages[stage] = read_output(path, n_key, base)
        if not stages:
            continue
        want = expected(logp)
        for stage, got in stages.items():
            ok &= compare(f"{stage} vs Python model", got, want, width)
            ran += 1
        if len(stages) == 2:
            same = stages["csim"] == stages["cosim"]
            print(f"  {GREEN + 'PASS' + RESET if same else RED + 'FAIL' + RESET} "
                  f"csim vs cosim: {'identical' if same else 'DIFFER'}")
            ok &= same
            ran += 1

    ok &= fma_verdict()

    print()
    if skipped:
        print(f"{YELLOW}Not run:{RESET} {', '.join(skipped)} "
              f"-- run `vitis-run --mode hls --tcl run.tcl` first.")
    if ran == 0:
        print(f"{RED}Nothing was compared.{RESET}")
        return 1
    print(f"{GREEN + 'ALL COMPARISONS PASSED' + RESET if ok else RED + 'FAILURES ABOVE' + RESET} "
          f"({ran} comparisons)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
