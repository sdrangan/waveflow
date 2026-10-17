"""HTTP boundary tests; fixture SSE is protocol test data, never benchmark evidence."""

import importlib
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@contextmanager
def http_fixture():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                b': keepalive\n\nevent: run.completed\ndata: {"runtime":{"model":"deepseek-flash","provider":"deepseek"},"usage":{"input_tokens":1}}\n\n'
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_http_transport_and_sse_capture(tmp_path):
    api = importlib.import_module("examples.dse_fir.run_agent")
    with http_fixture() as (url, seen):
        client = api.HermesHTTP(url, "test-only-token")
        assert client.json("GET", "/health")["status"] == "ok"
        events = client.stream("/chat", api.lock_payload(), tmp_path / "raw.sse")
    assert seen[0] == ("/health", "Bearer test-only-token")
    assert seen[1][1]["require_model_lock"] is True
    assert seen[1][1]["model_options"]["reasoning"]["enabled"] is False
    assert events[0]["event"] == "run.completed"
    assert "keepalive" in (tmp_path / "raw.sse").read_text()
    assert api.verify_runtime(events[0]["data"]["runtime"])["model"] == "deepseek-flash"
    with pytest.raises(ValueError):
        api.verify_runtime({"model": "deepseek-chat", "provider": "deepseek"})
    with pytest.raises(ValueError):
        api.HermesHTTP("http://127.0.0.1:8642", "test-only-token")


def test_exact_six_tool_boundary_rejects_extra_and_missing():
    api = importlib.import_module("examples.dse_fir.run_agent")
    names = ["mcp__waveflow__" + n for n in api.TOOL_NAMES]
    assert api.verify_tools(names) == sorted(names)
    with pytest.raises(ValueError, match="six"):
        api.verify_tools(names + ["terminal"])
    with pytest.raises(ValueError, match="six"):
        api.verify_tools(names[:-1])


def test_isolated_config_and_environment(tmp_path):
    from pathlib import Path

    api = importlib.import_module("examples.dse_fir.run_agent")
    config = api.isolated_config(
        tmp_path,
        tmp_path / "experiment",
        Path("/repo"),
        Path("/repo/.venv/bin/python"),
        12,
    )
    assert config["platform_toolsets"]["api_server"] == ["waveflow"]
    assert config["agent"]["max_turns"] == 12
    assert config["memory"]["memory_enabled"] is False
    assert set(config["mcp_servers"]) == {"waveflow"}
    env = api.isolated_environment(tmp_path, 8647, "test-provider-key", "test-http-key")
    assert env["HERMES_HOME"] == str(tmp_path)
    assert env["API_SERVER_HOST"] == "127.0.0.1"
    assert env["DEEPSEEK_API_KEY"] == "test-provider-key"
    assert "OPENAI_API_KEY" not in env
    with pytest.raises(ValueError):
        api.isolated_config(
            tmp_path, tmp_path / "x", Path("/repo"), Path("/python"), 17
        )


def test_launch_requires_fresh_isolated_home_and_bounded_port(tmp_path):
    api = importlib.import_module("examples.dse_fir.run_agent")
    with pytest.raises(ValueError):
        api.run_isolated(
            home=tmp_path, root=tmp_path / "root", output=tmp_path / "out", port=8642
        )
    with pytest.raises(FileExistsError):
        api.run_isolated(
            home=tmp_path, root=tmp_path / "root", output=tmp_path / "out", port=8647
        )
