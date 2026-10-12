"""AC9 of plans/sionna_rt/sionna_rt.md: everything outside the ``sionna`` marker runs without Sionna.

The venv this runs in has the ``sionna`` extra, so "without" is made on purpose: a subprocess runs
pytest with the import blocker (``tests/rfenv/_block_sionna.py``) loaded as a plugin, over every
non-``sionna`` test of the RF environment -- ``tests/rfenv/`` and the example's tests once they
exist -- and must pass.  This file leaves itself out of that run.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_every_non_sionna_test_passes_without_sionna():
    targets = ["tests/rfenv/"] + sorted(
        str(p.relative_to(REPO))
        for p in (REPO / "tests" / "examples").glob("test_rt_sounder*.py")
    )
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.rfenv._block_sionna",
            "-m",
            "not sionna and not xsi and not vitis",
            "--ignore",
            "tests/rfenv/test_no_sionna.py",
            *targets,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    out = run.stdout + run.stderr
    assert run.returncode == 0, out[-4000:]
    passed = re.search(r"(\d+) passed", out)
    assert passed and int(passed.group(1)) > 50, out[-2000:]
    assert " failed" not in out and " error" not in out, out[-2000:]
    assert "skipped" not in out, out[
        -2000:
    ]  # a module-level importorskip would hide here
