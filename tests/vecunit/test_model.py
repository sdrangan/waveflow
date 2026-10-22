"""The interpreter, the vector unit's golden: every instruction and operand form against an exact
rational reference (tests/vecunit/_ref.py), random chained programs against serial execution (chaining
never changes a bit), the hardware's checks, and the block layout with ``L`` not dividing ``n``."""

from __future__ import annotations

import numpy as np
import pytest

from tests.vecunit import _ref as ref
from waveflow.linalg.message import Status
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit import isa as I
from waveflow.vecunit.config import Cap, VecUnitConfig
from waveflow.vecunit.model import (
    Block,
    Machine,
    ProgramError,
    block_from_matrix,
    instr_status,
    matrix_from_block,
    pass_status,
    run_job,
    split_passes,
)

L, VLMAX = 4, 6


def rand_fmt(rng, wmin=6, wmax=14) -> Format:
    W = int(rng.integers(wmin, wmax + 1))
    return Format(W, int(rng.integers(1, W)), True,
                  QMode.AP_RND if rng.integers(2) else QMode.AP_TRN,
                  OMode.AP_SAT if rng.integers(2) else OMode.AP_WRAP)


def rand_cfg(rng, *, NG=8, NS=6, NQI=2, NQO=2, S=3, slot_caps=(), g_div=None) -> VecUnitConfig:
    return VecUnitConfig(
        group_formats=tuple(rand_fmt(rng) for _ in range(NG)),
        scalar_formats=tuple(rand_fmt(rng, 8, 22) for _ in range(NS)),
        in_queue_formats=tuple(rand_fmt(rng) for _ in range(NQI)),
        out_queue_formats=tuple(rand_fmt(rng) for _ in range(NQO)),
        L=L, VLMAX=VLMAX, Kmax=VLMAX, Nmax=2 * L + 1, S=S, NB=4, slot_caps=slot_caps,
        g_div=int(rng.integers(0, 7)) if g_div is None else g_div)


def full(fmt: Format, rng, shape):
    return rng.integers(-(1 << (fmt.W - 1)), 1 << (fmt.W - 1), shape)


def filled(cfg: VecUnitConfig, rng) -> Machine:
    """A machine whose every group, scalar and head block holds random data."""
    m = Machine(cfg)
    for g, f in enumerate(cfg.group_formats):
        m.vre[g], m.vim[g] = full(f, rng, (L, VLMAX)), full(f, rng, (L, VLMAX))
    m.vlen[:] = VLMAX
    for j, f in enumerate(cfg.scalar_formats):
        m.s[j] = full(f, rng, L)
        if rng.integers(4) == 0:
            m.s[j][int(rng.integers(L))] = 0  # a zero divisor now and then
    m.sset[:] = True
    for q, f in enumerate(cfg.in_queue_formats):
        k, n = VLMAX, cfg.Nmax
        m.inq[q].append(block_from_matrix(full(f, rng, (k, n)), full(f, rng, (k, n)), cfg))
    return m


def vals(m, g, l, i):
    f = m.cfg.group_formats[g]
    return ref.value(m.vre[g][l][i], f), ref.value(m.vim[g][l][i], f)


def cmul(a, b):
    return a[0] * b[0] - a[1] * b[1], a[0] * b[1] + a[1] * b[0]


# --- every instruction against the reference ------------------------------------------------------

VOPS = [I.VOp.MVI, I.VOp.MV, I.VOp.ADD, I.VOp.SUB, I.VOp.MUL, I.VOp.MACC, I.VOp.NMSAC, I.VOp.MADD,
        I.VOp.MACC_S, I.VOp.NMSAC_S, I.VOp.MADD_S]


@pytest.mark.parametrize("mode", ["pass", "serial"])
@pytest.mark.parametrize("v", VOPS, ids=lambda v: v.name)
def test_vector_instruction_against_reference(v, mode):
    rng = np.random.default_rng(int(v) * 7 + (mode == "pass"))
    for trial in range(12):
        cfg = rand_cfg(rng)
        m = filled(cfg, rng)
        vl = int(rng.integers(1, VLMAX + 1))
        m.vl = vl
        vd, vs1, vs2 = 0, 1, 2  # three banks
        conj = bool(rng.integers(2)) and v in I.MULTIPLYING
        bcast = bool(rng.integers(2)) and v in I.MULTIPLYING
        sj = int(rng.integers(cfg.NS))
        if v == I.VOp.MVI:
            x = I.vmv_v_i(vd, int(rng.integers(-2048, 2048)))
        elif v == I.VOp.MV:
            x = I.vmv_v_v(vd, vs1)
        elif v in (I.VOp.MACC_S, I.VOp.NMSAC_S, I.VOp.MADD_S):
            x = I.Instr(I.Opcode.VEC, v, rd=vd, rs1=sj, rs2=vs2)
        else:
            x = I.Instr(I.Opcode.VEC, v, rd=vd, rs1=vs1, rs2=vs2, conj=conj, bcast=bcast)
        before = m.vre[vd].copy(), m.vim[vd].copy()
        exp = {}
        fd = cfg.group_formats[vd]
        for l in range(L):
            for i in range(vl):
                c = vals(m, vd, l, i)
                b = vals(m, vs2, l, i)
                a = vals(m, vs1, 0 if bcast else l, i)
                if conj:
                    a = (a[0], -a[1])
                s = (ref.value(m.s[sj][l], cfg.scalar_formats[sj]), 0)
                r = {I.VOp.MVI: (x.simm, 0), I.VOp.MV: vals(m, vs1, l, i),
                     I.VOp.ADD: (a[0] + b[0], a[1] + b[1]), I.VOp.SUB: (a[0] - b[0], a[1] - b[1]),
                     I.VOp.MUL: cmul(a, b),
                     I.VOp.MACC: tuple(p + q for p, q in zip(c, cmul(a, b))),
                     I.VOp.NMSAC: tuple(p - q for p, q in zip(c, cmul(a, b))),
                     I.VOp.MADD: tuple(p + q for p, q in zip(cmul(a, c), b)),
                     I.VOp.MACC_S: tuple(p + q for p, q in zip(c, cmul(s, b))),
                     I.VOp.NMSAC_S: tuple(p - q for p, q in zip(c, cmul(s, b))),
                     I.VOp.MADD_S: tuple(p + q for p, q in zip(cmul(s, c), b))}[v]
                exp[l, i] = (ref.quantize(r[0], fd), ref.quantize(r[1], fd))
        m.run_pass([x], mode)
        for l in range(L):
            for i in range(VLMAX):
                got = (int(m.vre[vd][l][i]), int(m.vim[vd][l][i]))
                want = exp.get((l, i), (int(before[0][l][i]), int(before[1][l][i])))
                assert got == want, (str(x), trial, l, i)


@pytest.mark.parametrize("mode", ["pass", "serial"])
def test_vdotr_against_reference(mode):
    rng = np.random.default_rng(3)
    for _ in range(20):
        cfg = rand_cfg(rng)
        m = filled(cfg, rng)
        m.vl = vl = int(rng.integers(1, VLMAX + 1))
        sd = int(rng.integers(cfg.NS))
        m.run_pass([I.vdotr(sd, 1, 2)], mode)
        for l in range(L):
            tot = sum(vals(m, 1, l, i)[0] * vals(m, 2, l, i)[0]
                      + vals(m, 1, l, i)[1] * vals(m, 2, l, i)[1] for i in range(vl))
            assert int(m.s[sd][l]) == ref.quantize(tot, cfg.scalar_formats[sd])


def test_scalar_instructions_against_reference():
    rng = np.random.default_rng(4)
    zeros = 0
    for _ in range(60):
        cfg = rand_cfg(rng)
        m = filled(cfg, rng)
        sd, sa, sb = (int(v) for v in rng.integers(cfg.NS, size=3))
        fa, fb, fd = cfg.scalar_formats[sa], cfg.scalar_formats[sb], cfg.scalar_formats[sd]
        a = [ref.value(m.s[sa][l], fa) for l in range(L)]
        b = [ref.value(m.s[sb][l], fb) for l in range(L)]
        m.scalar(I.sdivz(sd, sa, sb))
        for l in range(L):
            zeros += b[l] == 0
            want = 0 if b[l] == 0 else ref.quantize(ref.divide(a[l], fa, b[l], cfg.g_div), fd)
            assert int(m.s[sd][l]) == want
        a = [ref.value(m.s[sa][l], fa) for l in range(L)]
        m.scalar(I.smv(sd, sa))
        assert [int(v) for v in m.s[sd]] == [ref.quantize(a[l], fd) for l in range(L)]
    assert zeros > 0  # the zero guard ran


def test_queue_reads_and_writes_with_L_not_dividing_n():
    rng = np.random.default_rng(5)
    cfg = rand_cfg(rng)
    k, n = VLMAX, 5  # two lane groups a row, the second with one value
    fq, fo = cfg.in_queue_formats[0], cfg.out_queue_formats[1]
    re, im = full(fq, rng, (k, n)), full(fq, rng, (k, n))
    blk = block_from_matrix(re, im, cfg)
    assert (matrix_from_block(blk, k, n, cfg)[0] == re).all()
    assert (blk.re[1::2, 1:] == 0).all()  # the spare lanes of each row's last group
    m = Machine(cfg)
    m.inq[0].append(blk)
    m.vl = k
    m.run_pass([I.vlse(0, 0, 1, 2)], "pass")
    f0 = cfg.group_formats[0]
    for i in range(k):
        assert int(m.vre[0][0][i]) == ref.quantize(ref.value(re[i, 4], fq), f0)
        assert (m.vre[0][1:, i] == 0).all()
    m.run_pass([I.vsse(1, 0, 1, 2)], "serial")
    blk2 = m.tail[1]
    for i in range(k):
        assert int(blk2.re[i * 2 + 1][0]) == ref.quantize(ref.value(m.vre[0][0][i], f0), fo)
    assert not blk2.written[0::2].any()


# --- chaining never changes a bit -----------------------------------------------------------------


def random_vector_instr(rng, cfg) -> I.Instr:
    g = lambda: int(rng.integers(cfg.NG))
    s = lambda: int(rng.integers(cfg.NS))
    kind = int(rng.integers(15))
    conj, bcast = bool(rng.integers(2)), bool(rng.integers(2))
    if kind < 11:
        v = VOPS[kind]
        if v == I.VOp.MVI:
            return I.vmv_v_i(g(), int(rng.integers(-2048, 2048)))
        if v == I.VOp.MV:
            return I.vmv_v_v(g(), g())
        if v in (I.VOp.MACC_S, I.VOp.NMSAC_S, I.VOp.MADD_S):
            return I.Instr(I.Opcode.VEC, v, rd=g(), rs1=s(), rs2=g())
        mul = v in I.MULTIPLYING
        return I.Instr(I.Opcode.VEC, v, rd=g(), rs1=g(), rs2=g(), conj=conj and mul,
                       bcast=bcast and mul)
    if kind == 11:
        return I.vdotr(s(), g(), g())
    gpr = cfg.groups_per_row(cfg.Nmax)
    if kind in (12, 13):
        return I.vlse(g(), int(rng.integers(cfg.NQI)), int(rng.integers(gpr)), gpr)
    return I.vsse(int(rng.integers(cfg.NQO)), g(), int(rng.integers(gpr)), gpr)


def random_program(rng, cfg, npass: int) -> list[I.Instr]:
    prog = [I.setvl(int(rng.integers(1, VLMAX + 1)))]
    while npass:
        r = int(rng.integers(10))
        if r == 0:
            prog.append(I.setvl(int(rng.integers(1, VLMAX + 1))))
            continue
        if r == 1:
            s = lambda: int(rng.integers(cfg.NS))
            prog.append(I.sdivz(s(), s(), s()) if rng.integers(2) else I.smv(s(), s()))
            continue
        chain = [random_vector_instr(rng, cfg) for _ in range(int(rng.integers(1, cfg.S + 1)))]
        if pass_status(chain, cfg) != Status.OK:
            continue
        prog += [x.c for x in chain[:-1]] + [chain[-1]]
        npass -= 1
    return prog


def state(m: Machine):
    blocks = [(b.re.copy(), b.im.copy(), b.written.copy()) for b in m.tail if b is not None]
    return m.vre.copy(), m.vim.copy(), m.s.copy(), blocks


def test_chaining_never_changes_a_bit():
    rng = np.random.default_rng(6)
    chained = 0
    for trial in range(150):
        cfg = rand_cfg(rng)
        seed = int(rng.integers(1 << 30))
        prog = random_program(rng, cfg, 8)
        chained += sum(x.chain for x in prog)
        out = []
        for mode in ("pass", "serial"):
            m = filled(cfg, np.random.default_rng(seed))
            for chain in split_passes(prog, cfg.S):  # passes as the hardware gathers them
                if chain[0].vector:
                    m.run_pass(chain, mode)
                elif int(chain[0].opcode) == I.Opcode.SCALAR:
                    m.scalar(chain[0])
                else:
                    m.control(chain[0])
            out.append(state(m))
        (a1, b1, c1, d1), (a2, b2, c2, d2) = out
        assert np.array_equal(a1, a2) and np.array_equal(b1, b2) and np.array_equal(c1, c2), trial
        assert len(d1) == len(d2) and all(all(np.array_equal(p, q) for p, q in zip(x, y))
                                          for x, y in zip(d1, d2)), trial
    assert chained > 300  # the programs did chain


def test_forwarding_reads_the_quantized_result():
    """A later slot reads an earlier slot's result after its quantization: v1 = q(v0 + v0) saturates,
    and v2 = v1 chained reads the saturated value, as v2 = v1 run alone does."""
    f = Format(8, 2, True, QMode.AP_RND, OMode.AP_SAT)
    cfg = VecUnitConfig(group_formats=(f,) * 4, scalar_formats=(f,), in_queue_formats=(f,),
                        out_queue_formats=(f,), L=L, VLMAX=2, Kmax=2, Nmax=L)
    m = Machine(cfg)
    m.vre[0][:] = 120
    m.vlen[0] = 2
    m.vl = 2
    m.run_pass([I.vadd_vv(1, 0, 0).c, I.vmv_v_v(2, 1)], "pass")
    assert (m.vre[1] == 127).all() and (m.vre[2] == 127).all()


# --- what the hardware checks -----------------------------------------------------------------------


def centre() -> VecUnitConfig:
    f = Format(12, 4, True, QMode.AP_RND, OMode.AP_SAT)
    return VecUnitConfig(group_formats=(f,) * 8, scalar_formats=(Format(20, 10),) * 4,
                         in_queue_formats=(f, f), out_queue_formats=(f, f), L=4, VLMAX=8)


@pytest.mark.parametrize("x, st", [
    (I.Instr(I.Opcode.VEC, 12), Status.BAD_OP),
    (I.Instr(I.Opcode.VEC, I.VOp.ADD, conj=True), Status.BAD_OP),
    (I.Instr(I.Opcode.SCALAR, I.SOp.DIVZ, chain=True), Status.BAD_OP),
    (I.ld(0, 0, 8, 8), Status.BAD_OP),
    (I.Instr(0x33, 0), Status.BAD_OP),
    (I.vmv_v_v(8, 0), Status.BAD_DIMS),
    (I.vdotr(4, 0, 0), Status.BAD_DIMS),
    (I.vmacc_vs(0, 4, 1), Status.BAD_DIMS),
    (I.setvl(0), Status.BAD_DIMS),
    (I.setvl(9), Status.BAD_DIMS),
    (I.vlse(0, 2, 0, 8), Status.BAD_DIMS),
    (I.vlse(0, 0, 8, 8), Status.BAD_DIMS),
    (I.vsse(0, 0, 0, 9), Status.BAD_DIMS),
    (I.qpop(2), Status.BAD_DIMS),
    (I.vmacc_vv(0, 1, 2, conj=True, bcast=True), Status.OK),
])
def test_instruction_status(x, st):
    assert instr_status(x, centre()) == st


@pytest.mark.parametrize("chain, ok", [
    ([I.vadd_vv(0, 1, 2), ], True),
    ([I.vadd_vv(0, 1, 5)], False),                          # v1 and v5 share bank 1
    ([I.vmv_v_v(1, 2).c, I.vadd_vv(0, 1, 5)], True),        # v1 forwarded: only v5 read in bank 1
    ([I.vmv_v_v(0, 1).c, I.vmv_v_v(4, 2)], False),          # two writes to bank 0
    ([I.vlse(0, 0, 0, 8).c, I.vlse(1, 0, 1, 8)], False),    # one wf.vlse a queue
    ([I.vdotr(0, 1, 2).c, I.vmacc_vs(3, 0, 1)], False),     # a reduction read in its pass
    ([I.vdotr(0, 1, 2).c, I.vdotr(0, 1, 1)], False),        # one scalar written twice
    ([I.vmv_v_v(0, 1).c, I.vmv_v_v(1, 2).c, I.vmv_v_v(2, 3).c, I.vmv_v_v(3, 0)], False),  # > S
])
def test_pass_rules(chain, ok):
    assert (pass_status(chain, centre()) == Status.OK) == ok


def test_slot_capabilities():
    f = Format(12, 4)
    cfg = VecUnitConfig(group_formats=(f,) * 4, scalar_formats=(f,), in_queue_formats=(f,),
                        out_queue_formats=(f,), slot_caps=(Cap.LOAD | Cap.MOVE, Cap.MUL, Cap.STORE))
    assert pass_status([I.vlse(0, 0, 0, 8).c, I.vmul_vv(1, 0, 2).c, I.vsse(0, 1, 0, 8)], cfg) == 0
    assert pass_status([I.vmul_vv(1, 0, 2)], cfg) == Status.BAD_OP


def test_an_error_is_fatal_and_stops_the_job():
    cfg = centre()
    rng = np.random.default_rng(8)
    B = (rng.integers(-2000, 2000, (8, 8)), rng.integers(-2000, 2000, (8, 8)))
    ex = [I.setvl(8), I.vlse(0, 0, 0, 2), I.vsse(0, 0, 0, 2), I.vsse(0, 0, 1, 2), I.qpush(0),
          I.vadd_vv(1, 0, 4),  # bank conflict: BAD_OP here
          I.qpop(0)]
    r = run_job(cfg, [I.ld(0, 0, 8, 8)], ex, [I.st(0, 0, 1, 8, 8), I.st(0, 0, 2, 8, 8)],
                {0: [B]}, tx_id=3)
    assert r.status == Status.BAD_OP and r.executed == 5
    assert len(r.outputs[0]) == 1  # the block pushed before the error, and nothing after


def test_program_errors_are_refused():
    cfg = centre()
    with pytest.raises(ProgramError, match="read"):
        Machine(cfg).run_pass([I.vmv_v_v(0, 1)], "serial")  # v1 never written
    m = Machine(cfg)
    with pytest.raises(ProgramError, match="empty"):
        m.run_pass([I.vlse(0, 0, 0, 1)], "serial")
    assert isinstance(Block.empty(4, 4), Block)
