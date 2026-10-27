"""The vector unit's program-examples and instruction-set pages show assembly and tables, not Python:
every ``text`` block on them is marked ``<!-- listing: NAME -->`` and equals what
:data:`LISTINGS` ``[NAME]`` prints (from the toy kernels, ``examples/vecunit/toy.py``, and the ISA), so
a listing cannot go stale.  An unmarked block, or a name with no listing, fails."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from examples.vecunit import toy as T
from waveflow.vecunit import isa as I
from waveflow.vecunit.asm import check
from waveflow.vecunit.isa import disassemble
from waveflow.vecunit.unit import VecUnit, job_features

PAGES = [Path(__file__).resolve().parents[2] / "docs/guide/dsp/vecunit" / name
         for name in ("program_examples.md", "isa.md")]

CFG = T.toy_config()
ONE_SLOT = replace(CFG, S=1, slot_caps=())  # the toy unit, built with one slot


def asm(prog) -> str:
    return "\n".join(disassemble(x) for x in prog)


def double_checks() -> str:
    unchained, chained = T.double_programs(16, 4, chain=False), T.double_programs(16, 4)

    def verdict(cfg, p) -> str:
        problems = check(cfg, p)
        return "accepted" if not problems else problems[0]

    return "\n".join([f"one slot,    unchained:  {verdict(ONE_SLOT, unchained)}",
                      f"one slot,    chained:    {verdict(ONE_SLOT, chained)}",
                      f"three slots, unchained:  {verdict(CFG, unchained)}",
                      f"three slots, chained:    {verdict(CFG, chained)}"])


def double_costs() -> str:
    rows = ["           passes  runs  elements  cycles (about)"]
    for name, chain in (("unchained", False), ("chained", True)):
        f = job_features(T.double_programs(16, 4, chain=chain).execute, CFG)
        rows.append(f"{name:10} {f['decode']:6} {f['run']:5} {f['elem']:9} "
                    f"{VecUnit.timing.predict(f):9.0f}")
    return "\n".join(rows)


def costs(rows: list[tuple[str, object, object]]) -> str:
    """A cost table: per ``(name, programs, cfg)``, the execute program's passes, runs and elements,
    and the calibrated model's cycles."""
    out = ["           passes  runs  elements  cycles (about)"]
    for name, p, cfg in rows:
        f = job_features(p.execute, cfg)
        out.append(f"{name:10} {f['decode']:6} {f['run']:5} {f['elem']:9} "
                   f"{VecUnit.timing.predict(f):9.0f}")
    return "\n".join(out)


def programs(p) -> str:
    return "\n".join(["# load program", asm(p.load), "# execute program", asm(p.execute),
                      "# store program", asm(p.store)])


POLY = (0.375, -0.5, 0.25)
FUSED = T.toy_config(fuse_raw_vl=16)


def refusals() -> str:
    from waveflow.vecunit.asm import Programs

    four = Programs(load=[I.ld(0, 0, 4, 4), I.ld(1, 0, 4, 4)],
                    execute=[I.setvl(4), I.vlse(0, 0, 0, 1).c, I.vlse(1, 1, 0, 1).c,
                             I.vmul_vv(2, 0, 1, conj=True).c, I.vsse(0, 2, 0, 1),
                             I.qpop(0), I.qpop(1), I.qpush(0)],
                    store=[I.st(0, 0, 0, 4, 4)])
    conflict = Programs(load=[I.ld(0, 0, 4, 4)],
                        execute=[I.setvl(4), I.vlse(0, 0, 0, 1), I.vlse(2, 0, 0, 1),
                                 I.vadd_vv(1, 0, 2).c, I.vsse(0, 1, 0, 1), I.qpop(0), I.qpush(0)],
                        store=[I.st(0, 0, 0, 4, 4)])
    deadlock = Programs(load=[I.ld(0, 0, 4, 4)] * 3 + [I.ld(1, 0, 4, 4)],
                        execute=[I.setvl(4), I.vlse(0, 1, 0, 1), I.qpop(1)]
                        + [I.vlse(1, 0, 0, 1), I.qpop(0)] * 3 + [I.vsse(0, 0, 0, 1), I.qpush(0)],
                        store=[I.st(0, 0, 0, 4, 4)])
    unread = Programs(load=[I.ld(0, 0, 4, 4)],
                      execute=[I.setvl(4), I.vadd_vv(1, 0, 0), I.vsse(0, 1, 0, 1),
                               I.vlse(0, 0, 0, 1), I.qpop(0), I.qpush(0)],
                      store=[I.st(0, 0, 0, 4, 4)])
    out = []
    for name, cfg, p in (("four slots", CFG, four), ("bank conflict", replace(CFG, NB=2), conflict),
                         ("never written", CFG, unread), ("deadlock", CFG, deadlock)):
        out.append(f"# {name}")
        out += check(cfg, p)[:1]
    return "\n".join(out)


def one_program() -> str:
    from waveflow.vecunit.asm import split

    p = T.double_programs(16, 4)
    one = p.load + p.execute + p.store
    q = split(one)
    assert (q.load, q.execute, q.store) == (p.load, p.execute, p.store)
    return asm(one)


def q_examples() -> str:
    """``q_d(x)`` on a small format, in each rounding and overflow mode (exact inputs)."""
    from waveflow.utils import fixputils as fx
    from waveflow.utils.fixputils import Format, OMode, QMode

    src = Format(16, 8, True)  # holds every x below exactly
    modes = [("AP_RND, AP_SAT", QMode.AP_RND, OMode.AP_SAT), ("AP_TRN, AP_SAT", QMode.AP_TRN, OMode.AP_SAT),
             ("AP_RND, AP_WRAP", QMode.AP_RND, OMode.AP_WRAP)]
    out = ["d: 8 bits, 3 integer, 5 fraction (steps of 1/32), from -4 to 3.96875",
           f"{'x':>10}  " + "".join(f"{name:>17}" for name, _q, _o in modes)]
    for x in (0.296875, 5.0, -0.015625):
        row = f"{x:>10}  "
        for _name, q, o in modes:
            d = Format(8, 3, True, q, o)
            s = fx.quantize(int(round(x * 2 ** src.frac_bits)), src, d)
            row += f"{float(fx.to_float(s, d)):>17}"
        out.append(row)
    return "\n".join(out)


def encoding_example() -> str:
    x = I.vmacc_vs(0, 2, 2).c
    word = x.encode()
    out = [f"{x}  ->  0x{word:016x}", "field     bits    value"]
    pos = 0
    for name, bits in I._FIELDS:
        v = (word >> pos) & ((1 << bits) - 1)
        out.append(f"{name:8}  {pos + bits - 1:>2}:{pos:<2}   {v}")
        pos += bits
    return "\n".join(out)


def operand_forms() -> str:
    return "\n".join(str(x) for x in (I.vmul_vv(2, 0, 1), I.vmul_vv(2, 0, 1, conj=True),
                                      I.vmul_vv(2, 0, 1, bcast=True),
                                      I.vmul_vv(2, 0, 1, conj=True, bcast=True)))


LISTINGS = {
    "isa.q": q_examples,
    "isa.encoding": encoding_example,
    "isa.forms": operand_forms,
    "double.load_store": lambda: "\n".join([
        "# load program", asm(T.double_programs(16, 4).load),
        "# store program", asm(T.double_programs(16, 4).store)]),
    "double.unchained": lambda: asm(T.double_programs(16, 4, chain=False).execute),
    "double.chained": lambda: asm(T.double_programs(16, 4).execute),
    "double.checks": double_checks,
    "double.costs": double_costs,
    "cmul.chunk": lambda: programs(T.cmul_programs(CFG, 16, 4)),
    "cmul.chunk.costs": lambda: costs([("cmul", T.cmul_programs(CFG, 16, 4), CFG)]),
    "cmul.strip": lambda: programs(T.cmul_programs(CFG, 40, 4)),
    "gain": lambda: asm(T.gain_programs(16, 4).execute),
    "poly": lambda: asm(T.poly_programs(CFG, 16, 4, *POLY).execute),
    "poly.costs": lambda: costs([("poly", T.poly_programs(CFG, 16, 4, *POLY), CFG),
                                 ("fused", T.poly_programs(FUSED, 16, 4, *POLY), FUSED)]),
    "cmul.rs": lambda: asm(T.cmul_programs(CFG, 16, 4, rs=3).execute),
    "rs.own": lambda: "\n".join(str(x) for x in (I.vmul_vs(1, 0, 0, rs=2), I.vmacc_vv(0, 1, 2, rs=0),
                                                 I.vdotr(1, 0, 0, rs=4))),
    "one_program": one_program,
    "refusals": refusals,
}

_BLOCK = re.compile(r"(?:<!-- listing: (?P<name>[\w.]+) -->\n)?```text\n(?P<body>.*?)```", re.S)


def blocks(page: Path) -> list[tuple[str | None, str]]:
    return [(m["name"], m["body"].rstrip("\n")) for m in _BLOCK.finditer(page.read_text("utf-8"))]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_text_block_is_a_checked_listing(page):
    found = blocks(page)
    assert found, f"no listings on {page.name}"
    assert [b for b in found if b[0] is None] == [], f"a text block without a listing marker on {page.name}"
    unknown = {n for n, _ in found} - set(LISTINGS)
    assert not unknown, f"markers with no listing: {sorted(unknown)}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_listings_match_what_they_print(page):
    for name, body in blocks(page):
        assert body == LISTINGS[name](), f"listing {name!r} on {page.name} differs from what it prints"


def test_unchained_and_chained_double_give_the_same_bits():
    """What the page says in words: chaining changes when results are computed, not what they are."""
    xr, xi = T.random_matrix(16, 4, seed=1)
    a = T.run(CFG, T.double_programs(16, 4, chain=False), [(xr, xi)])
    b = T.run(CFG, T.double_programs(16, 4), [(xr, xi)])
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])
