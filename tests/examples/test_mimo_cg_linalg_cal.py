"""Step 7.5's study tooling (``examples/mimo_cg/hw/linalg_cal.py``): the pre-registered builds."""

from __future__ import annotations

from examples.mimo_cg.hw import linalg_cal as LC


def test_space_and_split():
    space = LC.space()
    assert all(LC.valid(c) for c in space)
    fit, hold = LC.fit_set(), LC.holdout_set()
    assert len(fit) == 29 and len(set(fit)) == 29
    assert len(hold) == LC.N_HOLDOUT == 12 and not set(fit) & set(hold)
    assert set(fit) <= set(space) and set(hold) <= set(space)
    assert LC.CENTRE in fit


def test_split_file_is_the_rule():
    assert LC.SPLIT.read_text(encoding="utf-8") == LC.split_text()
    roles = LC.load_split()
    assert roles["fit"] == LC.fit_set() and roles["holdout"] == LC.holdout_set()


def test_shapes_are_valid_everywhere():
    for c in LC.fit_set() + LC.holdout_set():
        assert len(LC.shapes(c)) == 4  # shapes() asserts each is a job the core can run


def test_run_7_4_record():
    runs = LC.load_7_4()
    assert set(runs) == {"centre", "smallest"}
    assert len(runs["centre"]["jobs"]) == 10 and len(runs["smallest"]["jobs"]) == 7
    assert runs["centre"]["jobs"][-1]["reply_cycle"] == 2205
    assert runs["centre"]["jobs"][8]["status"] == "BAD_LENGTH"
    assert (
        runs["centre"]["jobs"][8]["n"] == 16
        and runs["centre"]["jobs"][8]["n_payload"] == 8
    )


def test_second_round_split():
    fit2, hold2 = LC.fit2_set(), LC.holdout2_set()
    first = set(LC.fit_set()) | set(LC.holdout_set())
    assert len(fit2) == 20 and len(hold2) == LC.N_HOLDOUT == 12
    assert not (set(fit2) & first) and not (set(hold2) & (first | set(fit2)))
    assert LC.SPLIT_V2.read_text(encoding="utf-8") == LC.split_v2_text()
    for c in fit2 + hold2:
        s = LC.shapes(c, reject=True)
        assert len(s) == 5 and s[-1][0] == LC.REJECT_OP
