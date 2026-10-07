"""Step 9.1: the study-table check knows where every committed table comes from.

The check itself (``python -m examples.mimo_cg.tools.study_tables --check``) takes about four
minutes, and each writer's byte-identity already has its own test; this file checks the list.
"""

from __future__ import annotations

from examples.mimo_cg.tools import study_tables as ST


def committed() -> set[str]:
    return {p.stem for p in ST.PAPER.glob("*.csv")}


def test_every_committed_table_is_checked_or_has_an_origin():
    checked = ST.checked_names()
    assert len(checked) == len(set(checked)) == 24
    assert set(checked) <= committed()
    assert not [n for n in checked if ST.origin(n) is not None]
    assert sorted(n for n in committed() - set(checked) if ST.origin(n) is None) == []


def test_every_named_origin_is_a_committed_table():
    exact = {k for k in ST.ELSEWHERE if not k.endswith("*")}
    assert exact <= committed()
    for key in ST.ELSEWHERE:
        if key.endswith("*"):
            assert any(n.startswith(key[:-1]) for n in committed()), key
