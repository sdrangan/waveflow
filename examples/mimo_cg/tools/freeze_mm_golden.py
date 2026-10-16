"""freeze_mm_golden.py — golden vectors of the example's matrix multiply, for ``waveflow.linalg``.

Step 7.2 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  The example's ``mm_step`` computes
``S = q_S(A @ P)``, exact before one rounding.  This script extracts the tree of commit
``6a2cdca`` (the end of Phase 6) into a temporary directory, runs that commit's ``mm_step`` there
on every format set of the Phase 5 space and the stress set, at K = 4, 8 and 16, and writes the
operands and results to ``tests/linalg/data/mm_golden.npz`` together with the commit it ran.
``tests/linalg/test_matmul_model.py`` holds ``waveflow.linalg.matmul`` to the file, which stays
valid after Phase 9 moves the example onto that model.

The sixteen format sets share five distinct ``(A, P, S)`` format triples (the scalars' formats,
where the sets differ, do not reach the matrix multiply), so the cases are drawn per triple and
the file maps every set to its triple.  Each triple and K has four kinds of case: ``small``
(nothing saturates), ``mid`` (about a fifth of the results saturate), ``full`` (operands over the
whole range, most results saturate) and ``extreme`` (operands from ``-2^(W-1)``, ``-2^(W-1)+1``,
``-1``, ``0``, ``1``, ``2^(W-1)-1``).

    python examples/mimo_cg/tools/freeze_mm_golden.py           # write the file
    python examples/mimo_cg/tools/freeze_mm_golden.py --check   # regenerate and compare
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
SOURCE_COMMIT = "6a2cdca"
OUT = REPO / "tests" / "linalg" / "data" / "mm_golden.npz"
SCRIPT = "examples/mimo_cg/tools/freeze_mm_golden.py"
#: The paths of the source commit the generator imports.
PATHS = ("waveflow", ":(glob)examples/mimo_cg/*.py", ":(glob)examples/mimo_cg/hw/*.py")

# Runs inside the extracted tree (cwd), with only that tree on PYTHONPATH.
_CHILD = r"""
import json, platform, sys
from pathlib import Path
import numpy as np
import waveflow
import examples.mimo_cg.mimo_cg_fixed as F
import examples.mimo_cg.hw.common as C

root = Path.cwd().resolve()
for mod in (waveflow, F, C):
    assert Path(mod.__file__).resolve().is_relative_to(root), mod.__file__
out, commit, script = sys.argv[1:4]

SEED, N, KS = 20261006, 32, (4, 8, 16)
KINDS = ("small", "mid", "full", "extreme")
sets = C.hw_formats()


def spec(f):
    return [f.W, f.int_bits, int(f.signed), f.q_mode.value, f.o_mode.value]


triples, set_triple, first = [], {}, {}
for name, fm in sets.items():
    t = [spec(fm.A), spec(fm.P), spec(fm.S)]
    if t not in triples:
        triples.append(t)
        first[len(triples) - 1] = fm
    set_triple[name] = triples.index(t)


def draw(rng, W, kind, K, shape):
    lo, hi = -(1 << (W - 1)), (1 << (W - 1)) - 1
    if kind == "full":
        return rng.integers(lo, hi + 1, size=shape, dtype=np.int64)
    if kind == "extreme":
        return rng.choice(np.array([lo, lo + 1, -1, 0, 1, hi], np.int64), size=shape)
    # A in +-4t and P in +-8t (their integer bits); "mid" puts the std of S at 12 of S's 16.
    t = 1 / 8 if kind == "small" else (0.633 / K) ** 0.25
    v = np.rint(rng.uniform(-1.0, 1.0, size=shape) * t * 2.0 ** (W - 1))
    return np.clip(v, lo, hi).astype(np.int64)


arrays, cases = {}, []
for ti, (fa, fp, fs) in enumerate(triples):
    for K in KS:
        for ki, kind in enumerate(KINDS):
            rng = np.random.default_rng([SEED, ti, K, ki])
            ar, ai = draw(rng, fa[0], kind, K, (K, K)), draw(rng, fa[0], kind, K, (K, K))
            pr, pi = draw(rng, fp[0], kind, K, (K, N)), draw(rng, fp[0], kind, K, (K, N))
            sr, si = F.mm_step(ar, ai, pr, pi, first[ti])
            key = f"t{ti}_k{K}_{kind}"
            for tag, (re, im) in (("a", (ar, ai)), ("b", (pr, pi)), ("c", (sr, si))):
                arrays[f"{key}_{tag}"] = np.stack([re, im]).astype(np.int16)
            rail = 1 << (fs[0] - 1)
            n_rail = int(np.sum((sr == -rail) | (sr == rail - 1)))
            n_rail += int(np.sum((si == -rail) | (si == rail - 1)))
            cases.append({"key": key, "triple": ti, "K": K, "N": N, "kind": kind,
                          "n_rail": n_rail, "n_out": 2 * K * N})

meta = {
    "source_commit": commit,
    "function": "examples.mimo_cg.mimo_cg_fixed.mm_step (S = q_S(A @ P))",
    "script": script,
    "python": platform.python_version(),
    "numpy": np.__version__,
    "seed": SEED,
    "layout": "<key>_a (2, K, K), <key>_b (2, K, N), <key>_c (2, K, N): [re, im] stored integers",
    "format_spec": "[W, I, signed, q_mode, o_mode] for a = A, b = P, c = S",
    "triples": [{"a": a, "b": b, "c": c} for a, b, c in triples],
    "sets": set_triple,
    "cases": cases,
}
arrays["meta"] = np.array(json.dumps(meta, indent=1))
np.savez_compressed(out, **arrays)
"""


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=REPO, check=True, capture_output=True
    ).stdout


def generate(out: Path) -> str:
    """Run the source commit's ``mm_step`` and write ``out``; returns the full commit hash."""
    commit = (
        _git("rev-parse", "--verify", f"{SOURCE_COMMIT}^{{commit}}").decode().strip()
    )
    tar = _git("archive", "--format=tar", commit, "--", *PATHS)
    with tempfile.TemporaryDirectory(prefix="mm_golden_") as tmp:
        with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
            tf.extractall(tmp, filter="data")
        env = {**os.environ, "PYTHONPATH": tmp}
        subprocess.run(
            [sys.executable, "-c", _CHILD, str(out.resolve()), commit, SCRIPT],
            cwd=tmp,
            env=env,
            check=True,
        )
    return commit


def load(path: Path) -> tuple[dict, dict[str, np.ndarray]]:
    with np.load(path, allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files}
    return json.loads(str(arrays.pop("meta"))), arrays


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="regenerate and compare")
    args = ap.parse_args(argv)
    if not args.check:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        commit = generate(OUT)
        meta, _ = load(OUT)
        print(
            f"wrote {OUT.relative_to(REPO)}: {len(meta['cases'])} cases, "
            f"{len(meta['sets'])} format sets, {len(meta['triples'])} triples, "
            f"from {commit}"
        )
        for kind in ("small", "mid", "full", "extreme"):
            rail = sum(c["n_rail"] for c in meta["cases"] if c["kind"] == kind)
            outs = sum(c["n_out"] for c in meta["cases"] if c["kind"] == kind)
            print(f"  {kind:8s} {rail:6d} of {outs:6d} results at a rail")
        return 0
    with tempfile.TemporaryDirectory(prefix="mm_golden_check_") as tmp:
        fresh = Path(tmp) / "mm_golden.npz"
        generate(fresh)
        meta_new, new = load(fresh)
    meta_old, old = load(OUT)
    bad = [
        k
        for k in sorted(set(old) | set(new))
        if k not in old or k not in new or not np.array_equal(old[k], new[k])
    ]
    for key in ("python", "numpy"):
        meta_old.pop(key), meta_new.pop(key)
    if meta_old != meta_new:
        bad.append("meta")
    print("mm_golden.npz: " + ("reproduced" if not bad else f"differs in {bad}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
