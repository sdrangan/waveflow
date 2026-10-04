"""space.py — the hardware design space, the calibration designs and the hold-out split.

Step 5.1 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 5.0 decision record, §14).

The space
---------
One configuration is the ten knobs of :class:`HwConfig`.  ``N = 32`` is fixed, and ``nit`` is a
runtime field, not hardware.  A configuration is **valid** when ``R`` divides ``K``, the lane count
``L`` divides ``C``, and the array has at most :data:`MAX_PES` processing elements.  The full space
has 107,460 configurations.

Each block sees only some of the knobs, which is what lets a model be calibrated per block:

* the **vector unit** ``(K, L, W, g_s)`` — 225 configurations;
* the **matmul** ``(K, R, C, cmul, W, L)`` — 1,990;
* the **glue** (framer, load, store, control, the stream-of-blocks and the queues)
  ``(K, L, W, mem_dw, sob_depth, cmd_depth)`` — 1,350.

The split
---------
A **build** is one synthesized top: a ``CgVecUnit``, a ``CgMmUnit`` or a ``CgDetector``.  A unit
build fixes the knobs its block does not see at their defaults.  ``fit`` builds are designed — a
centre point, one-at-a-time sweeps of every knob, and corner points for the knobs that interact.
``holdout`` builds are drawn uniformly at random, with a fixed seed, from each space minus its
``fit`` points.  The held-out detectors are both the held-out full designs and the held-out glue
configurations.

``python -m examples.mimo_cg.hw.space --write`` writes ``paper_data/holdout_split.csv``.  It is
committed before any campaign build, and the held-out builds are run only after the models are
committed (plan steps 5.1, 5.6, 5.7).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np

from examples.mimo_cg.hw.common import (
    DEFAULT_MEM_DW,
    DEFAULT_N,
    SPACE_G,
    SPACE_W,
    format_id,
)
from examples.mimo_cg.mimo_cg import provenance, read_table, write_table
from examples.mimo_cg.mimo_link import BASE_SEED

HERE = Path(__file__).resolve().parent
SPLIT_PATH = HERE.parent / "paper_data" / "holdout_split.csv"

K_VALUES = (4, 8, 16)
L_VALUES = (1, 2, 4, 8, 16)
R_VALUES = (1, 2, 4, 8, 16)
C_VALUES = (4, 8, 16, 32)
CMULS = (3, 4)
MEM_DWS = (32, 64)
SOB_DEPTHS = (2, 3, 4)
CMD_DEPTHS = (2, 4, 8)
#: The largest systolic array, in processing elements (1,024 DSPs with 4 multiplies each).
MAX_PES = 256

TOPS = ("vec", "mm", "det")
#: Seed namespace of the held-out draws: never shared with Phase 1 (20, 30) or Phase 3 (70).
_SPLIT_STREAM = 90
#: Held-out builds per top (AC5 asks for at least 10 per block and 5 full designs).
N_HOLDOUT = {"vec": 12, "mm": 12, "det": 10}


@dataclass(frozen=True, order=True)
class HwConfig:
    """One hardware configuration: every synthesis-time knob of the detector."""

    K: int = 4
    L: int = 4
    R: int = 4
    C: int = 4
    cmul: int = 4
    W: int = 12
    g_s: int = 8
    mem_dw: int = DEFAULT_MEM_DW
    sob_depth: int = 2
    cmd_depth: int = 2

    @property
    def fmt(self) -> int:
        """The hardware format id of ``(W, g_s)``."""
        return format_id(self.W, self.g_s)

    def vec_key(self) -> tuple:
        """The knobs the vector unit sees."""
        return (self.K, self.L, self.W, self.g_s)

    def mm_key(self) -> tuple:
        """The knobs the matmul sees."""
        return (self.K, self.R, self.C, self.cmul, self.W, self.L)

    def glue_key(self) -> tuple:
        """The knobs the framer, load, store, control, blocks and queues see."""
        return (self.K, self.L, self.W, self.mem_dw, self.sob_depth, self.cmd_depth)


def is_valid(c: HwConfig) -> bool:
    """Whether ``c`` is in the design space (ranges and the three structural rules)."""
    return (
        c.K in K_VALUES
        and c.L in L_VALUES
        and c.R in R_VALUES
        and c.C in C_VALUES
        and c.cmul in CMULS
        and c.W in SPACE_W
        and c.g_s in SPACE_G
        and c.mem_dw in MEM_DWS
        and c.sob_depth in SOB_DEPTHS
        and c.cmd_depth in CMD_DEPTHS
        and c.K % c.R == 0
        and c.C % c.L == 0
        and DEFAULT_N % c.C == 0
        and c.R * c.C <= MAX_PES
    )


# --- the spaces ------------------------------------------------------------------------------


def _arch() -> list[tuple]:
    """Valid ``(K, L, R, C)`` combinations."""
    return [
        (K, L, R, C)
        for K in K_VALUES
        for L in L_VALUES
        for R in R_VALUES
        if K % R == 0
        for C in C_VALUES
        if C % L == 0 and R * C <= MAX_PES
    ]


def full_space():
    """Every valid configuration, in a fixed order (107,460 of them)."""
    for K, L, R, C in _arch():
        for cmul in CMULS:
            for W in SPACE_W:
                for g in SPACE_G:
                    for mem_dw in MEM_DWS:
                        for sob in SOB_DEPTHS:
                            for cmd in CMD_DEPTHS:
                                yield HwConfig(K, L, R, C, cmul, W, g, mem_dw, sob, cmd)


def vec_space() -> list[HwConfig]:
    """The vector unit's configurations, as unit builds: ``(K, L, W, g_s)`` vary."""
    return [
        _vec(K, L, W, g)
        for K in K_VALUES
        for L in L_VALUES
        for W in SPACE_W
        for g in SPACE_G
    ]


def mm_space() -> list[HwConfig]:
    """The matmul's configurations, as unit builds: ``(K, R, C, cmul, W, L)`` vary."""
    return [
        _mm(K, R, C, cmul, W, L)
        for K, L, R, C in _arch()
        for cmul in CMULS
        for W in SPACE_W
    ]


def glue_space() -> list[tuple]:
    """The glue's configurations, as keys ``(K, L, W, mem_dw, sob_depth, cmd_depth)``."""
    return [
        (K, L, W, mem_dw, sob, cmd)
        for K in K_VALUES
        for L in L_VALUES
        for W in SPACE_W
        for mem_dw in MEM_DWS
        for sob in SOB_DEPTHS
        for cmd in CMD_DEPTHS
    ]


# --- unit builds -----------------------------------------------------------------------------


def _vec(K: int, L: int, W: int, g: int) -> HwConfig:
    """A ``CgVecUnit`` build.  The matmul knobs are not built; they hold valid placeholders."""
    return HwConfig(K=K, L=L, R=K, C=max(4, L), cmul=4, W=W, g_s=g)


def _mm(K: int, R: int, C: int, cmul: int, W: int, L: int) -> HwConfig:
    """A ``CgMmUnit`` build.  The matmul's types do not depend on the guard, held at 8."""
    return HwConfig(K=K, L=L, R=R, C=C, cmul=cmul, W=W, g_s=8)


# --- the calibration designs -----------------------------------------------------------------


def vec_fit() -> list[HwConfig]:
    """25 vector-unit builds: the centre, one knob at a time, then the L×W×g_s and K×L corners."""
    c = {"K": 8, "L": 4, "W": 12, "g": 8}
    pts = [c]
    pts += [c | {"K": K} for K in (4, 16)]
    pts += [c | {"L": L} for L in (1, 2, 8, 16)]
    pts += [c | {"W": W} for W in (8, 10, 14, 16)]
    pts += [c | {"g": g} for g in (0, 4)]
    pts += [
        c | {"L": L, "W": W, "g": g} for L in (1, 16) for W in (8, 16) for g in (0, 8)
    ]
    pts += [c | {"K": K, "L": L} for K in (4, 16) for L in (1, 16)]
    return _unique([_vec(**p) for p in pts])


def mm_fit() -> list[HwConfig]:
    """26 matmul builds: the centre, one knob at a time, the array-size × width corners, and the
    3-multiply form at both width ends."""
    c = {"K": 8, "R": 4, "C": 8, "cmul": 4, "W": 12, "L": 4}
    pts = [c]
    pts += [c | {"K": K} for K in (4, 16)]
    pts += [c | {"R": R} for R in (1, 2, 8)]
    pts += [c | {"C": C} for C in (4, 16, 32)]
    pts += [c | {"cmul": 3}]
    pts += [c | {"W": W} for W in (8, 10, 14, 16)]
    pts += [c | {"L": L} for L in (1, 8)]
    corners = ((8, 1, 4), (8, 8, 32), (16, 16, 16), (16, 1, 32))
    pts += [
        c | {"K": K, "R": R, "C": C, "W": W} for K, R, C in corners for W in (8, 16)
    ]
    pts += [c | {"cmul": 3, "W": W} for W in (8, 16)]
    return _unique([_mm(**p) for p in pts])


def det_fit() -> list[HwConfig]:
    """16 detector builds: the Phase 4 default, one knob at a time, and two opposite corners."""
    d = HwConfig()
    pts = [d]
    pts += [replace(d, K=K, R=K) for K in (8, 16)]
    pts += [replace(d, L=L, C=max(4, L)) for L in (1, 2, 8, 16)]
    pts += [replace(d, W=W) for W in (8, 16)]
    pts += [replace(d, mem_dw=32)]
    pts += [replace(d, sob_depth=s) for s in (3, 4)]
    pts += [replace(d, cmd_depth=q) for q in (4, 8)]
    pts += [
        HwConfig(16, 16, 4, 16, 3, 16, 8, 32, 4, 8),
        HwConfig(16, 1, 1, 4, 4, 8, 0, 32, 3, 4),
    ]
    return _unique(pts)


def _unique(cfgs: list[HwConfig]) -> list[HwConfig]:
    assert len(set(cfgs)) == len(cfgs), "a calibration design repeats a configuration"
    bad = [c for c in cfgs if not is_valid(c)]
    assert not bad, f"invalid calibration configurations: {bad}"
    return cfgs


# --- the held-out draws ----------------------------------------------------------------------


def _draw(
    pool: list[HwConfig], n: int, stream: int, namespace: int = _SPLIT_STREAM
) -> list[HwConfig]:
    """``n`` configurations drawn without replacement from ``pool`` (sorted first, so the draw does
    not depend on the enumeration order), in sorted order."""
    pool = sorted(pool)
    rng = np.random.default_rng(np.random.SeedSequence([BASE_SEED, namespace, stream]))
    picks = rng.choice(len(pool), size=n, replace=False)
    return [pool[i] for i in sorted(int(i) for i in picks)]


def holdout(top: str) -> list[HwConfig]:
    """The held-out builds of one top: drawn from its space minus its ``fit`` builds."""
    fit = set(FIT[top]())
    space = {"vec": vec_space, "mm": mm_space, "det": lambda: list(full_space())}[top]()
    pool = [c for c in space if c not in fit]
    return _draw(pool, N_HOLDOUT[top], TOPS.index(top))


FIT = {"vec": vec_fit, "mm": mm_fit, "det": det_fit}


# --- the supplementary held-out set (M5 review) ----------------------------------------------

SUPPLEMENT_PATH = HERE.parent / "paper_data" / "holdout_supplement.csv"
#: Seed namespace of the supplementary draws.
_SUPPLEMENT_STREAM = 91
#: The strata the first draw left thin, in draw order: ``(name, top, how many, what belongs)``.
SUPPLEMENT_STRATA = (
    ("vec: K = 16", "vec", 2, lambda c: c.K == 16),
    ("mm: 16 lanes", "mm", 1, lambda c: c.L == 16),
    ("mm: R >= 8", "mm", 1, lambda c: c.R >= 8 and c.L < 16),
    ("det: K = 16, 16 lanes", "det", 1, lambda c: c.K == 16 and c.L == 16),
    ("det: K = 16, R >= 8", "det", 1, lambda c: c.K == 16 and c.R >= 8 and c.L < 16),
)


def supplement() -> list[tuple[str, str, HwConfig]]:
    """Six more held-out builds, as ``(stratum, top, configuration)``.

    The first draw was uniform and came out thin where the designs are largest: no K = 16
    vector unit, no 16-lane matmul, matmul arrays of at most 4 rows, and one K = 16 detector.
    This set is drawn the same way (uniformly, with a fixed seed) but inside those strata.  A
    stratum's pool leaves out the ``fit`` builds, the first held-out builds, and every
    configuration that shares a block with a ``fit`` build of another kind of top, so nothing in
    it was seen by a fit, even as part of something else.

    It is scored with the models as frozen in step 5.6, and it is evidence beside AC5, not part of
    it: AC5 is judged on the first set.
    """
    spaces = {"vec": vec_space, "mm": mm_space, "det": lambda: list(full_space())}
    key = {"vec": HwConfig.vec_key, "mm": HwConfig.mm_key}
    fit_unit = {kind: {key[kind](c) for c in FIT[kind]()} for kind in key}
    fit_det = {kind: {key[kind](c) for c in det_fit()} for kind in key}

    def unseen(top: str, c: HwConfig) -> bool:
        if top == "det":
            return not any(key[kind](c) in fit_unit[kind] for kind in key)
        return key[top](c) not in fit_det[top]

    out = []
    for i, (name, top, n, belongs) in enumerate(SUPPLEMENT_STRATA):
        taken = (
            set(FIT[top]()) | set(holdout(top)) | {c for _s, t, c in out if t == top}
        )
        pool = [
            c for c in spaces[top]() if belongs(c) and c not in taken and unseen(top, c)
        ]
        out += [(name, top, c) for c in _draw(pool, n, i, _SUPPLEMENT_STREAM)]
    return out


def supplement_rows() -> list[dict]:
    return [
        {
            "build": label(top, c),
            "top": top,
            "role": "supplement",
            "stratum": name,
            **asdict(c),
        }
        for name, top, c in supplement()
    ]


def write_supplement(path: Path = SUPPLEMENT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_table(
        path,
        supplement_rows(),
        provenance("holdout_supplement", stream=_SUPPLEMENT_STREAM),
    )
    return path


def read_supplement(
    path: Path = SUPPLEMENT_PATH,
) -> list[tuple[str, str, str, HwConfig]]:
    """The committed supplementary set, as ``(build, top, role, configuration)``; empty if absent."""
    if not Path(path).is_file():
        return []
    knobs = list(HwConfig.__dataclass_fields__)
    return [
        (r["build"], r["top"], r["role"], HwConfig(**{k: int(r[k]) for k in knobs}))
        for r in read_table(path)
    ]


# --- the split file --------------------------------------------------------------------------


def label(top: str, c: HwConfig) -> str:
    """The build's name: the top and the knobs that top sees."""
    if top == "vec":
        return f"vec_k{c.K}_l{c.L}_w{c.W}g{c.g_s}"
    if top == "mm":
        return f"mm_k{c.K}_r{c.R}_c{c.C}_m{c.cmul}_w{c.W}_l{c.L}"
    return (
        f"det_k{c.K}_l{c.L}_r{c.R}_c{c.C}_m{c.cmul}_w{c.W}g{c.g_s}"
        f"_d{c.mem_dw}_s{c.sob_depth}_q{c.cmd_depth}"
    )


def split_rows() -> list[dict]:
    """Every build of the campaign: ``fit`` builds in design order, then ``holdout`` builds."""
    rows = []
    for role, source in (("fit", lambda top: FIT[top]()), ("holdout", holdout)):
        for top in TOPS:
            for c in source(top):
                rows.append(
                    {"build": label(top, c), "top": top, "role": role, **asdict(c)}
                )
    return rows


def write_split(path: Path = SPLIT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    comment = provenance(
        "holdout_split", stream=_SPLIT_STREAM, space=sum(1 for _ in full_space())
    )
    write_table(path, split_rows(), comment)
    return path


def read_split(path: Path = SPLIT_PATH) -> list[tuple[str, str, str, HwConfig]]:
    """The committed split, as ``(build, top, role, configuration)``."""
    knobs = list(HwConfig.__dataclass_fields__)
    return [
        (r["build"], r["top"], r["role"], HwConfig(**{k: int(r[k]) for k in knobs}))
        for r in read_table(path)
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help=f"write {SPLIT_PATH.name}")
    ap.add_argument(
        "--write-supplement", action="store_true", help=f"write {SUPPLEMENT_PATH.name}"
    )
    args = ap.parse_args(argv)
    if args.write_supplement:
        for r in supplement_rows():
            print(f"  {r['stratum']:24s} {r['build']}")
        print("wrote", write_supplement())
        return 0
    rows = split_rows()
    print(
        f"spaces: vec {len(vec_space())}, mm {len(mm_space())}, glue {len(glue_space())}, "
        f"full {sum(1 for _ in full_space())}"
    )
    for role in ("fit", "holdout"):
        counts = {
            t: sum(r["top"] == t and r["role"] == role for r in rows) for t in TOPS
        }
        print(f"{role}: {counts}")
    if args.write:
        print("wrote", write_split())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
