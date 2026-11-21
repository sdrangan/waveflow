"""The reference plan's golden and its pricing script: the checks plans/spec.md and plans/eval.md quote."""
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import golden as g

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("WF_REPO", HERE.parents[3]))   # price.py imports examples/ from the repo


def test_edge_cases_are_refused_or_empty():
    H, s2, Y, _ = g.scenario(u=4)
    assert g.mmse_filter(H[:, :0], s2).shape == (0, 32)           # U = 0: nothing to detect
    for bad in (0.0, -1.0, np.nan):
        with pytest.raises(ValueError):
            g.mmse_filter(H, bad)                                  # MMSE needs sigma2 > 0
    with pytest.raises(ValueError):
        g.mmse_filter(np.ones((4, 8), complex), s2)                # U > B
    with pytest.raises(ValueError):
        g.mmse_filter(np.ones((32, 9), complex), s2)               # U > 8


def test_cg_is_exact_at_u_iterations_and_5_suffice_at_8_users():
    worst5 = max(g.evm_db(g.detect(g.cg_filter(H, s2, 5), Y), g.detect(g.mmse_filter(H, s2), Y))
                 for H, s2, Y, _ in (g.scenario(u=8, seed=s) for s in range(20)))
    assert worst5 < -35                                            # spec.md: -38.1 dB
    H, s2, Y, _ = g.scenario(u=4)
    assert np.allclose(g.cg_filter(H, s2, 4), g.mmse_filter(H, s2))


def test_the_proposed_formats_cost_nothing_against_the_noise():
    for s in range(20):
        H, s2, Y, _ = g.scenario(u=8, seed=s)
        assert np.abs(g.mmse_filter(H, s2)).max() < 2              # FMT_W's range
        assert g.evm_db(g.fixed_detect(H, s2, Y), g.detect(g.mmse_filter(H, s2), Y)) < -70


def test_price_prints_the_numbers_eval_md_quotes():
    r = subprocess.run([sys.executable, str(HERE / "price.py")], cwd=REPO, capture_output=True,
                       text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    eval_md = (HERE / "plans" / "eval.md").read_text(encoding="utf-8")
    for number in ("457.88", "1,536.86", "149,204.00", "244,593.00", "137,287.00", "37,690.00",
                   "5,455.00", "859.37"):
        assert number in r.stdout, number
    for quoted in ("457.9", "1,537", "149,204", "244,593", "137,287", "37,690", "5,455"):
        assert quoted in eval_md, quoted
