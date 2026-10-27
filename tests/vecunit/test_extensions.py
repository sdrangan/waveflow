"""The batch of ``plans/vecunit_future.md``: the scalar extensions (F2), the result shift (F4) and
dependent fusion (F7), on the interpreter -- every new instruction and the shift against the exact
rational reference (tests/vecunit/_ref.py), chained against serial, the hardware's checks, and the
fusion rule."""

from __future__ import annotations

import dataclasses
from fractions import Fraction

import numpy as np
import pytest

from tests.vecunit import _ref as ref
from tests.vecunit import test_model as M
from tests.vecunit.test_model import L, VLMAX, cmul, filled, vals
from waveflow.linalg.message import Status
from waveflow.vecunit import isa as I
from waveflow.vecunit.model import exec_items, instr_status, pass_status, split_passes


def _ext(cfg):
    """*cfg* with the scalar extensions and the result shift built, which these tests exercise."""
    return dataclasses.replace(cfg, scalar_ext=True, rs_max=15)


def rand_cfg(rng):
    return _ext(M.rand_cfg(rng))


def centre():
    return _ext(M.centre())


def scale(x, rs: int):
    return tuple(v / Fraction(2) ** rs for v in x)


# --- every new instruction, and the result shift, against the reference ---------------------------


SCALED_CASES = ["MUL", "MACC", "NMSAC", "MADD", "MACC_S", "NMSAC_S", "MADD_S", "MUL_S"]


@pytest.mark.parametrize("mode", ["pass", "serial"])
@pytest.mark.parametrize("name", SCALED_CASES + ["ADD_S", "SUB_S"])
def test_scaled_and_scalar_forms_against_reference(name, mode):
    v = I.VOp[name]
    rng = np.random.default_rng(int(v) * 11 + (mode == "pass"))
    for trial in range(12):
        cfg = rand_cfg(rng)
        m = filled(cfg, rng)
        m.vl = vl = int(rng.integers(1, VLMAX + 1))
        m.rs = int(rng.integers(cfg.rs_max + 1))  # the wf.setrs default
        own = int(rng.integers(cfg.rs_max + 1)) if rng.integers(2) else None
        vd, vs1, vs2 = 0, 1, 2
        sj = int(rng.integers(cfg.NS))
        conj = bool(rng.integers(2)) and v in I.MULTIPLYING
        scalar_a = v in (I.VOp.MACC_S, I.VOp.NMSAC_S, I.VOp.MADD_S, I.VOp.MUL_S, I.VOp.ADD_S,
                         I.VOp.SUB_S)
        rs_arg = own if v in I.SCALED else None
        x = I._v(v, vd, sj if scalar_a else vs1, vs2, conj=conj, rs=rs_arg)
        rs = (m.rs if own is None else own) if v in I.SCALED else 0
        before = m.vre[vd].copy(), m.vim[vd].copy()
        fd = cfg.group_formats[vd]
        exp = {}
        for l in range(L):
            s = (ref.value(m.s[sj][l], cfg.scalar_formats[sj]), Fraction(0))
            for i in range(vl):
                c = vals(m, vd, l, i)
                b = vals(m, vs2, l, i)
                a = s if scalar_a else vals(m, vs1, l, i)
                if conj:
                    a = (a[0], -a[1])
                p = {I.VOp.MADD: lambda: cmul(a, c), I.VOp.MADD_S: lambda: cmul(s, c)}.get(
                    v, lambda: cmul(a, b))()
                p = scale(p, rs)
                r = {I.VOp.MUL: p, I.VOp.MUL_S: p,
                     I.VOp.MACC: tuple(q + w for q, w in zip(c, p)),
                     I.VOp.MACC_S: tuple(q + w for q, w in zip(c, p)),
                     I.VOp.NMSAC: tuple(q - w for q, w in zip(c, p)),
                     I.VOp.NMSAC_S: tuple(q - w for q, w in zip(c, p)),
                     I.VOp.MADD: tuple(q + w for q, w in zip(p, b)),
                     I.VOp.MADD_S: tuple(q + w for q, w in zip(p, b)),
                     I.VOp.ADD_S: (b[0] + s[0], b[1]),
                     I.VOp.SUB_S: (b[0] - s[0], b[1])}[v]
                exp[l, i] = (ref.quantize(r[0], fd), ref.quantize(r[1], fd))
        m.run_pass([x], mode)
        for l in range(L):
            for i in range(VLMAX):
                got = (int(m.vre[vd][l][i]), int(m.vim[vd][l][i]))
                want = exp.get((l, i), (int(before[0][l][i]), int(before[1][l][i])))
                assert got == want, (str(x), trial, l, i)


@pytest.mark.parametrize("mode", ["pass", "serial"])
def test_scaled_vdotr_and_vmv_s_v_against_reference(mode):
    rng = np.random.default_rng(13)
    for _ in range(20):
        cfg = rand_cfg(rng)
        m = filled(cfg, rng)
        m.vl = vl = int(rng.integers(1, VLMAX + 1))
        m.rs = int(rng.integers(cfg.rs_max + 1))
        own = int(rng.integers(cfg.rs_max + 1)) if rng.integers(2) else None
        rs = m.rs if own is None else own
        sd, se = 0, 1
        m.run_pass([I.vdotr(sd, 1, 2, rs=own).c, I.vmv_s_v(se, 3)], mode)
        for l in range(L):
            tot = sum(vals(m, 1, l, i)[0] * vals(m, 2, l, i)[0]
                      + vals(m, 1, l, i)[1] * vals(m, 2, l, i)[1] for i in range(vl))
            assert int(m.s[sd][l]) == ref.quantize(tot / Fraction(2) ** rs, cfg.scalar_formats[sd])
            assert int(m.s[se][l]) == ref.quantize(vals(m, 3, l, 0)[0], cfg.scalar_formats[se])


def test_sli_and_setrs():
    cfg = centre()
    from waveflow.vecunit.model import Machine

    m = Machine(cfg)
    m.scalar(I.sli(2, -1000))
    assert (m.s[2] == -1000).all() and m.sset[2]
    m.control(I.setrs(5))
    assert m.rs == 5


# --- the hardware's checks ------------------------------------------------------------------------


@pytest.mark.parametrize("x, st", [
    (I.vmul_vs(0, 3, 1), Status.OK),
    (I.vmul_vs(0, 4, 1), Status.BAD_DIMS),            # s4: centre() has four scalars
    (I.vadd_vs(8, 0, 1), Status.BAD_DIMS),
    (I.vmv_s_v(3, 7), Status.OK),
    (I.vmv_s_v(4, 0), Status.BAD_DIMS),
    (I.sli(0, (1 << 19) - 1), Status.OK),              # 20-bit scalars
    (I.sli(0, 1 << 19), Status.BAD_DIMS),
    (I.sli(0, -(1 << 19)), Status.OK),
    (I.setrs(15), Status.OK),
    (I.setrs(16), Status.BAD_DIMS),
    (I.vmul_vv(0, 1, 2, rs=15), Status.OK),
    (I.Instr(I.Opcode.VEC, I.VOp.ADD_S, rd=0, rs1=0, rs2=1, conj=True), Status.BAD_OP),
])
def test_instruction_status(x, st):
    assert instr_status(x, centre()) == st


def test_a_unit_built_without_the_features_refuses_them():
    """Off -- the defaults -- the extensions are not decoded (BAD_OP) and no shift is allowed."""
    lean = M.centre()
    assert not lean.scalar_ext and lean.rs_max == 0 and lean.fuse_raw_vl == 0
    for x in (I.vmul_vs(0, 0, 1), I.vadd_vs(0, 0, 1), I.vsub_vs(0, 0, 1), I.vmv_s_v(0, 1),
              I.sli(0, 1)):
        assert instr_status(x, lean) == Status.BAD_OP, str(x)
    assert instr_status(I.vmul_vv(0, 1, 2, rs=1), lean) == Status.BAD_DIMS
    assert instr_status(I.setrs(1), lean) == Status.BAD_DIMS
    assert instr_status(I.vmul_vv(0, 1, 2, rs=0), lean) == Status.OK
    assert instr_status(I.setrs(0), lean) == Status.OK


def test_a_shift_past_rs_max_is_refused():
    cfg = dataclasses.replace(centre(), rs_max=3)
    assert instr_status(I.vmul_vv(0, 1, 2, rs=3), cfg) == Status.OK
    assert instr_status(I.vmul_vv(0, 1, 2, rs=4), cfg) == Status.BAD_DIMS
    assert instr_status(I.setrs(4), cfg) == Status.BAD_DIMS


def test_vmv_s_v_is_a_reduction_in_the_pass_rules():
    cfg = centre()
    assert pass_status([I.vmv_s_v(0, 1).c, I.vmacc_vs(2, 0, 3)], cfg) == Status.BAD_OP
    assert pass_status([I.vmv_s_v(0, 1).c, I.vmacc_vs(2, 1, 3)], cfg) == Status.OK


def test_the_result_shift_and_constructors_refuse_bad_values():
    with pytest.raises(ValueError):
        I._v(I.VOp.ADD, 0, 1, 2, rs=1)  # only a multiplying instruction takes a shift
    with pytest.raises(ValueError):
        I.vmul_vv(0, 1, 2, rs=16)
    with pytest.raises(ValueError):
        I.sli(0, 1 << 23)


# --- chaining never changes a bit, with the new instructions -----------------------------------------


def test_chaining_never_changes_a_bit_with_the_extensions():
    from waveflow.vecunit.workload import _random_vector

    rng = np.random.default_rng(17)
    chained = 0
    for trial in range(120):
        cfg = rand_cfg(rng)
        seed = int(rng.integers(1 << 30))
        prog = [I.setvl(int(rng.integers(1, VLMAX + 1)))]
        npass = 8
        while npass:
            r = int(rng.integers(10))
            if r == 0:
                prog.append(I.setrs(int(rng.integers(cfg.rs_max + 1))))
                continue
            chain = [_random_vector(rng, cfg, cfg.groups_per_row(cfg.Nmax), extended=True)
                     for _ in range(int(rng.integers(1, cfg.S + 1)))]
            if pass_status(chain, cfg) != Status.OK:
                continue
            prog += [x.c for x in chain[:-1]] + [chain[-1]]
            npass -= 1
        chained += sum(x.chain for x in prog)
        out = []
        for mode in ("pass", "serial"):
            m = filled(cfg, np.random.default_rng(seed))
            for chain in split_passes(prog, cfg.S):
                if chain[0].vector:
                    m.run_pass(chain, mode)
                else:
                    m.control(chain[0])
            out.append((m.vre.copy(), m.vim.copy(), m.s.copy()))
        assert all(np.array_equal(a, b) for a, b in zip(*out)), trial
    assert chained > 200


# --- dependent fusion (F7) ---------------------------------------------------------------------------


def test_dependent_passes_fuse_only_when_vl_covers_the_distance():
    cfg = dataclasses.replace(centre(), fuse_raw_vl=6)
    prog = [I.vmv_v_i(0, 1), I.vadd_vv(1, 0, 0), I.vadd_vv(2, 1, 1)]  # each reads what the last wrote
    runs = lambda p, c: [len(it.chains) for it in exec_items(p, c) if it.kind == "passes"]
    assert runs([I.setvl(6)] + prog, cfg) == [3]                 # VL 6 >= 6: one run
    assert runs([I.setvl(5)] + prog, cfg) == [1, 1, 1]           # VL 5 < 6: a run each
    assert runs([I.setvl(8)] + prog, centre()) == [1, 1, 1]      # off by default
    # a scalar read still ends the run: the reduction delivers at the run's end
    p = [I.setvl(8), I.vdotr(0, 3, 3), I.vmacc_vs(1, 0, 2)]
    assert runs(p, cfg) == [1, 1]


def test_dependent_fusion_changes_no_result():
    """F7 is timing only: the interpreter's results are the same with and without it."""
    from waveflow.vecunit.workload import random_jobs
    from waveflow.vecunit.model import run_job

    base = dataclasses.replace(centre(), fuse_raw_vl=0)
    fused = dataclasses.replace(centre(), fuse_raw_vl=1)
    for tx, p, inputs in random_jobs(base, 6, seed=4, extended=True):
        a = run_job(base, *p, inputs, tx_id=tx)
        b = run_job(fused, *p, inputs, tx_id=tx)
        assert a.status == b.status == 0
        for (ra, xa, ya), (rb, xb, yb) in zip(a.outputs[0], b.outputs[0]):
            assert ra == rb and np.array_equal(xa, xb) and np.array_equal(ya, yb)
