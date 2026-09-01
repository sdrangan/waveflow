#!/usr/bin/env python3
"""Compare the Vitis SSR FFT against the Python bit-exact model.

Reads the files this folder's Vitis run produced and checks three things:

  1. C-simulation  == Python model      (the maths matches)
  2. Co-simulation == Python model      (the *synthesized RTL* matches)
  3. C-simulation  == Co-simulation     (C and RTL agree with each other)

Check 3 needs no Python model, so it runs at any transform length.  Checks 1 and 2 need a model
for this L; the model currently covers L=16 only, and this script says so plainly rather than
comparing the wrong thing or crashing.

Everything is compared as raw stored integers.  A float comparison would hide exactly the
1-LSB differences this exists to detect, so nothing here converts to decimal except the
optional human-readable dump.

Usage (from this directory, with the repo venv active)::

    python verify.py                 # compare, print a per-vector table
    python verify.py --show 3        # also dump vector 3 sample by sample
    python verify.py --quiet         # exit code only, for scripting

Exit status is 0 only if every comparison is bit-exact.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))          # repo root, so fft_bitexact/ imports

from fft_bitexact.wf_fft.fft import NO_SCALING, fft_general
from waveflow.utils import fixputils as fp

IN_W, IN_I = 16, 2        # must match src/fft_top.hpp
TW_W, TW_I = 18, 2

#: Transform lengths the Python model implements: any ``L = 4^S``.  ``fft_general`` covers the
#: whole family; sizes that are not a power of the radix (32, 128, 512) take a different
#: "forked" architecture in the library and are NOT covered.  See ../PLAN.md "S6".
MODEL_LENGTHS = {4 ** s: fft_general for s in range(2, 8)}


def _read_input(path: Path) -> tuple[np.ndarray, np.ndarray, int, int]:
    nums = [ln.split() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    n_vec, n_samp = int(nums[0][0]), int(nums[0][1])
    body = np.array([[int(a), int(b)] for a, b in nums[1:]], dtype=np.int64)
    if body.shape[0] != n_vec * n_samp:
        raise SystemExit(f"{path}: header says {n_vec}x{n_samp} but found {body.shape[0]} samples")
    return body[:, 0].reshape(n_vec, n_samp), body[:, 1].reshape(n_vec, n_samp), n_vec, n_samp


def _read_output(path: Path) -> tuple[np.ndarray, np.ndarray, int, int]:
    if not path.exists():
        raise SystemExit(f"missing {path} -- run the Vitis flow first (see README.md)")
    nums = [ln.split() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    out_w, out_i, n_vec, n_samp = (int(x) for x in nums[0])
    body = np.array([[int(a), int(b)] for a, b in nums[1:]], dtype=np.int64)
    return body[:, 0].reshape(n_vec, n_samp), body[:, 1].reshape(n_vec, n_samp), out_w, out_i


def _signed(bits: np.ndarray, w: int) -> np.ndarray:
    return np.where(bits >= (1 << (w - 1)), bits - (1 << w), bits)


def _model(in_re: np.ndarray, in_im: np.ndarray, out_w: int) -> tuple[np.ndarray, np.ndarray]:
    """Run the Python model over every vector, returning stored-bit arrays."""
    length = in_re.shape[1]
    fn = MODEL_LENGTHS[length]
    re_out = np.zeros_like(in_re)
    im_out = np.zeros_like(in_im)
    for v in range(in_re.shape[0]):
        r, i, fmt = fn(_signed(in_re[v], IN_W), _signed(in_im[v], IN_W), length,
                       IN_W, IN_I, TW_W, TW_I, mode=NO_SCALING)
        if fmt.W != out_w:
            raise SystemExit(f"model output width {fmt.W} != Vitis {out_w}")
        re_out[v] = np.asarray(fp.to_bits(np.asarray(r, dtype=np.int64), out_w))
        im_out[v] = np.asarray(fp.to_bits(np.asarray(i, dtype=np.int64), out_w))
    return re_out, im_out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", type=int, metavar="V", help="dump vector V sample by sample")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    args = ap.parse_args()

    in_re, in_im, n_vec, n_samp = _read_input(HERE / "data" / "input.txt")
    csim_re, csim_im, out_w, out_i = _read_output(HERE / "results" / "output_csim.txt")
    cosim_re, cosim_im, _, _ = _read_output(HERE / "results" / "output_cosim.txt")
    have_model = n_samp in MODEL_LENGTHS
    checks = [("C-sim   vs Co-sim      ", csim_re, csim_im, cosim_re, cosim_im)]
    mdl_re = mdl_im = None
    if have_model:
        mdl_re, mdl_im = _model(in_re, in_im, out_w)
        checks = [("C-sim   vs Python model", csim_re, csim_im, mdl_re, mdl_im),
                  ("Co-sim  vs Python model", cosim_re, cosim_im, mdl_re, mdl_im)] + checks

    if not args.quiet:
        print(f"input   : {n_vec} vectors x {n_samp} samples, ap_fixed<{IN_W},{IN_I}>")
        print(f"output  : ap_fixed<{out_w},{out_i}>  (raw stored integers)")
        if not have_model:
            print(f"\n  NOTE: the Python model does not implement L={n_samp} "
                  f"(it covers L = 4^S: {sorted(MODEL_LENGTHS)}).")
            print("        Running the C-sim vs Co-sim check only -- that one needs no model,")
            print("        and still proves synthesis preserved the C++ behaviour exactly.")
        print()
        width = max(len(name.strip()) for name, *_ in checks)
        head = " | ".join(name.strip().ljust(width) for name, *_ in checks)
        print(f"  vec | {head}")
        print("  ----+-" + "-+-".join("-" * width for _ in checks))
        for v in range(n_vec):
            cells = []
            for _, ar, ai, br, bi in checks:
                bad = int((ar[v] != br[v]).sum() + (ai[v] != bi[v]).sum())
                cells.append(("exact" if bad == 0 else f"{bad} DIFFER").center(width))
            print(f"  {v:3d} | " + " | ".join(cells))
        print()

    failed = 0
    for name, ar, ai, br, bi in checks:
        bad = int((ar != br).sum() + (ai != bi).sum())
        total = ar.size + ai.size
        failed += bad
        if not args.quiet:
            print(f"  {name}: {'BIT-EXACT' if bad == 0 else f'{bad}/{total} DIFFER'}  "
                  f"({total} values)")

    if args.show is not None and not have_model:
        print(f"\n  --show needs the Python model, which does not implement L={n_samp}.")
    elif args.show is not None:
        v = args.show
        print(f"\n  vector {v}, sample by sample (stored ints, then real value)")
        lsb = 2.0 ** -(out_w - out_i)
        print("    n |      C-sim re/im     |      model re/im     |  model as real")
        for n in range(n_samp):
            mr, mi = int(mdl_re[v, n]), int(mdl_im[v, n])
            fr = _signed(np.array([mr]), out_w)[0] * lsb
            fi = _signed(np.array([mi]), out_w)[0] * lsb
            flag = "" if (csim_re[v, n], csim_im[v, n]) == (mr, mi) else "   <-- DIFFERS"
            print(f"   {n:2d} | {csim_re[v,n]:9d} {csim_im[v,n]:9d} | {mr:9d} {mi:9d} |"
                  f" {fr:+10.5f} {fi:+10.5f}{flag}")

    if not args.quiet:
        print("\n" + ("ALL COMPARISONS BIT-EXACT" if failed == 0
                      else f"FAILED: {failed} differing values"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
