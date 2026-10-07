"""freeze_cg_golden.py — golden vectors of the example's CG vector step, for ``waveflow.linalg``.

Step 8.1a of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  The example's ``cg_init`` sets up the CG
state from ``B`` and ``vec_step`` advances it by one iteration from ``S = A·P`` (register steps
2–9: the column dots, the two guarded divisions, the updates of ``X``, ``R`` and ``P``).  This
script extracts the tree of commit ``6a2cdca`` (the end of Phase 6) into a temporary directory,
runs that commit's functions there, and writes their inputs and outputs to
``tests/linalg/data/cg_golden.npz`` together with the commit it ran, before the model moves into
Waveflow (gate 8.0).

Format sets: the sixteen of the Phase 5 space (the fifteen ``W{W}g{g}`` and the saturation stress
set) and ``wide()``, at K = 4, 8 and 16 where every intermediate fits 64 bits.  Problems: the
conformance generator's (16-QAM over Rayleigh channels, M from 32 to 128, quantized with scale 64),
three random ones and the zero-residual one, whose column 2 of ``B`` is zero so that both divisions
take the zero guard; N = 4 columns.  Per set and K, the cases are:

* ``init``: ``cg_init`` on ``B``;
* ``it1`` and ``itK``: ``vec_step`` at the first and the last iteration of a CG run (recurrence);
* ``rand``: ``vec_step`` on a state drawn over each register's whole range, so the updates of
  ``X``, ``R`` and ``P`` saturate;
* ``x1`` and ``xK`` (sets ``W12g8``, ``W14g8``, ``stress`` and ``wide``): the first and last
  iteration of a run in the explicit-residual form, ``R = B − A·X``.

Every value is a stored integer, saved in the narrowest of int16, int32 and int64 that holds the
array (``wide()``'s ``ps`` is 34 bits); readers widen to int64.

    python -m examples.mimo_cg.tools.freeze_cg_golden           # write the file
    python -m examples.mimo_cg.tools.freeze_cg_golden --check   # regenerate and compare
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
OUT = REPO / "tests" / "linalg" / "data" / "cg_golden.npz"
SCRIPT = "examples/mimo_cg/tools/freeze_cg_golden.py"
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
import examples.mimo_cg.mimo_cg_conformance as CF

root = Path.cwd().resolve()
for mod in (waveflow, F, C, CF):
    assert Path(mod.__file__).resolve().is_relative_to(root), mod.__file__
out, commit, script = sys.argv[1:4]

SEED, N, KS, SCALE = 20261007, 4, (4, 8, 16), 64.0
N_PROBLEMS = 3  # random problems before the zero-residual one
EXPLICIT_SETS = ("W12g8", "W14g8", "stress", "wide")
sets = {**C.hw_formats(), "wide": F.CgFormats.wide()}
REGS = ("A", "B", "P", "R", "S", "X", "ps", "rz", "alpha", "beta")


def spec(f):
    return [f.W, f.int_bits, int(f.signed), f.q_mode.value, f.o_mode.value]


def problems(fmt, K, seed):
    probs = CF._problems(CF.CaseSetSpec("golden", fmt, K, N, K, False, seed))
    keep = probs[:N_PROBLEMS] + probs[-1:]  # the last is the zero-residual problem
    A = np.stack([p[0] for p in keep])
    B = np.stack([p[1] for p in keep])
    return F.quantize_inputs(A, B, fmt, SCALE)


def explicit_hook(ar, ai, br, bi, f):
    # As cg_fixed builds it: the exact B - A X, before the quantization to R.
    def residual(xr, xi):
        axr, axi, ax_fmt = F._matmul(ar, ai, f.A, xr, xi, f.X)
        return F._addsub(F.fx.sub, br, bi, f.B, axr, axi, ax_fmt)
    return residual


def state_arrays(prefix, s):
    return {f"{prefix}_x": np.stack([s.xr, s.xi]), f"{prefix}_r": np.stack([s.rr, s.ri]),
            f"{prefix}_p": np.stack([s.pr, s.pi]), f"{prefix}_rz": s.rz}


def rails(v, fmt):
    lo, hi = -(1 << (fmt.W - 1)), (1 << (fmt.W - 1)) - 1
    return int(np.sum((v == lo) | (v == hi)))


def draw(rng, fmt, shape, nonneg=False):
    lo = 0 if nonneg else -(1 << (fmt.W - 1))
    return rng.integers(lo, (1 << (fmt.W - 1)), size=shape, dtype=np.int64)


groups, cases = {}, []  # (K, kind) -> {"sets": [...], field: [array per set]}


def put(K, kind, name, fields):
    g = groups.setdefault((K, kind), {"sets": []})
    g["sets"].append(name)
    for k, v in fields.items():
        g.setdefault(k, []).append(v)


def record(name, K, kind, case, state_in, sr, si, state_out, scalars, f, extra=None):
    fields = {**state_arrays("in", state_in), "s": np.stack([sr, si]),
              **state_arrays("out", state_out),
              **{k: scalars[k] for k in ("ps", "alpha", "beta")}, **(extra or {})}
    put(K, kind, name, fields)
    cases.append({
        **case, "group": f"k{K}_{kind}", "set": name,
        "ps_zero": int(np.sum(scalars["ps"] == 0)), "rz_in_zero": int(np.sum(state_in.rz == 0)),
        "alpha_rail": rails(scalars["alpha"], f.alpha), "beta_rail": rails(scalars["beta"], f.beta),
        "xrp_rail": rails(state_out.xr, f.X) + rails(state_out.rr, f.R) + rails(state_out.pr, f.P),
    })


skipped = []
for si_, (name, f) in enumerate(sets.items()):
    for K in KS:
        try:
            f.intermediates(K)
        except NotImplementedError as e:
            skipped.append({"set": name, "K": K, "why": str(e)})
            continue
        ar, ai, br, bi = problems(f, K, 900 + K)
        s0 = F.cg_init(br, bi, f)
        put(K, "init", name, {"b": np.stack([br, bi]), **state_arrays("out", s0)})
        cases.append({"group": f"k{K}_init", "set": name, "K": K, "kind": "init", "form": "",
                      "n": 0, "ps_zero": 0, "rz_in_zero": 0, "alpha_rail": 0, "beta_rail": 0,
                      "xrp_rail": 0})
        forms = [("recurrence", None, "it")]
        if name in EXPLICIT_SETS:
            forms.append(("explicit", explicit_hook(ar, ai, br, bi, f), "x"))
        for form, hook, tag in forms:
            state = s0
            for n in range(1, K + 1):
                sr, si = F.mm_step(ar, ai, state.pr, state.pi, f)
                new, scalars = F.vec_step(state, sr, si, f, residual=hook)
                if n in (1, K):
                    kind = f"{tag}{'1' if n == 1 else 'K'}"
                    extra = None
                    if hook is not None:
                        extra = {"a": np.stack([ar, ai]), "b": np.stack([br, bi])}
                    record(name, K, kind, {"K": K, "kind": kind, "form": form, "n": n},
                           state, sr, si, new, scalars, f, extra)
                state = new
        # A state over each register's whole range: the updates saturate.
        rng = np.random.default_rng([SEED, si_, K])
        shape, col = (len(ar), K, N), (len(ar), 1, N)
        rand_in = F.CgState(draw(rng, f.X, shape), draw(rng, f.X, shape), draw(rng, f.R, shape),
                            draw(rng, f.R, shape), draw(rng, f.P, shape), draw(rng, f.P, shape),
                            draw(rng, f.rz, col, nonneg=True))
        sr, si = draw(rng, f.S, shape), draw(rng, f.S, shape)
        new, scalars = F.vec_step(rand_in, sr, si, f)
        record(name, K, "rand", {"K": K, "kind": "rand", "form": "recurrence", "n": 0},
               rand_in, sr, si, new, scalars, f)


def narrowest(v):
    # The smallest signed integer type that holds every value; readers widen to int64.
    v = np.asarray(v, dtype=np.int64)
    for dt in (np.int16, np.int32):
        info = np.iinfo(dt)
        if v.size == 0 or (v.min() >= info.min and v.max() <= info.max):
            return v.astype(dt)
    return v


arrays, group_sets = {}, {}
for (K, kind), g in groups.items():
    group_sets[f"k{K}_{kind}"] = g.pop("sets")
    for field, values in g.items():
        arrays[f"k{K}_{kind}_{field}"] = narrowest(np.stack(values))
meta = {
    "source_commit": commit,
    "functions": "examples.mimo_cg.mimo_cg_fixed.cg_init and vec_step",
    "script": script,
    "python": platform.python_version(),
    "numpy": np.__version__,
    "seed": SEED,
    "N": N,
    "scale": SCALE,
    "layout": ("one array per group k<K>_<kind> and field, stacked over the group's format sets "
               "(group_sets gives their order): init has b (S, 2, P, K, N) and out_{x,r,p} "
               "(S, 2, P, K, N), out_rz (S, P, 1, N); a vec_step group has in_{x,r,p,rz}, s, "
               "out_{x,r,p,rz} and ps, alpha, beta (S, P, 1, N); the explicit groups x1, xK add "
               "a (S, 2, P, K, K) and b, the operands of the residual hook B - A X.  S: format sets, "
               "P: problems (the last is the zero-residual one)"),
    "group_sets": group_sets,
    "format_spec": "[W, I, signed, q_mode, o_mode]",
    "sets": {name: {**{r: spec(getattr(f, r)) for r in REGS}, "g_div": f.g_div}
             for name, f in sets.items()},
    "explicit_sets": list(EXPLICIT_SETS),
    "skipped": skipped,
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
    """Run the source commit's ``cg_init`` and ``vec_step`` and write ``out``; returns the full
    commit hash."""
    commit = (
        _git("rev-parse", "--verify", f"{SOURCE_COMMIT}^{{commit}}").decode().strip()
    )
    tar = _git("archive", "--format=tar", commit, "--", *PATHS)
    with tempfile.TemporaryDirectory(prefix="cg_golden_") as tmp:
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
        steps = [c for c in meta["cases"] if c["kind"] != "init"]
        print(
            f"wrote {OUT.relative_to(REPO)}: {len(meta['cases'])} cases "
            f"({len(steps)} vec_step), {len(meta['sets'])} format sets, from {commit}; "
            f"skipped {[(s['set'], s['K']) for s in meta['skipped']]}"
        )
        for key in ("ps_zero", "rz_in_zero", "alpha_rail", "beta_rail", "xrp_rail"):
            print(f"  {key:10s} {sum(c[key] for c in steps):6d}")
        return 0
    with tempfile.TemporaryDirectory(prefix="cg_golden_check_") as tmp:
        fresh = Path(tmp) / "cg_golden.npz"
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
    print("cg_golden.npz: " + ("reproduced" if not bad else f"differs in {bad}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
