"""Isolated, fail-closed Hermes HTTP benchmark (no personal gateway mutations)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def lock_payload():
    return {
        "model": "deepseek-flash",
        "provider": "deepseek",
        "require_model_lock": True,
        "model_options": {"reasoning": {"enabled": False}},
    }


def verify_runtime(runtime):
    if (
        runtime.get("model") != "deepseek-flash"
        or runtime.get("provider") != "deepseek"
    ):
        raise ValueError(f"Runtime boundary violation: {runtime}")
    return runtime


class HermesHTTP:
    def __init__(self, url, token, timeout=600):
        parsed = urlparse(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port in (None, 8642)
        ):
            raise ValueError(
                "Only a dedicated loopback HTTP port other than 8642 is permitted"
            )
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def request(self, method, path, body=None):
        payload = None if body is None else json.dumps(body).encode()
        return Request(
            self.url + path,
            data=payload,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )

    def json(self, method, path, body=None):
        with urlopen(
            self.request(method, path, body), timeout=self.timeout
        ) as response:
            return json.load(response)

    def stream(self, path, body, raw_path):
        events, event_name, data = [], "", []
        started = time.monotonic()
        with (
            urlopen(self.request("POST", path, body), timeout=self.timeout) as response,
            Path(raw_path).open("wb") as raw,
        ):
            for line in response:
                raw.write(line)
                raw.flush()
                if time.monotonic() - started > self.timeout:
                    raise TimeoutError("Overall stream deadline exceeded")
                text = line.decode("utf-8").rstrip("\r\n")
                if not text:
                    if data:
                        payload = json.loads("\n".join(data))
                        events.append({"event": event_name, "data": payload})

                    event_name, data = "", []
                elif text.startswith("event:"):
                    event_name = text[6:].strip()
                elif text.startswith("data:"):
                    data.append(text[5:].lstrip())
        return events


TOOL_NAMES = (
    "dse_get_dse_context",
    "dse_pysim",
    "dse_predict_resource",
    "dse_synth",
    "dse_rtlsim",
    "dse_get_results",
)


def verify_tools(names):
    expected = {"mcp__waveflow__" + n for n in TOOL_NAMES}
    if len(names) != 6 or set(names) != expected:
        raise ValueError(f"Expected exactly six Waveflow tools; found {sorted(names)}")
    return sorted(names)


def isolated_config(home, root, repo, waveflow_python, turns):
    if not 1 <= turns <= 16:
        raise ValueError("Agent turns must be between 1 and 16")
    return {
        "model": {
            "default": "deepseek-flash",
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
        },
        "agent": {"max_turns": turns},
        "reasoning_effort": "none",
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "compression": {"enabled": False},
        "checkpoints": {"enabled": False},
        "smart_model_routing": {"enabled": False},
        "toolsets": ["mcp_waveflow"],
        "tools": {"tool_search": {"enabled": "off"}},
        "platform_toolsets": {"api_server": ["waveflow"]},
        "terminal": {"cwd": str(home)},
        "gateway": {"multiplex_profiles": False},
        "mcp_servers": {
            "waveflow": {
                "command": str(waveflow_python),
                "tools": {
                    "include": list(TOOL_NAMES),
                    "resources": False,
                    "prompts": False,
                },
                "args": ["-m", "examples.dse_fir", "--root", str(root), "serve"],
                "cwd": str(repo),
            }
        },
    }


def isolated_environment(home, port, provider_key, http_key):
    import os

    # Deliberate whitelist: never inherit default-profile hooks or other credentials.
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if key in os.environ
    }
    env.update(
        HOME=str(home),
        HERMES_HOME=str(home),
        HERMES_DEFAULT_PROFILE="default",
        API_SERVER_KEY=http_key,
        API_SERVER_HOST="127.0.0.1",
        API_SERVER_PORT=str(port),
        DEEPSEEK_API_KEY=provider_key,
        TERMINAL_CWD=str(home),
        TMPDIR=str(home / "tmp"),
        PYTHONUNBUFFERED="1",
    )
    return env


PROMPT = """Find the best feasible FIR design using only the six provided tools. First call dse_get_dse_context.
The scored search space is ntap in [8,16,32], samp_w in [8,12,16,24], samp_i=2,
mem_dwidth=32, unroll_lane in [false,true]. Maximize stopband_rej_db subject to the
context constraints. Use the returned budgets; there are at most 16 model turns.
Make evidence-driven choices; use parallel tool calls or synth batches where useful.
Obtain pysim and synth evidence for the recommended candidate, not just predictions.
Treat replayed HLS estimates as replay, never new synthesis or board validation.
Return the final answer inline as JSON with recommended_candidate_id, params,
evaluation_ids, objective_value, feasible, and limitations. Do not claim global
optimality without evidence; an unsupported guess is not a successful recommendation."""


def run_isolated(
    *,
    home,
    root,
    output,
    port=8647,
    turns=16,
    paid=False,
    hermes_source=None,
    credential_home=None,
    waveflow_python=None,
):
    """One fresh trial. Paid inference requires opt-in. No personal gateway writes."""
    import os
    import secrets
    import signal
    import socket
    import subprocess
    import sys
    from urllib.error import URLError

    home, root, output = (Path(p).resolve() for p in (home, root, output))
    HermesHTTP(f"http://127.0.0.1:{port}", "validation-only")
    if home.exists():
        raise FileExistsError(
            "Isolated home must be new, not an existing profile or run"
        )
    repo = Path(__file__).resolve().parents[2]
    source = Path(hermes_source or Path.home() / ".hermes/hermes-agent").resolve()
    credentials = Path(credential_home or Path.home() / ".hermes").resolve()
    hpython = source / "venv/bin/python"
    wpython = Path(waveflow_python or sys.executable).absolute()
    config = isolated_config(home, root, repo, wpython, turns)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
    home.mkdir(parents=True, mode=0o700)
    output.mkdir(parents=True, exist_ok=False)
    for name in ("tmp", "skills", "memories", "sessions", "logs"):
        (home / name).mkdir()
    (home / "config.yaml").write_text(json.dumps(config, indent=2))
    (home / "SOUL.md").write_text(
        "You are a bounded FIR design-space exploration assistant.\n"
    )
    (home / "AGENTS.md").write_text("Only the six Waveflow tools are authorized.\n")

    def save(name, value):
        (output / name).write_text(json.dumps(value, indent=2, ensure_ascii=False))

    # Resolve one credential through Hermes' secret scope; never read/copy auth files
    # or log subprocess stdout. Only the isolated child's environment receives it.
    credential_code = (
        "import sys; from pathlib import Path; "
        "from agent.secret_scope import build_profile_secret_scope, set_secret_scope, get_secret_str; "
        "set_secret_scope(build_profile_secret_scope(Path(sys.argv[1]))); "
        'k=get_secret_str("DEEPSEEK_API_KEY"); '
        'assert k, "DeepSeek credential unavailable"; sys.stdout.write(k)'
    )
    provider_key = subprocess.check_output(
        [str(hpython), "-c", credential_code, str(credentials)], cwd=source, text=True
    )
    http_key = secrets.token_urlsafe(32)
    env = isolated_environment(home, port, provider_key, http_key)
    env["PYTHONPATH"] = str(source)
    client = HermesHTTP(f"http://127.0.0.1:{port}", http_key)
    probe_code = """import json
from hermes_cli.config import load_config
from tools.mcp_tool import register_mcp_servers, shutdown_mcp_servers
from model_tools import get_tool_definitions
from hermes_cli.tools_config import _get_platform_tools
config=load_config()
register_mcp_servers(config['mcp_servers'])
toolsets=sorted(_get_platform_tools(config, 'api_server'))
from gateway.platforms.api_server import APIServerAdapter
from gateway.config import PlatformConfig
adapter=APIServerAdapter(PlatformConfig())
agent=adapter._create_agent(requested_model='deepseek-flash', requested_provider='deepseek',
    model_options={'reasoning':{'enabled':False}}, confirmed_runtime_lock=True)
tools=agent.tools
print('BOUNDARY_JSON='+json.dumps({'toolsets':toolsets, 'tools':tools,
    'runtime':agent._hermes_api_runtime, 'max_iterations':agent.max_iterations,
    'system_prompt':agent._build_system_prompt()}))
shutdown_mcp_servers()
"""
    probe = subprocess.run(
        [str(hpython), "-c", probe_code],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    lines = [
        line for line in probe.stdout.splitlines() if line.startswith("BOUNDARY_JSON=")
    ]
    if len(lines) != 1:
        raise RuntimeError("Missing authoritative MCP schema preflight")
    boundary = json.loads(lines[0].split("=", 1)[1])
    names = verify_tools([tool["function"]["name"] for tool in boundary["tools"]])
    verify_runtime(boundary["runtime"])
    if boundary["max_iterations"] != turns:
        raise ValueError("Agent did not apply requested hard iteration bound")
    prompt = boundary.get("system_prompt", "")
    if any(marker in prompt for marker in ("<available_skills>", "Ashesh")):
        raise ValueError("Unexpected personal context or skills in isolated prompt")
    if set(boundary["toolsets"]) - {"mcp_waveflow", "waveflow"}:
        raise ValueError(
            f"Unexpected enabled platform toolsets: {boundary['toolsets']}"
        )
    save("tool_boundary.json", boundary)
    process = None
    summary = {"paid": paid, "port": port, "tool_names": names, "max_turns": turns}
    try:
        with (output / "gateway.log").open("w") as log:
            process = subprocess.Popen(
                [str(source / "venv/bin/hermes"), "gateway", "run", "--no-supervise"],
                env=env,
                cwd=home,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            summary["pid"] = process.pid
            deadline = time.monotonic() + 90
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Isolated gateway exited; see gateway.log")
                try:
                    health = client.json("GET", "/health")
                    break
                except (URLError, ConnectionError):
                    if time.monotonic() > deadline:
                        raise TimeoutError("Gateway failed to become healthy")
                    time.sleep(0.5)
            save("health.json", health)
            discovery = client.json("GET", "/v1/toolsets")
            save("http_toolsets.json", discovery)
            enabled = [row for row in discovery["data"] if row["enabled"]]
            # Hermes 0.21.3's catalog enumerates built-in/plugin toolsets only,
            # not dynamic MCP servers. Assert no built-ins; MCP schemas are
            # checked through the canonical API agent constructor below.
            if enabled:
                raise ValueError("Unexpected built-in HTTP tools enabled")
            if not paid:
                summary["status"] = "preflight_verified"
            else:
                payload = lock_payload()
                session = client.json(
                    "POST", "/api/sessions", {**payload, "system_prompt": PROMPT}
                )
                save("session_created.json", session)
                session_id = session.get("id") or session.get("session", {}).get("id")
                if not session_id:
                    raise RuntimeError("Missing created session id")
                lock = client.json("POST", f"/api/sessions/{session_id}/model", payload)
                verify_runtime(lock["runtime"])
                save("model_lock.json", lock)
                request = {**payload, "message": PROMPT.replace("16 model turns", f"{turns} model turns")}
                save("request.json", request)
                started = time.monotonic()
                events = client.stream(
                    f"/api/sessions/{session_id}/chat/stream",
                    request,
                    output / "raw.sse",
                )
                summary["wall_time_seconds"] = time.monotonic() - started
                save("events.json", events)
                terminal = [e["data"] for e in events if e["event"] in ("run.completed", "run.failed", "run.cancelled")]
                if len(terminal) != 1:
                    raise RuntimeError("Missing unique terminal SSE event; inspect raw.sse")
                runtime = verify_runtime(terminal[0]["runtime"])
                tool_events = [
                    e["data"] for e in events if e["event"] == "tool.started"
                ]
                if not tool_events or any(
                    e["tool_name"] not in names for e in tool_events
                ):
                    raise ValueError("Missing tools or unexpected tool execution")
                if tool_events[0]["tool_name"] != "mcp__waveflow__dse_get_dse_context":
                    raise ValueError("First tool was not context discovery")
                save("session.json", client.json("GET", f"/api/sessions/{session_id}"))
                save(
                    "messages.json",
                    client.json(
                        "GET", f"/api/sessions/{session_id}/messages?limit=1000"
                    ),
                )
                summary.update(
                    status="completed" if terminal[0].get("completed", False) else "failed",
                    turn_exit_reason=terminal[0].get("turn_exit_reason"),
                    session_id=session_id,
                    runtime=runtime,
                    usage=terminal[0].get("usage"),
                    tool_call_count=len(tool_events),
                    final_response=next((e["data"]["content"] for e in reversed(events)
                                         if e["event"] == "assistant.completed"), ""),
                )
    except Exception as exc:
        summary.update(status="failed", error=str(exc))
        raise
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            summary["gateway_exit_code"] = process.returncode
            summary["cleaned_up"] = process.poll() is not None
        save("summary.json", summary)
    return summary


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8647)
    parser.add_argument("--turns", type=int, default=16)
    parser.add_argument(
        "--paid", action="store_true", help="Authorize one real inference trial"
    )
    args = parser.parse_args()
    print(json.dumps(run_isolated(**vars(args)), indent=2))


if __name__ == "__main__":
    main()
