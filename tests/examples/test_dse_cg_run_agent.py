"""examples/dse_cg/run_agent.py, the agent harness (plan step 7), without an agent: the container
wrapper's environment allowlist, the shell-command flags, the settings rule (a scored run needs the
pre-registration frozen), and one whole run with a stand-in agent -- a fake ``query`` that drives
``dselib`` over HTTP to the service ``run_one`` started, on a synthetic G1 table -- recorded and scored.
No API call, no container (the fake replaces the Agent SDK's ``query``)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("claude_agent_sdk")

from examples.dse_cg import run_agent as ra


def test_the_wrapper_forwards_only_the_allowlist(tmp_path):
    w = ra.write_wrapper(tmp_path / "claude.sh", "img:1", tmp_path / "cli", [tmp_path / "sb"], "dse-x")
    text = w.read_text()
    assert "for v in " + " ".join(ra.FORWARD_ENV) + ";" in text
    assert f"-v {tmp_path / 'sb'}:{tmp_path / 'sb'}" in text and f"-v {tmp_path / 'cli'}:/opt/claude/claude:ro" in text
    assert "SSE_PORT" not in text and "--network host" in text and '-w "$PWD"' in text


def test_shell_commands_naming_the_outside_are_flagged():
    def bash(cmd):
        return {"type": "AssistantMessage", "content": [{"id": "t", "name": "Bash", "input": {"command": cmd}}]}

    t = [bash("python run.py"), bash("curl -s http://127.0.0.1:8765/call"), bash("curl https://example.com/x"),
         bash("pip install torch")]
    assert ra.network_flags(t) == ["curl https://example.com/x", "pip install torch"]


def test_a_scored_run_needs_the_pre_registration_frozen(monkeypatch):
    from examples.dse_cg import prereg

    monkeypatch.setattr(prereg, "DATE", None)
    with pytest.raises(SystemExit, match="not frozen"):
        ra.settings(False, {})
    s = ra.settings(True, {"model": "m", "budget_s": 100.0})
    assert s["model"] == "m" and s["budget_s"] == 100.0 and s["image"] == prereg.AGENT_IMAGE


def test_a_run_is_recorded_and_scored(tmp_path, monkeypatch):
    import claude_agent_sdk as sdk

    from examples.dse_cg import score
    from examples.dse_cg.prereg import OBJECTIVES
    from tests.examples.test_dse_cg_service import (
        OPT0,
        OPT0_LUT,
        Design,
        synthetic_table,
    )

    d = Design()
    table = tmp_path / "table"
    table.mkdir()
    pts = synthetic_table(table, d)
    obj = OBJECTIVES[0]
    seen = {}

    async def fake_query(*, prompt, options):
        """The stand-in agent: what a run's agent does through the skill, in four calls."""
        from examples.dse_cg.skill.dse import dselib

        seen.update(prompt=prompt, cwd=options.cwd, env=dict(options.env), skills=options.skills,
                    sources=options.setting_sources, disallowed=options.disallowed_tools)
        yield sdk.SystemMessage("init", {"skills": ["dse"], "tools": ["Bash", "Read"], "model": "fake"})
        monkeypatch.setattr(dselib, "_SERVICE", options.env["DSE_SERVICE"])
        p = {**OPT0, "form": "factored", "cg_window": 2}
        row = dselib.wait(dselib.synth([p]))[0]
        t = dselib.wait(dselib.rtlsim([p], {"name": "long_term", "t": 3}))[0]
        loss = dselib.golden(p, {"name": "accuracy", "t": 3, "target": "-6_3"})
        out = dselib.submit(p, {"params": p, "rationale": "fake", "evidence": [
            {"metric": "lut", "value": row["lut"], "source": "synth"},
            {"metric": "t_upd_ms", "value": t["t_upd_ms"], "workload": "long_term", "source": "rtlsim"},
            {"metric": "median_loss_db", "value": loss["median_loss_db"],
             "workload": {"name": "accuracy", "t": 3, "target": "-6_3"}, "source": "golden"}]})
        assert out["accepted"], out
        yield sdk.AssistantMessage([sdk.ToolUseBlock("t1", "Bash", {"command": "python explore.py"})],
                                   "fake", usage={"input_tokens": 10, "output_tokens": 5}, message_id="m1")
        yield sdk.ResultMessage("success", 1000, 900, False, 3, "s1", total_cost_usd=0.01,
                                usage={"input_tokens": 10, "output_tokens": 5})

    monkeypatch.setattr(sdk, "query", fake_query)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-key")
    rec = ra.run_one("traditional", obj["id"], 1, tmp_path / "runs", pilot=True,
                     overrides={"model": "fake", "budget_s": 1e6}, table=table, design=d)
    r = json.loads((rec / "record.json").read_text())
    assert r["void"] is None and r["submitted"] and r["calls"] == 4 and r["stopped"] == "ended"
    assert r["usage"]["output_tokens"] == 5 and r["result"]["subtype"] == "success"
    assert seen["prompt"].startswith("/dse DSE.md ") and seen["cwd"] == str(rec / "sandbox")
    assert seen["sources"] == ["project"] and seen["skills"] == ["dse"]
    assert "WebFetch" in seen["disallowed"] and seen["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert (rec / "sandbox" / ".git").is_dir() and (rec / "sandbox" / ".claude" / "skills" / "dse").is_dir()
    assert not (rec / "sandbox" / "ledger.jsonl").exists()          # the ledger stays outside the sandbox
    m = score.run_measures(rec, score.Truth(table, d), obj, score.optimum(score.Truth(table, d), obj, pts)["value"],
                           epsilon=0.05)
    assert m["best"] == OPT0_LUT and m["quality"] == 0.0 and m["report_errors"] == []
    assert m["time_to_target"]["spent_s"] > 0


def test_a_run_whose_skill_did_not_load_is_void(tmp_path, monkeypatch):
    import claude_agent_sdk as sdk

    from tests.examples.test_dse_cg_service import Design, synthetic_table

    d = Design()
    table = tmp_path / "table"
    table.mkdir()
    synthetic_table(table, d)

    async def fake_query(*, prompt, options):
        yield sdk.SystemMessage("init", {"skills": [], "slash_commands": ["clear"]})
        yield sdk.ResultMessage("success", 10, 9, False, 1, "s1")

    monkeypatch.setattr(sdk, "query", fake_query)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-key")
    rec = ra.run_one("waveflow", "lut_tupd5_loss01", 2, tmp_path / "runs", pilot=True,
                     overrides={"model": "fake", "budget_s": 1e6}, table=table, design=d)
    assert json.loads((rec / "record.json").read_text())["void"] == "the dse skill did not load"
