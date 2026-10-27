"""The vector unit's program-examples page shows assembly only: every ``text`` block on it is marked
``<!-- listing: NAME -->`` and equals what :data:`LISTINGS` ``[NAME]`` prints from the toy kernels
(``examples/vecunit/toy.py``), so a listing cannot go stale.  An unmarked block, or a name with no
listing, fails."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import numpy as np

from examples.vecunit import toy as T
from waveflow.vecunit.asm import check
from waveflow.vecunit.isa import disassemble
from waveflow.vecunit.unit import VecUnit, job_features

PAGE = Path(__file__).resolve().parents[2] / "docs/guide/dsp/vecunit/program_examples.md"

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


LISTINGS = {
    "double.load_store": lambda: "\n".join([
        "# load program", asm(T.double_programs(16, 4).load),
        "# store program", asm(T.double_programs(16, 4).store)]),
    "double.unchained": lambda: asm(T.double_programs(16, 4, chain=False).execute),
    "double.chained": lambda: asm(T.double_programs(16, 4).execute),
    "double.checks": double_checks,
    "double.costs": double_costs,
}

_BLOCK = re.compile(r"(?:<!-- listing: (?P<name>[\w.]+) -->\n)?```text\n(?P<body>.*?)```", re.S)


def blocks() -> list[tuple[str | None, str]]:
    return [(m["name"], m["body"].rstrip("\n")) for m in _BLOCK.finditer(PAGE.read_text("utf-8"))]


def test_every_text_block_is_a_checked_listing():
    found = blocks()
    assert found, "no listings on the page"
    assert [b for b in found if b[0] is None] == [], "a text block without a listing marker"
    unknown = {n for n, _ in found} - set(LISTINGS)
    assert not unknown, f"markers with no listing: {sorted(unknown)}"


def test_listings_match_the_toy_kernels():
    for name, body in blocks():
        assert body == LISTINGS[name](), f"listing {name!r} differs from what the toy prints"


def test_unchained_and_chained_double_give_the_same_bits():
    """What the page says in words: chaining changes when results are computed, not what they are."""
    xr, xi = T.random_matrix(16, 4, seed=1)
    a = T.run(CFG, T.double_programs(16, 4, chain=False), [(xr, xi)])
    b = T.run(CFG, T.double_programs(16, 4), [(xr, xi)])
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])
