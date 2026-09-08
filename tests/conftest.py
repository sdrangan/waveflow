"""Session-level gates for the ``-m xsi`` suite: **a skip must not read as a pass.**

Three times in one arc a session reported a green XSI run having measured almost nothing, and each
time a human caught it by noticing a number was implausible -- never a failing test.  ``23 skipped``
in a summary line is not a signal anybody reads under time pressure, and the individual gates are
right to skip: ``<example>/<top>_proj/`` is gitignored build output, so a gate looking at RTL it did
not produce must decline to measure rather than report a cycle count as a behaviour change.  (See
:func:`waveflow.build.trace_steps.rtl_staleness`.)

So the skip stays where it is, and the *session* is what fails.  Two assertions, both cheap:

``no XSI gate may skip``
    If one does, the run fails and names it with its reason.  This is the general shape, not the
    specific bug: it catches the next reason a gate declines to run, not only the last one.

``a full XSI run must collect at least WANT_XSI_GATES gates``
    A skip is one way to measure nothing; a gate file that stops being collected -- deleted,
    renamed, or broken by an import error -- is the other, and it leaves no ``FAILED`` line at all.
    That exact shape already fooled a suite gate written as ``grep -c "^FAILED"``, which reported
    zero failures against a baseline of six because a collection error produces no such lines.

This is deliberately separate from the content-digest fix in :mod:`waveflow.build.rtl_digest`.  That
one removes the *false* skips; this makes a *true* skip visible.  Both are needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

#: How many ``xsi``-marked tests a full ``-m xsi`` run collects.  Recorded the way a gate's
#: ``WANT_CYCLES`` is: a measured number, updated **only** when gates are deliberately added or
#: removed, and never nudged downward to make a run go green.  A run that collects fewer has lost a
#: gate -- most likely to a collection error, which is silent in every other reading of the output.
#:
#: To update: ``pytest -m xsi --collect-only -q`` and sum the per-file counts.
#:
#: 87 -> 97 on 2026-09-07 (``plans/lt_transient.md`` S2): ``test_rf_shot_tx_xsi.py`` went 20 -> 30.
#: One byte-identical-from-t=0 comparison (2 items) retired, and the three LT gates took its place —
#: phase and transient per backend and scenario (4 each), agreement per scenario (2), and the
#: derived-bound check (2).  A deliberate addition, which is the only reason this number ever moves.
#:
#: 97 -> 98 on 2026-09-07 (``plans/rf_shot_geometry.md``): the same file went 30 -> 31.  Removing
#: ``base`` removed the ``base + offset`` arithmetic its gate covered, and
#: ``test_the_player_sweeps_the_whole_buffer_and_wraps`` gates what replaced it — the read pointer's
#: wrap at ``depth``, which is the only address arithmetic the design still has.  No gate was lost:
#: the two retired VERDICTS (``SHOT_ZERO_LEN`` and ``SHOT_WRONG_LEN``'s length half) were asserted
#: inside gates that survive, not by gates of their own.
#:
#: 98 -> 115 on 2026-09-07 (``plans/rf_shot_absolute.md``): a new file,
#: ``test_rf_shot_tx_abs_xsi.py``, collects **17**.  ``absolute_index`` is a template argument, so
#: the two settings are two pieces of RTL and the mode needs its own csynth and its own xsim
#: snapshot — a gate that only ever elaborated the default would be asserting the mode's behaviour
#: against a simulator.  The 17 are: the two feature gates (address-is-phase, boundary start) and
#: their negative control, the ``nrep`` gate, block shape and deferral cost per scenario (4), the
#: loop stream's preemption gate, DAC and verdict counters per scenario (4), cross-backend
#: agreement, II, the template argument, and the pysim rung's record.  ``test_rf_shot_tx_xsi.py``
#: is unchanged at 31: the default build is the negative control, so every one of its numbers had
#: to keep its meaning, and every one did.
WANT_XSI_GATES = 115

#: Filled in at collection; module state because a pytest run is one process and the hooks that
#: write and read it are plain functions.
_XSI_SELECTED: set[str] = set()
_XSI_SKIPPED: dict[str, str] = {}


def _is_xsi_session(items) -> bool:
    """True when this session is *about* the XSI gates -- every collected test is one.

    That is what ``-m xsi`` produces, and what a targeted run of a single gate file under ``-m xsi``
    produces.  A plain ``pytest`` over the whole tree is not an XSI session even though it collects
    these tests, and must not be failed for skipping them: someone without Vivado installed is
    entitled to run the suite and see the toolchain gates step aside.
    """
    return bool(items) and all(i.get_closest_marker("xsi") for i in items)


def _is_narrowed(config) -> bool:
    """True when collection was restricted to particular files or tests.

    The floor is a claim about the *whole* suite, so it cannot apply to ``pytest <one_file> -m xsi``
    -- that run is supposed to collect five gates, not sixty-three.  The no-skip rule still does.
    """
    if getattr(config.option, "keyword", None) or getattr(config.option, "deselect", None):
        return True
    if getattr(config.option, "lf", False) or getattr(config.option, "failedfirst", False):
        return True
    for arg in config.args:
        head = str(arg).split("::")[0]
        if "::" in str(arg) or Path(head).is_file():
            return True
    return False


def pytest_collection_finish(session):
    """Record the xsi gates that survived collection *and* deselection.

    ``pytest_collection_finish`` rather than ``pytest_collection_modifyitems`` because the ``-m``
    expression does its deselecting in the latter, and hook order between plugins is not something
    to depend on: this one is handed the final list.
    """
    _XSI_SELECTED.clear()
    _XSI_SKIPPED.clear()
    if _is_xsi_session(session.items):
        _XSI_SELECTED.update(i.nodeid for i in session.items)


def pytest_runtest_logreport(report):
    if report.nodeid in _XSI_SELECTED and report.skipped:
        _XSI_SKIPPED.setdefault(report.nodeid, _skip_reason(report))


def _skip_reason(report) -> str:
    """The message the gate skipped with -- the part that says what to rebuild."""
    lr = report.longrepr
    if isinstance(lr, tuple) and len(lr) == 3:
        text = str(lr[2])
        return text[len("Skipped: "):] if text.startswith("Skipped: ") else text
    return str(lr)


def _problems(config) -> list[str]:
    """The session-level failures, as lines to print.  Empty when the run really did measure."""
    if not _XSI_SELECTED:
        return []
    out: list[str] = []
    if _XSI_SKIPPED:
        out.append(f"{len(_XSI_SKIPPED)} of {len(_XSI_SELECTED)} XSI gates SKIPPED -- this session "
                   f"measured less than it appears to have:")
        for nodeid, why in sorted(_XSI_SKIPPED.items()):
            out.append(f"  {nodeid}")
            out.append(f"      {why}")
    if not _is_narrowed(config) and len(_XSI_SELECTED) < WANT_XSI_GATES:
        out.append(f"only {len(_XSI_SELECTED)} XSI gates collected, expected at least "
                   f"{WANT_XSI_GATES} (tests/conftest.py::WANT_XSI_GATES). A gate file that fails "
                   f"to import produces no FAILED line at all -- check the collection errors above "
                   f"before adjusting the number.")
    return out


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    problems = _problems(config)
    if not problems:
        return
    terminalreporter.write_sep("=", "XSI SESSION GATE FAILED", red=True, bold=True)
    for line in problems:
        terminalreporter.write_line(line)


def pytest_sessionfinish(session, exitstatus):
    """Fail the *run*, not the gates.

    Skipping is the right thing for an individual gate to do -- see the module docstring.  What must
    not happen is the session reporting success afterwards.  An already-failing run keeps its own
    exit status; this only turns a green one red.
    """
    if _problems(session.config) and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
