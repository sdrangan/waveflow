#!/usr/bin/env python3
"""Compare the C-simulation and co-simulation output against the vendor golden.

Raw stored integers throughout; a decimal comparison would absorb the 1-LSB differences this
whole package exists to detect.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def rows(p: Path) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in
            (ln.split() for ln in p.read_text().splitlines() if ln.strip())]


def main() -> int:
    want = rows(HERE / "data" / "golden_output.txt")
    ok = True
    seen: dict[str, list] = {}
    for tag in ("csim", "cosim"):
        p = HERE / "results" / f"output_{tag}.txt"
        if not p.exists():
            print(f"  SKIP {tag}: {p.name} absent — run build.py and vitis-run first")
            continue
        got = seen[tag] = rows(p)
        if len(got) != len(want):
            print(f"  FAIL {tag}: {len(got)} values, golden has {len(want)}")
            ok = False
            continue
        bad = sum(1 for g, w in zip(got, want) if g != w)
        print(f"  {'PASS' if bad == 0 else 'FAIL'} {tag} vs golden: "
              f"{len(want) - bad}/{len(want)} values bit-exact")
        ok &= bad == 0
    if {"csim", "cosim"} <= seen.keys():
        same = seen["csim"] == seen["cosim"]
        print(f"  {'PASS' if same else 'FAIL'} csim vs cosim: "
              f"{'identical' if same else 'DIFFER'}")
        ok &= same
    print("ALL BIT-EXACT" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
