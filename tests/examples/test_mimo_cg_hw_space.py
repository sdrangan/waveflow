"""Step 5.1 of plans/mimo_cg/mimo_cg_paper_sims.md: the hardware design space and the hold-out split.

The spaces have the sizes of the gate 5.0 decision record (§14): 225 vector-unit, 1,990 matmul, 1,350
glue and 107,460 full configurations.  The split has 25 / 26 / 16 ``fit`` and 12 / 12 / 10 ``holdout``
vector-unit / matmul / detector builds, every build valid, none in both roles, and the committed
``paper_data/holdout_split.csv`` is what the code regenerates, byte for byte.

The hardware format registry now holds the 15 formats of the space (ids 0–2 unchanged), and the
Python simulation of the detector and of both units is bit-exact at K = 4 for each of them.
"""

from __future__ import annotations

from collections import Counter

import pytest

from examples.mimo_cg.hw import space
from examples.mimo_cg.hw.common import (
    ALL_FORMAT_NAMES,
    HW_FORMAT_NAMES,
    LANE_BITS,
    SPACE_G,
    SPACE_W,
    check_lane_formats,
    format_id,
    format_wg,
    hw_format,
    hw_formats,
)
from examples.mimo_cg.hw.detector import CgDetectorSim, detector_problems
from examples.mimo_cg.hw.mm import CgMmUnitSim
from examples.mimo_cg.hw.vec import CgVecUnitSim
from examples.mimo_cg.mimo_cg_accuracy_sweep import sweep_format
from examples.mimo_cg.mimo_cg_conformance import CaseSetSpec, _problems

N = 32
SPACE_FORMATS = [f"W{W}g{g}" for W in SPACE_W for g in SPACE_G]


# --- the format registry ---------------------------------------------------------------------


def test_format_registry_keeps_the_gate_ids_and_adds_the_space():
    assert ALL_FORMAT_NAMES[:3] == HW_FORMAT_NAMES == ("W12g8", "W14g8", "stress")
    assert len(ALL_FORMAT_NAMES) == 16 == len(set(ALL_FORMAT_NAMES))
    assert set(ALL_FORMAT_NAMES) == {*SPACE_FORMATS, "stress"}
    assert list(hw_formats()) == list(ALL_FORMAT_NAMES)
    for W in SPACE_W:
        for g in SPACE_G:
            i = format_id(W, g)
            assert format_wg(i) == (W, g)
            assert hw_format(i) == sweep_format(W, g)
            check_lane_formats(hw_format(i))
            assert hw_format(i).P.W == W <= LANE_BITS
            assert hw_format(i).rz.W == hw_format(i).ps.W == W + g
    with pytest.raises(ValueError, match="not a hardware format"):
        format_id(18, 8)
    with pytest.raises(ValueError, match="not a .W, g_s. sweep format"):
        format_wg(2)


# --- the spaces ------------------------------------------------------------------------------


def test_space_sizes():
    assert len(space.vec_space()) == 225 == len(set(space.vec_space()))
    assert len(space.mm_space()) == 1990 == len(set(space.mm_space()))
    assert len(space.glue_space()) == 1350
    full = list(space.full_space())
    assert len(full) == 107_460 == len(set(full))
    assert all(space.is_valid(c) for c in full)
    # the sub-spaces are the projections of the full space
    assert {c.vec_key() for c in full} == {c.vec_key() for c in space.vec_space()}
    assert {c.mm_key() for c in full} == {c.mm_key() for c in space.mm_space()}
    assert {c.glue_key() for c in full} == set(space.glue_space())


def test_validity_rules():
    ok = space.HwConfig()
    assert space.is_valid(ok)
    assert not space.is_valid(space.replace(ok, K=8, R=16))  # R must divide K
    assert not space.is_valid(space.replace(ok, L=8, C=4))  # L must divide C
    assert not space.is_valid(space.replace(ok, K=16, R=16, C=32))  # 512 PEs > 256
    assert not space.is_valid(space.replace(ok, W=18))  # wider than the memory lane
    assert not space.is_valid(space.replace(ok, mem_dw=128))


# --- the split -------------------------------------------------------------------------------


def test_split_counts_validity_and_disjoint_roles():
    rows = space.split_rows()
    counts = Counter((r["top"], r["role"]) for r in rows)
    assert counts == {
        ("vec", "fit"): 25,
        ("mm", "fit"): 26,
        ("det", "fit"): 16,
        ("vec", "holdout"): 12,
        ("mm", "holdout"): 12,
        ("det", "holdout"): 10,
    }
    assert len({r["build"] for r in rows}) == len(rows) == 101
    for top in space.TOPS:
        fit, held = set(space.FIT[top]()), set(space.holdout(top))
        assert not fit & held
        assert all(space.is_valid(c) for c in fit | held)
    # a unit build belongs to its block's space; a detector build to the full space
    assert set(space.vec_fit()) | set(space.holdout("vec")) <= set(space.vec_space())
    assert set(space.mm_fit()) | set(space.holdout("mm")) <= set(space.mm_space())
    # AC5's minimum counts
    assert all(counts[(top, "holdout")] >= 10 for top in ("vec", "mm"))
    assert counts[("det", "holdout")] >= 5


def test_what_the_fit_designs_visit():
    """Which knob values each calibration design visits — and which it does not.

    The vector-unit design visits every value of every knob the block sees.  The other two do not
    (M5 review): the matmul design has 1, 4 and 8 lanes only, and the detector design has
    W ∈ {8, 12, 16}, g_s ∈ {0, 8} and no C = 32.  A model evaluated at a value its design skipped
    is extrapolating there.
    """
    vec, mm, det = space.vec_fit(), space.mm_fit(), space.det_fit()
    assert {c.K for c in vec} == set(space.K_VALUES)
    assert {c.L for c in vec} == set(space.L_VALUES)
    assert {c.W for c in vec} == set(SPACE_W)
    assert {c.g_s for c in vec} == set(SPACE_G)

    assert {c.K for c in mm} == set(space.K_VALUES)
    assert {c.R for c in mm} == set(space.R_VALUES)
    assert {c.C for c in mm} == set(space.C_VALUES)
    assert {c.cmul for c in mm} == set(space.CMULS)
    assert {c.W for c in mm} == set(SPACE_W)
    assert {c.L for c in mm} == {1, 4, 8}  # not 2, not 16

    assert {c.K for c in det} == set(space.K_VALUES)
    assert {c.L for c in det} == set(space.L_VALUES)
    assert {c.mem_dw for c in det} == set(space.MEM_DWS)
    assert {c.sob_depth for c in det} == set(space.SOB_DEPTHS)
    assert {c.cmd_depth for c in det} == set(space.CMD_DEPTHS)
    assert {c.cmul for c in det} == set(space.CMULS)
    assert {c.W for c in det} == {8, 12, 16}  # not 10, not 14
    assert {c.g_s for c in det} == {0, 8}  # not 4
    assert {c.C for c in det} == {4, 8, 16}  # not 32
    assert {c.R for c in det} == {1, 4, 8, 16}  # not 2


def test_what_the_held_out_draw_covers():
    """The draw is uniform and seeded, and it came out thin at K = 16 (M5 review): no vector-unit
    build, three matmul builds with small arrays, and one detector.  Recorded here so that the
    coverage behind the AC5 numbers is stated wherever they are checked."""
    vec, mm, det = (space.holdout(t) for t in space.TOPS)
    assert sorted(c.K for c in vec).count(16) == 0
    assert sorted(c.K for c in mm).count(16) == 3 and max(c.R for c in mm) == 4
    assert sorted(c.K for c in det).count(16) == 1
    assert {c.L for c in vec} == {1, 2, 4, 8, 16}
    assert {c.L for c in mm} == {1, 2, 4, 8}  # no 16-lane matmul unit
    assert {c.L for c in det} == {1, 2, 4, 16}
    assert 10 not in {c.W for c in det}


def test_supplementary_held_out_set():
    """The six builds added at the M5 review: drawn by rule inside the strata the first draw left
    thin, disjoint from every fit and first held-out build, sharing no block with a fit build of
    another top, and committed as the code regenerates it.  The first split is untouched.
    """
    sup = space.supplement()
    assert [(name, top) for name, top, _c in sup] == [
        ("vec: K = 16", "vec"),
        ("vec: K = 16", "vec"),
        ("mm: 16 lanes", "mm"),
        ("mm: R >= 8", "mm"),
        ("det: K = 16, 16 lanes", "det"),
        ("det: K = 16, R >= 8", "det"),
    ]
    assert all(c.K == 16 and space.is_valid(c) for _n, _t, c in sup)
    by_top = {top: [c for _n, t, c in sup if t == top] for top in space.TOPS}
    assert by_top["mm"][0].L == 16 and by_top["mm"][1].R >= 8
    assert by_top["det"][0].L == 16 and by_top["det"][1].R >= 8
    for top, cfgs in by_top.items():
        assert not set(cfgs) & (set(space.FIT[top]()) | set(space.holdout(top)))
    # nothing in it was seen by a fit, even as a block of something else
    det_fit = space.det_fit()
    assert not {c.vec_key() for c in by_top["vec"]} & {c.vec_key() for c in det_fit}
    assert not {c.mm_key() for c in by_top["mm"]} & {c.mm_key() for c in det_fit}
    assert not {c.vec_key() for c in by_top["det"]} & {
        c.vec_key() for c in space.vec_fit()
    }
    assert not {c.mm_key() for c in by_top["det"]} & {
        c.mm_key() for c in space.mm_fit()
    }
    read = space.read_supplement()
    assert [(b, t, r) for b, t, r, _c in read] == [
        (space.label(t, c), t, "supplement") for _n, t, c in sup
    ]


def test_committed_supplement_is_what_the_code_regenerates(tmp_path):
    again = space.write_supplement(tmp_path / "holdout_supplement.csv")
    assert again.read_bytes() == space.SUPPLEMENT_PATH.read_bytes()


def test_second_calibration_round():
    """Step 6.1: 18 more matmul calibration builds at 2 and 16 lanes, one more with one row and one
    lane group at C = 8, and six fresh held-out matmul builds at 2 and 16 lanes.  Nothing in the
    round is an earlier build, the held-out ones share no matmul with any calibration build, and
    the first split and supplement are untouched.
    """
    fit2 = space.mm_fit_v2()
    assert len(fit2) == 19 == len(set(fit2))
    assert Counter(c.L for c in fit2) == {2: 9, 16: 9, 8: 1}
    # the build added after the first eighteen ran: last, and outside both held-out pools
    assert fit2[-1] == space.HwConfig(K=8, L=8, R=1, C=8, cmul=4, W=12, g_s=8)
    assert all(space.is_valid(c) for c in fit2)
    # the first design's centre and corners, with C raised to the lane count where it must be
    assert {(c.K, c.R, c.C, c.W) for c in fit2 if c.L == 2} == {
        (8, 4, 8, 12),
        *(
            (K, R, C, W)
            for K, R, C in ((8, 1, 4), (8, 8, 32), (16, 16, 16), (16, 1, 32))
            for W in (8, 16)
        ),
    }
    assert {(c.K, c.R, c.C, c.W) for c in fit2 if c.L == 16} == {
        (8, 4, 16, 12),
        *(
            (K, R, C, W)
            for K, R, C in ((8, 1, 16), (8, 8, 32), (16, 16, 16), (16, 1, 32))
            for W in (8, 16)
        ),
    }
    earlier_held = set(space.holdout("mm")) | {
        c for _n, t, c in space.supplement() if t == "mm"
    }
    assert not set(fit2) & (set(space.mm_fit()) | earlier_held)

    sup2 = space.supplement2()
    assert [(name, top) for name, top, _c in sup2] == [
        *[("mm: 2 lanes", "mm")] * 3,
        *[("mm: 16 lanes", "mm")] * 3,
    ]
    held = [c for _n, _t, c in sup2]
    assert [c.L for c in held] == [2, 2, 2, 16, 16, 16]
    assert len(set(held)) == 6 and all(space.is_valid(c) for c in held)
    assert not set(held) & (set(space.mm_fit()) | set(fit2) | earlier_held)
    assert not {c.mm_key() for c in held} & {c.mm_key() for c in space.det_fit()}
    # what the draw covers: recorded so the coverage behind the refit's score is stated
    assert [c.K for c in held] == [4, 4, 4, 8, 16, 16]
    assert [c.cmul for c in held] == [3, 3, 3, 4, 3, 4]

    read = space.read_v2()
    assert [(b, r) for b, _t, r, _c in read] == [
        *[(space.label("mm", c), "fit2") for c in fit2],
        *[(space.label("mm", c), "supplement2") for c in held],
    ]
    assert space.FIT_ROLES == ("fit", "fit2")


def test_committed_second_round_is_what_the_code_regenerates(tmp_path):
    again = space.write_v2(tmp_path / "calibration_v2.csv")
    assert again.read_bytes() == space.V2_PATH.read_bytes()


def test_bruteforce_grid():
    """Step 6.3: the 1,440 detectors of gate 6.0 (item 4).  A full cross-product of the listed
    values, inside the design space, with what the calibration has seen of each build recorded.
    """
    grid = space.bruteforce_grid()
    assert len(grid) == 1440 == len(set(grid))
    assert all(space.is_valid(c) for c in grid)
    assert Counter(c.K for c in grid) == {4: 360, 8: 540, 16: 540}
    assert {(c.W, c.g_s) for c in grid} == {(W, g) for W in SPACE_W for g in SPACE_G}
    assert {(c.L, c.C) for c in grid} == {(1, 4), (4, 4), (16, 16)}
    assert {K: {c.R for c in grid if c.K == K} for K in (4, 8, 16)} == {
        4: {1, 4},
        8: {1, 4, 8},
        16: {1, 4, 16},
    }
    assert {c.cmul for c in grid} == {3, 4} and {c.mem_dw for c in grid} == {32, 64}
    assert {(c.sob_depth, c.cmd_depth) for c in grid} == {(2, 2)}
    # a full cross-product: every combination of the values above that K allows
    assert len(grid) == sum(15 * 3 * n_rows * 2 * 2 for n_rows in (2, 3, 3))

    rows = space.bruteforce_rows()
    assert all(
        r["build"].startswith("bf_det_") and r["role"] == "bruteforce" for r in rows
    )
    assert len({r["build"] for r in rows}) == 1440
    # what the calibration has seen: 8 calibration detectors, and the blocks of others
    assert sum(r["in_fit"] for r in rows) == 8
    assert sum(r["vec_in_fit"] for r in rows) == 304
    assert sum(r["mm_in_fit"] for r in rows) == 102
    assert sum(not (r["vec_in_fit"] or r["mm_in_fit"]) for r in rows) == 1078
    assert all(r["vec_in_fit"] and r["mm_in_fit"] for r in rows if r["in_fit"])
    # no held-out detector is in the grid, and no build name collides with an earlier build
    held = set(space.holdout("det")) | {
        c for _n, t, c in space.supplement() if t == "det"
    }
    assert not set(grid) & held
    earlier = {
        b for b, *_ in [*space.read_split(), *space.read_supplement(), *space.read_v2()]
    }
    assert not {r["build"] for r in rows} & earlier


def test_committed_bruteforce_grid_is_what_the_code_regenerates(tmp_path):
    again = space.write_bruteforce(tmp_path / "bruteforce_grid.csv")
    assert again.read_bytes() == space.BRUTEFORCE_PATH.read_bytes()
    read = space.read_bruteforce()
    assert [c for _b, _t, _r, c in read] == space.bruteforce_grid()
    assert {r for _b, _t, r, _c in read} == {"bruteforce"}


def test_committed_split_is_what_the_code_regenerates(tmp_path):
    again = space.write_split(tmp_path / "holdout_split.csv")
    assert again.read_bytes() == space.SPLIT_PATH.read_bytes()
    read = space.read_split()
    assert [(b, t, r) for b, t, r, _ in read] == [
        (r["build"], r["top"], r["role"]) for r in space.split_rows()
    ]
    assert all(space.label(t, c) == b for b, t, _, c in read)


# --- the Python simulation at every format of the space --------------------------------------


def _unit_jobs(kind: str, fmt: int, K: int, seed: int):
    """8 random problems and the zero-residual one, with a nit per job cycling 1..K."""
    probs = _problems(CaseSetSpec(kind, hw_format(fmt), K, N, K, False, seed))
    probs = probs[:8] + probs[-1:]
    return [(A, B, 64.0) for A, B, _ in probs], [(j % K) + 1 for j in range(len(probs))]


@pytest.mark.parametrize("name", SPACE_FORMATS)
def test_pysim_is_bit_exact_at_every_format(name):
    """Detector (every nit), vector unit and matmul unit at K = 4: each ``run`` raises on a mismatch."""
    K, fmt = 4, ALL_FORMAT_NAMES.index(name)
    probs = detector_problems(
        32, K, N, 5, seed=40 + fmt
    )  # the last is the zero-residual case
    CgDetectorSim(
        [p for p in probs for _ in range(K)],
        [nit for _ in probs for nit in range(1, K + 1)],
        K=K,
        fmt=fmt,
    ).run()
    problems, jobs = _unit_jobs("vec", fmt, K, seed=60 + fmt)
    CgVecUnitSim(problems, jobs, K=K, fmt=fmt).run()
    problems, jobs = _unit_jobs("mm", fmt, K, seed=80 + fmt)
    CgMmUnitSim(problems, jobs, K=K, fmt=fmt).run()
