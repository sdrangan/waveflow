"""The assembler: a well-formed job assembles into three framed bursts and runs; each kind of
problem is refused with a reason."""

from __future__ import annotations

import numpy as np
import pytest

from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit import isa as I
from waveflow.vecunit.asm import AsmError, Programs, assemble, check, disassemble_words
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.model import run_job


def fmt(W, i):
    return Format(W, i, True, QMode.AP_RND, OMode.AP_SAT)


CFG = VecUnitConfig(group_formats=(fmt(12, 3), fmt(12, 4), fmt(12, 4), fmt(12, 5)) * 2,
                    scalar_formats=(fmt(20, 10), fmt(20, 9), fmt(12, 5), fmt(12, 3), fmt(20, 9)),
                    in_queue_formats=(fmt(12, 4), fmt(12, 5)), out_queue_formats=(fmt(12, 4), fmt(12, 3)),
                    L=4, VLMAX=8, Kmax=8, Nmax=8, g_div=6)
K, N = 8, 6  # two column groups, the second half full


def job() -> Programs:
    """A two-group job: start, one CG-like step, P and X out (the pattern, not CG itself)."""
    ex = [I.setvl(K)]
    for g in range(2):
        X, R, P, S = 4 * (g % 2) + 0, 4 * (g % 2) + 1, 4 * (g % 2) + 2, 4 * (g % 2) + 3
        ex += [I.vmv_v_i(X, 0), I.vlse(R, 0, g, 2).c, I.vmv_v_v(P, R).c, I.vdotr(1, R, R),
               I.vlse(S, 1, g, 2).c, I.vdotr(0, P, S), I.sdivz(2, 1, 0),
               I.vmacc_vs(X, 2, P).c, I.vnmsac_vs(R, 2, S).c, I.vdotr(4, R, R), I.sdivz(3, 4, 1),
               I.vmadd_vs(P, 3, R).c, I.vsse(0, P, g, 2), I.vsse(1, X, g, 2)]
    ex += [I.qpop(0), I.qpop(1), I.qpush(0), I.qpush(1)]
    return Programs(load=[I.ld(0, 0, K, N), I.ld(1, 0, K, N)], execute=ex,
                    store=[I.st(0, 0, 1, K, N), I.st(0, 1, 2, K, N)])


def test_a_well_formed_job_assembles_and_runs():
    p = job()
    assert check(CFG, p) == []
    words = assemble(CFG, p, tx_id=9)
    assert set(words) == {"load", "execute", "store"}
    assert int(words["execute"][0]) == 9 | (len(p.execute) << 16)
    text = disassemble_words(words["execute"])
    assert text[0] == f"# job tx_id=9 ninstr={len(p.execute)}" and text[3] == "wf.vlse.c v1, qi0, 0, 2"
    assert [I.Instr.decode(int(w)) for w in words["execute"][1:]] == p.execute
    rng = np.random.default_rng(0)
    data = [tuple(rng.integers(-2048, 2048, (K, N)) for _ in range(2)) for _ in range(2)]
    r = run_job(CFG, *p, {0: data}, tx_id=9)
    assert r.status == 0 and [o[0]["tag"] for o in r.outputs[0]] == [1, 2]


def broken(**edit) -> Programs:
    p = job()
    for name, fn in edit.items():
        setattr(p, name, fn(getattr(p, name)))
    return p


@pytest.mark.parametrize("progs, says", [
    (broken(execute=lambda e: e[:-1]), "holding output queue qo1"),
    (broken(execute=lambda e: e[:-4] + e[-3:]), "holding input queue qi0"),
    (broken(store=lambda s: s[:1]), "fills more blocks than the store program takes"),
    (broken(load=lambda ld: ld[:1]), "no more blocks"),
    (broken(execute=lambda e: [e[0], I.vmv_v_v(0, 1)] + e[1:]), "reads v1 at 8 elements; 0 were"),
    (broken(execute=lambda e: [e[0], I.smv(0, 1)] + e[1:]), "reads s1 before"),
    (broken(execute=lambda e: e[:2] + [I.vlse(1, 0, 0, 1).c] + e[3:]), "the stride is 2"),
    (broken(execute=lambda e: e[:27] + e[28:]), "(row, column group) [(0, 1)"),
    (broken(execute=lambda e: e[:2] + [I.vlse(1, 0, 0, 2).c, I.vmv_v_v(5, 1).c, I.vdotr(1, 1, 1)]
            + e[5:]), "pass rule"),
    (broken(execute=lambda e: e + [I.vmv_v_v(0, 0).c]), "left open"),
    (broken(execute=lambda e: [I.setvl(9)] + e[1:]), "BAD_DIMS"),
    (broken(load=lambda ld: [I.ld(0, 1, K, N)] + ld[1:]), "load task"),
    (broken(store=lambda s: [I.st(0, 0, 1, K + 1, N)] + s[1:]), "store task"),
    (broken(store=lambda s: [I.st(0, 0, 1, K, 2)] + s[1:]), "the stride is 1"),
])
def test_problems_are_refused(progs, says):
    problems = check(CFG, progs)
    assert any(says in p for p in problems), problems
    with pytest.raises(AsmError):
        assemble(CFG, progs, tx_id=1)


def test_a_write_past_the_blocks_rows_is_refused():
    """``wf.vsse`` at ``VL`` beyond the block's ``k`` rows (review R4): the read side refused it, the
    write side now does too."""
    k = 4
    p = Programs(load=[I.ld(0, 0, k, 4)],
                 execute=[I.setvl(8), I.vmv_v_i(0, 1), I.vsse(0, 0, 0, 1), I.setvl(k),
                          I.vlse(1, 0, 0, 1), I.qpop(0), I.qpush(0)],
                 store=[I.st(0, 0, 0, k, 4)])
    assert any("VL <= 4" in s for s in check(CFG, p))


# --- one program, split (F6); the deadlock check (R3) ----------------------------------------------


def test_one_program_splits_into_the_three():
    from waveflow.vecunit.asm import split

    p = job()
    one = [p.load[0], p.load[1]] + p.execute + p.store
    q = split(one)
    assert (q.load, q.execute, q.store) == (p.load, p.execute, p.store)
    assert check(CFG, q) == []


def test_a_job_that_deadlocks_on_two_block_queues_is_refused():
    """Three blocks into qi0 before execute reads qi1: the load task waits for a third qi0 block, the
    execute task for qi1's (review R3)."""
    ld = [I.ld(0, 0, 8, 4)] * 3 + [I.ld(1, 0, 8, 4)]
    ex = [I.setvl(8), I.vlse(0, 1, 0, 1), I.qpop(1)]
    for _ in range(3):
        ex += [I.vlse(1, 0, 0, 1), I.qpop(0)]
    ex += [I.vsse(0, 0, 0, 1), I.qpush(0)]
    p = Programs(load=ld, execute=ex, store=[I.st(0, 0, 0, 8, 4)])
    problems = check(CFG, p)
    assert any("deadlock" in s and "the load task at wf.ld qi0" in s
               and "the execute task at wf.vlse v0, qi1" in s for s in problems), problems
    p.load = [I.ld(0, 0, 8, 4), I.ld(1, 0, 8, 4), I.ld(0, 0, 8, 4), I.ld(0, 0, 8, 4)]
    assert check(CFG, p) == []  # the same job with qi1's block loaded second runs


def _small_job(rng):
    """Balanced programs over two input queues and one output queue, in random orders."""
    nin = [int(rng.integers(0, 6)), int(rng.integers(0, 6))]
    nout = int(rng.integers(0, 5))
    loads = [q for q in (0, 1) for _ in range(nin[q])]
    rng.shuffle(loads)
    uses = [("i", q) for q in (0, 1) for _ in range(nin[q])] + [("o", 0)] * nout
    rng.shuffle(uses)
    ex = [I.setvl(2), I.vmv_v_i(0, 1)]
    for side, q in uses:
        ex += ([I.vlse(1, q, 0, 1), I.qpop(q)] if side == "i" else [I.vsse(0, 0, 0, 1), I.qpush(0)])
    return Programs(load=[I.ld(q, 0, 2, 4) for q in loads], execute=ex,
                    store=[I.st(0, 0, 0, 2, 4)] * nout)


def _completes_every_way(progs) -> tuple[bool, bool]:
    """Every interleaving of the three programs, one instruction at a time, on two-block queues:
    ``(some order finishes, some order deadlocks)``."""
    from functools import cache

    L_, X, S_ = progs.load, progs.execute, progs.store

    @cache
    def go(a, b, c, av0, av1, h0, h1, pushed, tail):
        if (a, b, c) == (len(L_), len(X), len(S_)):
            return True, False
        av, hd = [av0, av1], [h0, h1]
        nxt = []
        if a < len(L_) and av[L_[a].rd] + hd[L_[a].rd] < 2:
            av2 = list(av)
            av2[L_[a].rd] += 1
            nxt.append((a + 1, b, c, *av2, h0, h1, pushed, tail))
        if b < len(X):
            x = X[b]
            if x.opcode == I.Opcode.QUEUE and x.funct == I.QOp.VLSE:
                q = x.rs1
                if hd[q]:
                    nxt.append((a, b + 1, c, av0, av1, h0, h1, pushed, tail))
                elif av[q]:
                    av2, hd2 = list(av), list(hd)
                    av2[q] -= 1
                    hd2[q] = True
                    nxt.append((a, b + 1, c, *av2, *hd2, pushed, tail))
            elif x.opcode == I.Opcode.CTRL and x.funct == I.COp.QPOP:
                hd2 = list(hd)
                hd2[x.rs1] = False
                nxt.append((a, b + 1, c, av0, av1, *hd2, pushed, tail))
            elif x.opcode == I.Opcode.QUEUE and x.funct == I.QOp.VSSE:
                if tail or pushed < 2:
                    nxt.append((a, b + 1, c, av0, av1, h0, h1, pushed, True))
            elif x.opcode == I.Opcode.CTRL and x.funct == I.COp.QPUSH:
                nxt.append((a, b + 1, c, av0, av1, h0, h1, pushed + 1, False))
            else:
                nxt.append((a, b + 1, c, av0, av1, h0, h1, pushed, tail))
        if c < len(S_) and pushed:
            nxt.append((a, b, c + 1, av0, av1, h0, h1, pushed - 1, tail))
        if not nxt:
            return False, True
        res = [go(*n) for n in nxt]
        return any(r[0] for r in res), any(r[1] for r in res)

    return go(0, 0, 0, 0, 0, False, False, 0, False)


def test_the_deadlock_check_agrees_with_every_interleaving():
    """The check runs one order; on small jobs every order is tried: a job finishes in one order
    exactly when it finishes in all of them, and the check says which."""
    from waveflow.vecunit.asm import schedule_problems

    f = fmt(12, 4)
    cfg = VecUnitConfig(group_formats=(f,) * 4, scalar_formats=(f,), in_queue_formats=(f, f),
                        out_queue_formats=(f,), L=4, VLMAX=2, Kmax=2, Nmax=4)
    rng = np.random.default_rng(21)
    seen = {True: 0, False: 0}
    for _ in range(300):
        p = _small_job(rng)
        some_finish, some_deadlock = _completes_every_way(p)
        assert some_finish != some_deadlock  # order-independent: never both
        ok = schedule_problems(cfg, p) == []
        assert ok == some_finish
        seen[ok] += 1
    assert seen[True] > 30 and seen[False] > 30
