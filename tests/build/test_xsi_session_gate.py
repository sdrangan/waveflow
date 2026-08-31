"""tests/build/test_xsi_session_gate.py -- the session gate in ``tests/conftest.py``.

The gate exists because a skipped test and a passing test look the same in a summary line.  It is
itself a piece of test infrastructure that could silently stop working, so its decisions are pinned
here rather than only exercised by the ``-m xsi`` runs it guards -- which need Vivado, and which are
precisely the runs nobody would notice going quiet.

Loaded by path: ``tests/`` is not a package, so there is no import name for the conftest.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_conftest():
    spec = importlib.util.spec_from_file_location(
        "waveflow_tests_conftest", REPO / "tests" / "conftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate():
    """A fresh copy of the conftest module, so mutating its session state cannot leak."""
    return _load_conftest()


def _item(nodeid: str, *, xsi: bool):
    return SimpleNamespace(nodeid=nodeid,
                           get_closest_marker=lambda name, _x=xsi: object() if (
                               name == "xsi" and _x) else None)


def _config(*, args=("tests",), keyword=None, deselect=None, lf=False, failedfirst=False):
    return SimpleNamespace(args=list(args),
                           option=SimpleNamespace(keyword=keyword, deselect=deselect,
                                                  lf=lf, failedfirst=failedfirst))


class TestWhenTheGateIsActive:
    def test_an_all_xsi_selection_is_an_xsi_session(self, gate):
        assert gate._is_xsi_session([_item("a", xsi=True), _item("b", xsi=True)])

    def test_a_mixed_selection_is_not(self, gate):
        """A plain ``pytest`` over the tree collects these too.

        Someone without Vivado is entitled to run the suite and watch the toolchain gates step
        aside; failing that run would make the gate the thing people route around.
        """
        assert not gate._is_xsi_session([_item("a", xsi=True), _item("b", xsi=False)])

    def test_an_empty_selection_is_not(self, gate):
        assert not gate._is_xsi_session([])

    def test_the_usual_dev_loop_selection_is_not(self, gate):
        """``-m "not vitis and not xsi"`` deselects every gate, leaving nothing to assert about."""
        assert not gate._is_xsi_session([_item("a", xsi=False)])


class TestNarrowing:
    def test_a_directory_run_is_not_narrowed(self, gate):
        assert not gate._is_narrowed(_config(args=("tests",)))

    def test_a_single_file_is_narrowed(self, gate):
        rel = "tests/examples/test_rf_samp_buf_rx_xsi.py"
        assert (REPO / rel).is_file(), "the fixture path must exist for this to mean anything"
        assert gate._is_narrowed(_config(args=(str(REPO / rel),)))

    def test_a_single_test_id_is_narrowed(self, gate):
        assert gate._is_narrowed(_config(args=("tests/examples/x.py::test_one",)))

    @pytest.mark.parametrize("kw", [{"keyword": "loopback"}, {"deselect": ["tests/x.py::t"]},
                                    {"lf": True}, {"failedfirst": True}])
    def test_selection_flags_narrow(self, gate, kw):
        assert gate._is_narrowed(_config(**kw))


class TestProblems:
    def test_a_run_that_measured_everything_has_none(self, gate):
        gate._XSI_SELECTED.update(f"t{i}" for i in range(gate.WANT_XSI_GATES))
        assert gate._problems(_config()) == []

    def test_a_skip_is_reported_with_its_reason(self, gate):
        gate._XSI_SELECTED.update(f"t{i}" for i in range(gate.WANT_XSI_GATES))
        gate._XSI_SKIPPED["t7"] = "no csynth RTL at ..."
        problems = "\n".join(gate._problems(_config()))
        assert "SKIPPED" in problems
        assert "t7" in problems, "the failure must name what skipped, not just how many"
        assert "no csynth RTL at ..." in problems

    def test_a_missing_gate_trips_the_floor(self, gate):
        """The shape a collection error takes: fewer gates, and not one ``FAILED`` line."""
        gate._XSI_SELECTED.update(f"t{i}" for i in range(gate.WANT_XSI_GATES - 1))
        problems = "\n".join(gate._problems(_config()))
        assert f"expected at least {gate.WANT_XSI_GATES}" in problems

    def test_the_floor_does_not_apply_to_a_narrowed_run(self, gate):
        """``pytest <one_gate_file> -m xsi`` is supposed to collect five, not sixty-three."""
        gate._XSI_SELECTED.update({"t0", "t1"})
        assert gate._problems(_config(args=("tests/examples/x.py::t",))) == []

    def test_a_narrowed_run_still_may_not_skip(self, gate):
        gate._XSI_SELECTED.update({"t0", "t1"})
        gate._XSI_SKIPPED["t0"] = "stale RTL"
        assert gate._problems(_config(args=("tests/examples/x.py::t",)))

    def test_a_non_xsi_session_is_never_a_problem(self, gate):
        assert gate._problems(_config()) == []


class TestSkipReason:
    def test_unwraps_pytests_skip_triple(self, gate):
        report = SimpleNamespace(longrepr=("x.py", 12, "Skipped: stale RTL, rebuild"))
        assert gate._skip_reason(report) == "stale RTL, rebuild"

    def test_falls_back_to_str_for_anything_else(self, gate):
        assert gate._skip_reason(SimpleNamespace(longrepr="odd shape")) == "odd shape"


def test_the_recorded_floor_matches_what_the_suite_actually_has(gate):
    """``WANT_XSI_GATES`` is a measured number, and this is the measurement.

    Measured by asking pytest to COLLECT, in a subprocess, rather than by counting decorators:
    ``pytest.mark.xsi`` also arrives via ``pytestmark`` and via class decorators, and a parametrized
    gate contributes one test per case -- an approximation of pytest's own arithmetic here would be
    a second thing to keep in sync, and it would drift in the direction of a smaller number.
    Collection needs no toolchain, so this runs in the ordinary dev loop: adding a gate and
    forgetting to raise the floor fails HERE, rather than quietly lowering what a green XSI run is
    allowed to mean.
    """
    import re
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "pytest", "-m", "xsi", "--collect-only", "-q"],
                       cwd=str(REPO), capture_output=True, text=True, timeout=900)
    out = r.stdout + r.stderr
    counts = [int(m) for m in re.findall(r"^\S+\.py: (\d+)$", out, flags=re.M)]
    assert counts, f"could not read a collection summary from:\n{out[-3000:]}"
    total = sum(counts)
    assert total == gate.WANT_XSI_GATES, (
        f"{total} xsi-marked tests collected, but WANT_XSI_GATES is {gate.WANT_XSI_GATES}. "
        f"If gates were deliberately added or removed, update tests/conftest.py::WANT_XSI_GATES; "
        f"never lower it to make a run go green.\n{out[-3000:]}")
