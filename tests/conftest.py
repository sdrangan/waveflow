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
WANT_XSI_GATES = 76

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
