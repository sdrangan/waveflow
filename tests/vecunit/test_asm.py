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
