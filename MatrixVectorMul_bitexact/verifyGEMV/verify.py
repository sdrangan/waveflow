#!/usr/bin/env python3
"""Compare every Vitis GEMV output against the Python model, bit for bit.

Nine DUTs, each existing because some claim about the kernel could not be checked without
synthesized RTL.  For each: C-simulation vs the model, co-simulation vs the model, and C-sim vs
co-sim.  Co-simulation *is* the RTL simulation -- Vitis has no separate RTL-sim step for an
``ap_ctrl_hs`` kernel.

Reads whatever ``results/`` contains and reports honestly on what is missing rather than
pretending a skipped stage passed.  Exit code 0 only if every comparison that could run passed.

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
from MatrixVectorMul_bitexact.wf_gemv.gemv import f32_bits, gemv, gemv_ab, gemv_int

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
GREEN, RED, YELLOW, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"


def _payload(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _f32(p: int) -> np.float32:
    return np.float32(struct.unpack("<f", struct.pack("<I", int(p)))[0])


def _f64(p: int) -> np.float64:
    return np.float64(struct.unpack("<d", struct.pack("<Q", int(p)))[0])


def _b64(d) -> int:
    return struct.unpack("<Q", struct.pack("<d", np.float64(d)))[0]


def read_cases(name: str, conv, dtype):
    """The shared ``n_cases M N`` input format: per case, an M*N matrix then an N vector."""
    lines = _payload(HERE / "data" / f"input_{name}.txt")
    n_case, m, n = (int(v) for v in lines[0].split())
    vals, pos, cases = [int(v) for v in lines[1:]], 0, []
    for _ in range(n_case):
        a = np.array([conv(v) for v in vals[pos:pos + m * n]], dtype=dtype).reshape(m, n)
        pos += m * n
        x = np.array([conv(v) for v in vals[pos:pos + n]], dtype=dtype)
        pos += n
        cases.append((a, x))
    assert pos == len(vals), f"input_{name}.txt has {len(vals) - pos} trailing values"
    return m, n, cases


def read_ab_input():
    lines = _payload(HERE / "data" / "input_ab.txt")
    n_case, m, n, n_ab = (int(v) for v in lines[0].split())
    vals, pos, cases = [int(v) for v in lines[1:]], 0, []
    for _ in range(n_case):
        a = np.array([_f32(v) for v in vals[pos:pos + m * n]],
                     dtype=np.float32).reshape(m, n); pos += m * n
        x = np.array([_f32(v) for v in vals[pos:pos + n]], dtype=np.float32); pos += n
        y = np.array([_f32(v) for v in vals[pos:pos + m]], dtype=np.float32); pos += m
        cases.append((a, x, y))
    ab = []
    for _ in range(n_ab):
        ab.append((_f32(vals[pos]), _f32(vals[pos + 1]))); pos += 2
    return m, n, cases, ab


def read_output(path: Path, n_key: int, base: int) -> dict:
    rows = {}
    for ln in _payload(path)[1:]:
        parts = ln.split()
        rows[tuple(int(v) for v in parts[:n_key])] = int(parts[n_key], base)
    return rows


def read_header(path: Path, index: int) -> int | None:
    """A field of the output header, taken from the file rather than assumed.

    Returns None for an empty or truncated file.  Not hypothetical: an aborted ``cosim_design``
    leaves a **zero-byte** output behind, and a partly-run flow is the normal state when
    something has gone wrong -- the script has to say so rather than traceback over it.
    """
    rows = _payload(path)
    if not rows:
        return None
    f = rows[0].split()
    return int(f[index]) if len(f) > index else None


# --- expected values, one function per DUT family ----------------------------------------------
def _float_expected(name: str, logp: int, dtype) -> dict:
    conv = _f32 if dtype is np.float32 else _f64
    bits = f32_bits if dtype is np.float32 else _b64
    _m, _n, cases = read_cases(name, conv, dtype)
    return {(k, r): bits(v)
            for k, (a, x) in enumerate(cases)
            for r, v in enumerate(gemv(a, x, par_entries=1 << logp, dtype=dtype))}


def _int_expected(name: str, width: int, signed: bool) -> dict:
    """The integer DUTs are compared as VALUES, not stored bits.

    ``int32_t`` and ``ap_uint<32>`` are the same hardware and emit the same bits, so a bit-level
    comparison passes for a signed model and an unsigned one alike -- the ``u32`` DUT would prove
    nothing.  The decimal value is the only place the two differ.
    """
    _m, _n, cases = read_cases(name, int, object)
    return {(k, r): int(v)
            for k, (a, x) in enumerate(cases)
            for r, v in enumerate(gemv_int(a, x, width=width, signed=signed))}


def _fixed_expected(name: str, w: int, i: int) -> dict:
    """The DUT is the UNPATCHED library, so the model to compare against is the as-shipped one."""
    _m, _n, cases = read_cases(name, int, object)
    fmt = fixed_format(w, i, QMode.AP_TRN, OMode.AP_WRAP)   # ap_fixed<W,I> template defaults
    mask = (1 << w) - 1
    return {(k, r): int(v) & mask
            for k, (a, x) in enumerate(cases)
            for r, v in enumerate(gemv_fixed_as_shipped(np.array(a.tolist(), dtype=np.int64),
                                                        np.array(x.tolist(), dtype=np.int64), fmt))}


def _ab_expected(logp: int) -> dict:
    _m, _n, cases, ab = read_ab_input()
    return {(t, k, r): f32_bits(v)
            for t, (alpha, beta) in enumerate(ab)
            for k, (a, x, y) in enumerate(cases)
            for r, v in enumerate(gemv_ab(a, x, y, alpha, beta, par_entries=1 << logp))}


#: name, what it settles, key length, output radix, hex digits, header field holding logP,
#: and a callable taking that logP and returning {key: expected bits}
DUTS = [
    ("f32", "dot_tree survives to RTL", 2, 10, 8, 3,
     lambda lp: _float_expected("f32", lp, np.float32)),
    ("f32_wide", "a different stream width (P=8)", 2, 10, 8, 3,
     lambda lp: _float_expected("f32_wide", lp, np.float32)),
    ("f32_pad", "the beat-count PADDING path", 2, 10, 8, 3,
     lambda lp: _float_expected("f32_pad", lp, np.float32)),
    ("f64", "double -- AdderDelay 8, not 4", 2, 10, 16, 3,
     lambda lp: _float_expected("f64", lp, np.float64)),
    ("ab", "does HLS fuse axpy's alpha*x + y?", 3, 10, 8, 4, _ab_expected),
    ("i32", "dot_dsp, signed -- never synthesized before", 2, 10, 8, 4,
     lambda lp: _int_expected("int", 32, True)),
    ("u32", "the same bits read as UNSIGNED", 2, 10, 8, 4,
     lambda lp: _int_expected("int", 32, False)),
    ("fixed", "the ap_fixed defect -- real silicon?", 2, 16, 4, 4,
     lambda lp: _fixed_expected("fixed", 16, 8)),
    ("fix24", "ap_fixed<24,12> -- WideType slot is 32 bits in csim, 24 in RTL", 2, 16, 6, 4,
     lambda lp: _fixed_expected("fix24", 24, 12)),
]


def compare(label: str, got: dict, want: dict, width: int) -> bool:
    missing, extra = set(want) - set(got), set(got) - set(want)
    if missing or extra:
        print(f"    {RED}FAIL{RESET} {label}: {len(missing)} expected rows absent, "
              f"{len(extra)} unexpected")
        return False
    bad = [(k, got[k], want[k]) for k in sorted(want) if got[k] != want[k]]
    if bad:
        print(f"    {RED}FAIL{RESET} {label}: {len(bad)}/{len(want)} rows differ")
        for k, g, w in bad[:6]:
            print(f"          {k}: vitis 0x{g:0{width}x}  model 0x{w:0{width}x}")
        return False
    print(f"    {GREEN}PASS{RESET} {label}: {len(want)}/{len(want)} rows bit-exact")
    return True


def fma_verdict() -> bool:
    """⚠️ The question the ab DUT exists to answer: does HLS fuse axpy's multiply-add?

    Reports the verdict AND whether the data could have produced the other one.  A check that
    cannot fail proves nothing: if no row separates the two readings, "matches unfused" is an
    accident of the data rather than a finding, and this says so.
    """
    path = RESULTS / "output_ab_cosim.txt"
    if not path.exists() or not _payload(path):
        print(f"  {YELLOW}SKIP{RESET} FMA verdict: no co-simulation output")
        return True
    m, _n, cases, ab = read_ab_input()
    logp = read_header(path, 4)
    got, unfused, fused = read_output(path, 3, 10), _ab_expected(logp), {}
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
    print(f"\n{BOLD}FMA verdict{RESET} (the ab DUT's reason for existing):")
    print(f"  rows on which the two readings differ at all : {sep}/{n}")
    print(f"  RTL matches the UNFUSED (separate mul + add) : {hit_u}/{n}")
    print(f"  RTL matches the FUSED (single rounding)      : {hit_f}/{n}")
    if sep == 0:
        print(f"  {YELLOW}INCONCLUSIVE{RESET}: this data cannot tell the two apart -- enlarge it "
              f"rather than reading anything into the match above.")
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


def sign_verdict() -> bool:
    """⚠️ The question the u32 DUT exists to answer -- and it nearly went unasked.

    ``int32_t`` and ``ap_uint<32>`` are the same hardware and emit the same **bits**.  The first
    version of this package compared the integer DUTs bit-for-bit, which passes for a signed model
    and an unsigned one alike: the u32 DUT proved nothing.  The testbenches now emit the decimal
    VALUE, which is the only place the two differ.

    Reports the verdict and whether the data could have produced the other one, for the same
    reason ``fma_verdict`` does.
    """
    path = RESULTS / "output_u32_cosim.txt"
    if not path.exists() or not _payload(path):
        print(f"  {YELLOW}SKIP{RESET} sign verdict: no co-simulation output")
        return True
    got = read_output(path, 2, 10)
    uns, sgn = _int_expected("int", 32, False), _int_expected("int", 32, True)
    sep = sum(uns[k] != sgn[k] for k in uns)
    hit_u = sum(got[k] == uns[k] for k in uns)
    hit_s = sum(got[k] == sgn[k] for k in sgn)
    n = len(uns)
    print(f"\n{BOLD}Signed/unsigned verdict{RESET} (the u32 DUT's reason for existing):")
    print(f"  rows on which the two readings differ at all : {sep}/{n}")
    print(f"  RTL matches the UNSIGNED reading             : {hit_u}/{n}")
    print(f"  RTL matches the SIGNED reading               : {hit_s}/{n}")
    if sep == 0:
        print(f"  {YELLOW}INCONCLUSIVE{RESET}: no row has its top bit set, so this data cannot "
              f"tell a signed model from an unsigned one.")
        return False
    if hit_u == n and hit_s < n:
        print(f"  {GREEN}=> ap_uint<32> really is read unsigned.{RESET}  gemv_int(signed=False) "
              f"is what the hardware means.")
        return True
    print(f"  {RED}=> The unsigned reading does not match the RTL.{RESET}")
    return False


def main() -> int:
    ok, ran, skipped = True, 0, []
    for name, why, n_key, base, width, logp_field, expected in DUTS:
        print(f"\n{BOLD}{name}{RESET}  ({why})")
        stages, logp = {}, None
        for stage in ("csim", "cosim"):
            path = RESULTS / f"output_{name}_{stage}.txt"
            if not path.exists():
                skipped.append(f"{name}/{stage}")
                print(f"    {YELLOW}SKIP{RESET} {stage}: {path.name} not present")
                continue
            this = read_header(path, logp_field)
            if this is None:
                ok = False
                print(f"    {RED}FAIL{RESET} {stage}: {path.name} is empty or truncated "
                      f"({path.stat().st_size} bytes) -- the Vitis stage that writes it did not "
                      f"finish.  Check logs/run.log.")
                continue
            logp = this
            stages[stage] = read_output(path, n_key, base)
        if not stages:
            continue
        want = expected(logp)
        for stage, got in stages.items():
            ok &= compare(f"{stage} vs Python model", got, want, width)
            ran += 1
        if len(stages) == 2:
            same = stages["csim"] == stages["cosim"]
            print(f"    {GREEN + 'PASS' + RESET if same else RED + 'FAIL' + RESET} "
                  f"csim vs cosim: {'identical' if same else 'DIFFER'}")
            ok &= same
            ran += 1

    ok &= fma_verdict()
    ok &= sign_verdict()

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
