"""``WANT_SIONNA_GATES`` is a measured number, and this is the measurement (M1 review, amendment A6).

The same check ``tests/build/test_xsi_session_gate.py`` makes for the XSI gates: ask pytest to
COLLECT the ``sionna``-marked tests, in a subprocess, and compare with the floor
``tests/conftest.py`` records.  Collection needs no Sionna -- the test modules import it only inside
:mod:`waveflow.rfenv.sionna_backend` -- so this runs in the ordinary dev loop, and adding a gate
without raising the floor fails here.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _conftest():
    spec = importlib.util.spec_from_file_location(
        "waveflow_tests_conftest_sionna", REPO / "tests" / "conftest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_recorded_sionna_floor_matches_what_the_suite_has():
    want = _conftest().WANT_SIONNA_GATES
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "sionna", "--collect-only", "-q"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    out = r.stdout + r.stderr
    counts = [int(m) for m in re.findall(r"^\S+\.py: (\d+)$", out, flags=re.MULTILINE)]
    total = sum(counts)
    assert total == want, (
        f"{total} sionna-marked tests collected, but WANT_SIONNA_GATES is {want}. If gates were "
        f"deliberately added or removed, update tests/conftest.py::WANT_SIONNA_GATES; never lower "
        f"it to make a run go green.\n{out[-3000:]}"
    )
