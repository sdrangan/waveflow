"""``waveflow blind-test``: the parts that do not need a live agent.

Running the agent takes minutes and uses up a plan's usage, so it is a manual check.  What is
tested here is everything around it: the refusals that keep the test blind,
the copying of the spec's companion files, and the summary built from a
transcript -- replayed from a synthetic stream-json file shaped like the real
one (recorded from Claude Code 2.1.285).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from waveflow.mcp import blind_test as bt


def _write_transcript(path: Path, folder: Path, repo: Path) -> None:
    def tool(name, /, **inp):
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": inp}]}}

    recs = [
        {"type": "system", "subtype": "init", "model": "m", "tools": [
            "Read", "mcp__waveflow__waveflow_get_process"],
         "mcp_servers": [{"name": "waveflow", "status": "connected"}]},
        tool("Read", file_path=str(folder / "spec.md")),
        tool("mcp__waveflow__waveflow_list_examples"),
        tool("mcp__waveflow__waveflow_get_process", frame="stream_inband"),
        tool("mcp__waveflow__waveflow_get_example", name="stream_inband"),
        tool("Read", file_path=str(repo / "examples" / "good" / "a.py")),
        tool("Read", file_path=str(repo / "plans" / "secret.md")),
        tool("Read", file_path=str(repo / "examples" / "old_thing" / "x.py")),
        tool("Write", file_path=str(folder / "gen" / "k.cpp"), content="x"),
        tool("Bash", command="python build.py --through csim"),
        {"type": "result", "subtype": "success", "num_turns": 7,
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 90000, "output_tokens": 1500}, "modelUsage": {"claude-x": {}}, "session_id": "s1", "result": "stopped for review",
         "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}]},
    ]
    path.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")


def test_summary_reports_leaks_generated_edits_and_denials(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs" / "examples" / "good").mkdir(parents=True)
    (repo / "docs" / "examples" / "good" / "index.md").write_text(
        "---\nexample_dir: examples/good\n---\n", encoding="utf-8")
    folder = tmp_path / "trial"
    folder.mkdir()
    t = tmp_path / "t.jsonl"
    _write_transcript(t, folder, repo)

    phase = {"phase": 1, "message": "build it", "transcript": str(t), "exit_code": 0,
             "timed_out": False, "wall_s": 60.0, "session_id": "s1",
             "result": bt._result_record(t)}
    s = bt.summarize([phase], folder=folder, repo=repo, copied=["spec.md"], allowed=["Read"])

    # The summary leads with the choice.
    assert s.index("## Choice") < s.index("## Waveflow tools")
    assert "- `waveflow_get_process`: `stream_inband`" in s
    assert "- `waveflow_list_frames`: **never called**" in s
    assert "- `waveflow_list_examples`: before the first write" in s
    assert "- first write: tool call #8 of 9" in s
    assert "- references read: `stream_inband`, `good (read from the checkout)`" in s
    assert "- scaffold: not requested" in s
    assert "- direct Vitis / Vivado commands: none" in s
    assert "LEAK: repo plans/" in s
    assert "repo example NOT in TOC" in s
    assert "hand-edited a generated file" in s
    assert "rm -rf x" in s
    assert "python build.py --through csim" in s
    assert "claude-x" in s and "90.0k (90.0k)" in s and "1.5k" in s
    assert "$" not in s.split("## Waveflow tools")[0]


def test_the_default_first_message_names_no_frame_or_example():
    from waveflow.mcp.frames import list_frames
    from waveflow.mcp.knowledge import get_index

    for name in [*list_frames(), *get_index().cards]:
        assert name not in bt.WAVEFLOW_FIRST, name


def test_companions_are_the_linked_markdown_beside_the_spec(tmp_path):
    (tmp_path / "frame.md").write_text("f", encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("Read [frame.md](frame.md) and [web](https://x.org/a.md) "
                    "and [missing](nope.md).", encoding="utf-8")
    assert [p.name for p in bt._companions(spec)] == ["frame.md"]


def test_refuses_a_folder_under_a_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("context", encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("x", encoding="utf-8")
    out = bt.run_blind_test(spec, tmp_path / "trial", silent=True)
    assert "context for the agent" in out["error"]


def test_refuses_a_folder_inside_the_clone():
    repo = bt._repo_root()
    assert repo is not None
    out = bt.run_blind_test(repo / "README.md", repo / "scratch_trial", silent=True)
    assert "inside the Waveflow clone" in out["error"]
    assert not (repo / "scratch_trial").exists()


def test_login_note_distinguishes_subscription_from_api_key():
    # A subscription login reports apiKeySource "none" (recorded from 2.1.285).
    assert "nothing is charged" in bt._login_note("none")
    assert "Every token is billed" in bt._login_note("ANTHROPIC_API_KEY")


def test_default_folder_is_beside_the_clone_not_in_it():
    repo = bt._repo_root()
    f = bt.default_folder(repo / "examples" / "mcp_test" / "tiny_test.md")
    assert f == repo.parent / "waveflow_blind_tests" / "tiny_test"
    assert repo not in f.parents


def test_the_repo_spec_links_to_its_companion():
    spec = bt._repo_root() / "examples" / "mcp_test" / "tiny_test.md"
    assert [p.name for p in bt._companions(spec)] == ["notes.md"]


def _jsonl(path: Path, *recs: dict) -> None:
    path.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")


def test_a_run_cut_off_is_resumable_from_its_init_record(tmp_path):
    # Killed mid-run: no result record, but init (written first) names the
    # session.  An old-style run has no phases.json at all -- like the first
    # rotate run, which predates it.
    logdir = tmp_path / "trial.blindtest"
    logdir.mkdir()
    _jsonl(logdir / "transcript-1.jsonl",
           {"type": "rate_limit_event"},
           {"type": "system", "subtype": "init", "session_id": "abc"},
           {"type": "assistant", "message": {"content": []}})
    (p,) = bt._load_phases(logdir)
    assert (p["session_id"], p["status"], p["message"]) == ("abc", "interrupted", "(not recorded)")


def test_phases_json_supplies_messages_and_a_later_result_wins(tmp_path):
    logdir = tmp_path / "trial.blindtest"
    logdir.mkdir()
    _jsonl(logdir / "transcript-1.jsonl", {"type": "system", "subtype": "init", "session_id": "s"})
    _jsonl(logdir / "transcript-2.jsonl",
           {"type": "system", "subtype": "init", "session_id": "s"},
           {"type": "result", "subtype": "success", "session_id": "s"})
    bt._write_json(logdir / "phases.json", [
        {"phase": 1, "message": "build it", "status": "timed out"},
        {"phase": 2, "message": "carry on", "status": "running"},
    ])
    p1, p2 = bt._load_phases(logdir)
    assert (p1["message"], p1["status"], p1["timed_out"]) == ("build it", "timed out", True)
    assert (p2["message"], p2["result"]["subtype"]) == ("carry on", "success")


def test_a_run_started_before_its_session_has_no_session_id(tmp_path):
    # Measured: start-up can outlast a short limit, leaving only a
    # rate_limit_event.  Resume then starts the phase over instead.
    logdir = tmp_path / "trial.blindtest"
    logdir.mkdir()
    _jsonl(logdir / "transcript-1.jsonl", {"type": "rate_limit_event"})
    assert bt._load_phases(logdir)[0]["session_id"] is None


def test_a_second_new_run_points_at_resume(tmp_path):
    spec = tmp_path / "src" / "spec.md"
    spec.parent.mkdir()
    spec.write_text("x", encoding="utf-8")
    logdir = tmp_path / "trial.blindtest"
    logdir.mkdir()
    (logdir / "transcript-1.jsonl").write_text("", encoding="utf-8")
    out = bt.run_blind_test(spec, tmp_path / "trial", silent=True)
    assert "--resume" in out["error"]


def test_a_background_job_killed_after_the_turn_is_detected(tmp_path):
    # The rotate run's phase 2: ScheduleWakeup, no Monitor, turn ends, and
    # Claude Code kills the running csim after the result record.
    t = tmp_path / "t.jsonl"
    _jsonl(t,
           {"type": "system", "subtype": "task_updated", "task_id": "early",
            "patch": {"status": "killed"}},
           {"type": "result", "subtype": "success", "session_id": "s"},
           {"type": "system", "subtype": "task_updated", "task_id": "csim",
            "patch": {"status": "killed"}})
    assert bt._killed_at_end(t) == ["csim"]      # only what died after the turn ended
    _jsonl(t, {"type": "result", "subtype": "success", "session_id": "s"},
           {"type": "system", "subtype": "task_updated", "task_id": "x",
            "patch": {"status": "completed"}})
    assert bt._killed_at_end(t) == []


def test_live_log_shows_paths_relative_to_the_folder(tmp_path, capsys):
    rec = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": str(tmp_path / "a" / "b.py")}},
        {"type": "tool_use", "name": "mcp__waveflow__waveflow_search", "input": {"query": "TLAST"}},
    ]}}
    bt._live(json.dumps(rec), tmp_path)
    out = capsys.readouterr().out
    assert "-> Read  ./a/b.py" in out
    assert "-> waveflow_search  TLAST" in out


def test_the_no_waveflow_arm_differs_only_in_waveflow(tmp_path, monkeypatch):
    """Stage 0's baseline: no server, no Waveflow tools, the operator's Python."""
    spec = tmp_path / "src" / "rotate_func.md"
    spec.parent.mkdir()
    spec.write_text("Write and test a Vitis kernel.", encoding="utf-8")
    seen = {}

    def fake_drive(folder, logdir, config, **kw):
        seen.update(config=config, first=kw["first"], mcp=json.loads((logdir / "mcp.json").read_text()))
        return {"ok": True}

    monkeypatch.setattr(bt, "_drive", fake_drive)
    monkeypatch.setattr(bt, "_waveflow_importable", lambda nw: False if nw else None)
    bt.run_blind_test(spec, tmp_path / "base", silent=True, no_waveflow=True)
    assert seen["mcp"] == {"mcpServers": {}}
    assert seen["config"]["no_waveflow"] is True
    assert not [a for a in seen["config"]["allowed"] if "waveflow" in a.lower()]
    assert "Monitor" in seen["config"]["allowed"] and "Bash(python:*)" in seen["config"]["allowed"]
    assert "Vitis HLS directly" in seen["first"] and "rotate_func.md" in seen["first"]
    assert "Waveflow" not in seen["first"]

    bt.run_blind_test(spec, tmp_path / "wf", silent=True)
    assert "waveflow" in seen["mcp"]["mcpServers"] and "mcp__waveflow" in seen["config"]["allowed"]
    # The same spec file; the arm is in the first message only.
    assert "with Waveflow" in seen["first"] and "stream_inband" not in seen["first"]


def test_the_no_waveflow_arm_keeps_the_operators_path(monkeypatch):
    monkeypatch.setattr(bt, "_vitis_bin", lambda: None)   # Vitis's own PATH entry: next test
    monkeypatch.setattr(bt, "_vivado_bin", lambda: None)
    monkeypatch.setenv("PATH", "OPERATOR")
    assert bt._agent_env(True)["PATH"] == "OPERATOR"
    assert bt._agent_env(False)["PATH"].endswith("OPERATOR")
    assert bt._agent_env(False)["PATH"] != "OPERATOR"


def test_vitis_is_on_path_and_allowed_however_it_is_spelled(tmp_path, monkeypatch):
    """The first no-Waveflow rotate run was denied every call of vitis-run by full path."""
    vb = tmp_path / "Xilinx" / "2025.1" / "Vitis" / "bin"
    monkeypatch.setattr(bt, "_vitis_bin", lambda: vb)
    rules = bt.vitis_allowed()
    for spelled in (str(vb / "vitis-run.bat"), (vb / "vitis-run.bat").as_posix(), "vitis-run"):
        assert f"Bash({spelled}:*)" in rules and f"PowerShell({spelled}:*)" in rules
    for arm in (True, False):
        assert bt._agent_env(arm)["PATH"].split(os.pathsep).count(str(vb)) == 1
    assert str(vb) in bt.harness_note() and "vitis-run" in bt.harness_note()

    # Vivado likewise: a system-level spec needs its simulator.
    vv = tmp_path / "Xilinx" / "2025.1" / "Vivado" / "bin"
    monkeypatch.setattr(bt, "_vivado_bin", lambda: vv)
    rules = bt.vitis_allowed()
    for spelled in (str(vv / "xsim.bat"), (vv / "xelab.bat").as_posix(), "xvlog"):
        assert f"Bash({spelled}:*)" in rules
    for arm in (True, False):
        assert bt._agent_env(arm)["PATH"].split(os.pathsep).count(str(vv)) == 1
    assert str(vv) in bt.harness_note() and "xsim" in bt.harness_note()

    spec = tmp_path / "s.md"
    spec.write_text("x", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(bt, "_drive", lambda f, l, config, **kw: seen.update(config) or {})
    monkeypatch.setattr(bt, "_waveflow_importable", lambda nw: None)
    for arm in (True, False):
        bt.run_blind_test(spec, tmp_path / f"run{arm}", silent=True, no_waveflow=arm)
        assert f"Bash({(vb / 'vitis-run.bat').as_posix()}:*)" in seen["allowed"]


def test_a_phase_with_several_results_is_summed(tmp_path):
    """Monitor wake-ups after a turn ends each write a result; the phase is all of them."""
    t = tmp_path / "transcript-1.jsonl"
    _jsonl(t,
           {"type": "result", "subtype": "success", "num_turns": 65, "duration_ms": 330000,
            "session_id": "s", "usage": {"input_tokens": 96, "cache_read_input_tokens": 2502945,
                                         "cache_creation_input_tokens": 72784, "output_tokens": 33934}},
           {"type": "result", "subtype": "success", "num_turns": 1, "duration_ms": 2000,
            "session_id": "s", "usage": {"input_tokens": 4, "cache_read_input_tokens": 90790,
                                         "cache_creation_input_tokens": 479, "output_tokens": 31}})
    r = bt._result_record(t)
    assert r["num_turns"] == 66 and r["duration_ms"] == 332000 and r["result_records"] == 2
    assert bt._tokens(r) == (100 + 2502945 + 90790 + 72784 + 479, 2502945 + 90790, 33965)


@pytest.mark.parametrize("command,runs", [
    ("vitis-run --mode hls --tcl run.tcl", True),
    ("/c/Xilinx/2025.1/Vivado/bin/xelab.bat -debug all top", True),
    ("cd xsi && xvlog top.v", True),
    ("vivado -mode batch -source bd.tcl", True),
    (r"C:\Xilinx\2025.1\Vivado\bin\xsim.bat top -R", True),
    (r'grep -n "xelab\|xvlog" xsi/run.bat', False),
    ("python build.py --through csynth", False),
    ('cmd //c "run.bat x y"', False),
    ("rm -rf xsim.dir/x", False),
])
def test_direct_toolchain_calls_are_told_apart_from_mentions(command, runs):
    """The Choice section flags an agent that left the Waveflow flow for the vendor's."""
    assert bt.runs_toolchain(command) is runs


def test_time_split_charges_background_builds_and_splits_them_by_step(tmp_path):
    """Where the wall clock went: the agent's own time, and a background build split by step."""
    folder = tmp_path / "trial"
    (folder / ".waveflow").mkdir(parents=True)
    T0 = 1_800_000_000.0

    def at(s):
        import datetime as dt
        return dt.datetime.fromtimestamp(T0 + s, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")

    recs = [
        {"type": "user", "timestamp": at(0), "message": {"content": "go"}},
        # 0-60 s: the agent thinks.  60 s: it starts the build in the background.
        {"type": "assistant", "timestamp": at(60), "message": {"content": [
            {"type": "tool_use", "id": "b1", "name": "Bash",
             "input": {"command": "python x_build.py --through compare", "run_in_background": True}}]}},
        {"type": "user", "timestamp": at(61), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "b1", "content": "running in background"}]}},
        {"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": "b1"},
        {"type": "system", "subtype": "task_updated", "task_id": "t1",
         "patch": {"status": "completed", "end_time": (T0 + 660) * 1000}},
        {"type": "assistant", "timestamp": at(720), "message": {"content": [{"type": "text", "text": "done"}]}},
    ]
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    # The build's own step log: 400 s of csynth, then 200 s of XSI.
    steps = [{"step": "csynth", "start": T0 + 60, "end": T0 + 460, "success": True},
             {"step": "system_xsi", "start": T0 + 460, "end": T0 + 660, "success": True}]
    (folder / ".waveflow" / "build_steps.jsonl").write_text(
        "\n".join(json.dumps(s) for s in steps), encoding="utf-8")

    text = "\n".join(bt._time_split([{"transcript": str(t)}], folder))
    assert "| synth | 6.7 |" in text            # 400 s
    assert "| rtl sim | 3.3 |" in text          # 200 s
    assert "| agent (no tool running) | 2.0 |" in text   # 0-60 and 660-720
    assert "build (unsplit)" not in text        # the step log split the whole build
    assert "| **wall** | **12.0** |" in text
