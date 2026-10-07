"""The frozen golden vectors of the CG vector step (``tests/linalg/data/cg_golden.npz``).

Written by ``examples/mimo_cg/tools/freeze_cg_golden.py`` from commit ``6a2cdca`` before the CG
model moved into Waveflow (gate 8.0).  Here: the file reproduces from its commit, it covers what
gate 8.0 asked of it, and the example's ``cg_init`` and ``vec_step`` replay every case, through the
public API only, so the replay holds whichever module the functions live in.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from examples.mimo_cg.mimo_cg_fixed import CgFormats, CgState, cg_init, vec_step
from examples.mimo_cg.tools import freeze_cg_golden as FG
from waveflow.linalg.matmul import matmul_exact
from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format, OMode, QMode

REGS = ("A", "B", "P", "R", "S", "X", "ps", "rz", "alpha", "beta")


@pytest.fixture(scope="module")
def golden():
    with np.load(FG.OUT, allow_pickle=False) as z:
        arrays = {k: z[k].astype(np.int64) for k in z.files if k != "meta"}
        meta = json.loads(str(z["meta"]))
    return meta, arrays


def _formats(spec: dict) -> CgFormats:
    def fmt(s):
        W, I, signed, q, o = s
        return Format(W, I, bool(signed), QMode(q), OMode(o))

    return CgFormats(**{r: fmt(spec[r]) for r in REGS}, g_div=spec["g_div"])


def _state(arrays: dict, prefix: str, j: int) -> CgState:
    x, r, p = (arrays[f"{prefix}_{k}"][j] for k in "xrp")
    return CgState(x[0], x[1], r[0], r[1], p[0], p[1], arrays[f"{prefix}_rz"][j])


def _hook(a, b, f: CgFormats):
    """The explicit residual ``B - A X``, exact, as the example's ``cg_fixed`` builds it."""

    def residual(xr, xi):
        axr, axi, ax_fmt = matmul_exact(a[0], a[1], f.A, xr, xi, f.X)
        re, fmt = fx.sub(b[0], f.B, axr, ax_fmt)
        im, _ = fx.sub(b[1], f.B, axi, ax_fmt)
        return re, im, fmt

    return residual


def test_golden_reproduces_from_its_commit():
    assert FG.main(["--check"]) == 0


def test_golden_coverage(golden):
    meta, _ = golden
    assert meta["source_commit"].startswith("6a2cdca") and not meta["skipped"]
    names = {f"W{W}g{g}" for W in (8, 10, 12, 14, 16) for g in (0, 4, 8)}
    assert set(meta["sets"]) == names | {"stress", "wide"}
    kinds = {(c["set"], c["K"], c["kind"]) for c in meta["cases"]}
    for name in meta["sets"]:
        for K in (4, 8, 16):
            for kind in ("init", "it1", "itK", "rand"):
                assert (name, K, kind) in kinds
            if name in meta["explicit_sets"]:
                assert {(name, K, "x1"), (name, K, "xK")} <= kinds
    steps = [c for c in meta["cases"] if c["kind"] != "init"]
    for key in ("ps_zero", "rz_in_zero", "alpha_rail", "beta_rail", "xrp_rail"):
        assert sum(c[key] for c in steps) > 0, key  # every guard and rail is exercised


def test_example_replays_every_case(golden):
    meta, arrays = golden
    for group, names in meta["group_sets"].items():
        kind = group.split("_", 1)[1]
        for j, name in enumerate(names):
            f = _formats(meta["sets"][name])
            if kind == "init":
                b = arrays[f"{group}_b"][j]
                got = cg_init(b[0], b[1], f)
                want = _state(arrays, f"{group}_out", j)
                for k in ("xr", "xi", "rr", "ri", "pr", "pi", "rz"):
                    assert np.array_equal(getattr(got, k), getattr(want, k)), (
                        group,
                        name,
                    )
                continue
            hook = None
            if kind in ("x1", "xK"):
                hook = _hook(arrays[f"{group}_a"][j], arrays[f"{group}_b"][j], f)
            s = arrays[f"{group}_s"][j]
            got, scalars = vec_step(
                _state(arrays, f"{group}_in", j), s[0], s[1], f, residual=hook
            )
            want = _state(arrays, f"{group}_out", j)
            for k in ("xr", "xi", "rr", "ri", "pr", "pi", "rz"):
                assert np.array_equal(getattr(got, k), getattr(want, k)), (
                    group,
                    name,
                    k,
                )
            for k in ("ps", "alpha", "beta"):
                assert np.array_equal(scalars[k], arrays[f"{group}_{k}"][j]), (group, k)
