"""``waveflow.events``: timing spans, their nesting, the log's safety, and the analysis.

The log is appended to by several writers at once in practice -- the MCP server and a build, two
builds of one project, the threads of an MCP server -- so the concurrency test is the important
one.  On Windows, append mode alone lets two processes overwrite each other's lines; the test
fails without the file lock in ``_append_line``.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from waveflow import events
from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.trace_steps import xsi_phases


@pytest.fixture(autouse=True)
def _events_on(monkeypatch):
    # tests/conftest.py turns the log off for the suite; these tests are about the log.
    monkeypatch.setenv(events.ENV_OFF, "on")
    monkeypatch.delenv(events.ENV_FILE, raising=False)


# ---------------------------------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------------------------------

_WRITER = textwrap.dedent("""
    import sys, threading
    from waveflow import events
    proc, n_threads, n_events = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    def work(t):
        for i in range(n_events):
            # A long attribute, so a line is far bigger than one small write.
            events.record("test", f"p{proc}-t{t}-{i}", pad="x" * 3000)
    threads = [threading.Thread(target=work, args=(t,)) for t in range(n_threads)]
    for th in threads: th.start()
    for th in threads: th.join()
""")


def test_concurrent_processes_and_threads_lose_and_mangle_nothing(tmp_path):
    log = tmp_path / "events.jsonl"
    env = {**__import__("os").environ, events.ENV_FILE: str(log), events.ENV_OFF: "on"}
    n_proc, n_threads, n_events = 4, 4, 150
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, str(p), str(n_threads), str(n_events)],
                              env=env) for p in range(n_proc)]
    assert all(p.wait(timeout=300) == 0 for p in procs)

    lines = log.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines]            # raises on a mangled line
    names = {e["name"] for e in parsed}
    assert len(parsed) == n_proc * n_threads * n_events
    assert len(names) == len(parsed), "a line was written twice or overwritten"


# ---------------------------------------------------------------------------------------------------
# Spans and nesting
# ---------------------------------------------------------------------------------------------------


@dataclass(kw_only=True)
class _Leaf(BuildStep):
    def run(self, config, **_):
        return {}


@dataclass(kw_only=True)
class _Outer(BuildStep):
    """A step that runs a DAG of its own, the way csynth and system_xsi do."""

    def run(self, config, **_):
        inner = BuildDag()
        inner.add(_Leaf(name="inner_a"))
        with events.span("tool", "vitis-run"):
            pass
        inner.run(config, force=True)
        return {}


def test_a_nested_dag_nests_under_the_step_that_ran_it(tmp_path):
    dag = BuildDag()
    dag.add(_Outer(name="outer_csynth"))
    with events.collect() as spans:
        dag.run(BuildConfig(root_dir=tmp_path), force=True)
    by_name = {e["name"]: e for e in spans}
    outer = by_name["outer_csynth"]
    assert by_name["inner_a"]["parent"] == outer["id"]
    assert by_name["vitis-run"]["parent"] == outer["id"]
    # Logged to the build's own project.
    logged = events.load_events(tmp_path)
    assert {e["name"] for e in logged} == {"outer_csynth", "inner_a", "vitis-run"}
    # A tool inside a csynth step is charged to synth.
    analysis = events.analyze_events(tmp_path)
    by = {s["name"]: s for s in analysis["by_name"]}
    assert by["vitis-run"]["category"] == "synth" and by["inner_a"]["category"] == "synth"
    tree = events.format_tree(spans)
    assert tree.index("outer_csynth") < tree.index("  inner_a")


def test_the_log_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv(events.ENV_OFF, "off")
    dag = BuildDag()
    dag.add(_Leaf(name="only"))
    with events.collect() as spans:
        dag.run(BuildConfig(root_dir=tmp_path), force=True)
    assert [e["name"] for e in spans] == ["only"]        # the caller still gets them
    assert not (tmp_path / events.EVENTS_FILE).exists()  # but nothing is written


def test_each_second_is_charged_once_to_the_most_specific_category():
    def ev(i, name, a, b, parent=None, kind="step"):
        return {"id": i, "parent": parent, "kind": kind, "name": name, "start": a, "end": b,
                "elapsed": b - a, "ok": True}
    evs = [ev("b", "build_all", 0, 100),                 # other build, around everything
           ev("s", "csynth", 0, 60, "b"),
           ev("x", "system_xsi", 60, 90, "b"),
           ev("p", "pysim", 90, 95, "b")]
    cats = events.analyze_events(events=evs)["categories"]
    assert cats == {"synth": 60, "rtl sim": 30, "pysim": 5, "other build": 5, "mcp": 0}


# ---------------------------------------------------------------------------------------------------
# XSI phases
# ---------------------------------------------------------------------------------------------------


def test_xsi_phases_from_run_bat_and_run_sh():
    bat = ("WF_PHASE compile_rtl  9:59:58.50\nxvlog ...\nWF_PHASE elaborate 10:00:01,50\n"
           "WF_PHASE compile_tb 10:00:10.00\nWF_PHASE simulate 10:00:12.00\nXSI_EXITCODE=0\n"
           "WF_PHASE end 10:00:15.25\n")
    assert xsi_phases(bat) == [("compile_rtl", 3.0), ("elaborate", 8.5), ("compile_tb", 2.0),
                               ("simulate", pytest.approx(3.25))]
    sh = "WF_PHASE compile_rtl 1000.0\nWF_PHASE simulate 1004.5\nWF_PHASE end 1010.0\n"
    assert xsi_phases(sh) == [("compile_rtl", 4.5), ("simulate", 5.5)]
    # Past midnight, %time% wraps.
    assert xsi_phases("WF_PHASE simulate 23:59:59.00\nWF_PHASE end  0:00:01.00\n") == [
        ("simulate", 2.0)]
