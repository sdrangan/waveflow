"""Tests for ``waveflow_mcp_setup``: rendering and writing ``.vscode/mcp.json``.

This file used to test ``--build-rag`` and nothing else.  That flag built an
OpenAI vector store and wrote its ID into the config; both went away with
decision D1, and the knowledge tools that replaced them need no key, no
service and no environment variable -- so there is nothing left for the
generated config to carry beyond the interpreter path.

What is still worth pinning is the part that can destroy a file: the command
refuses to overwrite an existing config without ``--force``, and ``--dry-run``
writes nothing at all.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from waveflow.scripts.waveflow_mcp_setup import (
    main,
    render_mcp_config,
    write_mcp_config,
)


@pytest.fixture
def no_interpreter_probe():
    """Skip the subprocess that imports ``waveflow.mcp.server``."""
    with patch("waveflow.scripts.waveflow_mcp_setup.validate_python_interpreter"):
        yield


# ---------------------------------------------------------------------------
# render / write
# ---------------------------------------------------------------------------


def test_render_points_the_server_at_the_given_interpreter():
    config = json.loads(render_mcp_config(python_path="/usr/bin/python3"))
    server = config["servers"]["waveflow"]
    assert server["command"] == "/usr/bin/python3"
    assert server["args"] == ["-m", "waveflow.mcp.server"]


def test_render_carries_no_api_environment():
    """Nothing the server needs comes from the environment any more (D1)."""
    config = json.loads(render_mcp_config(python_path="/usr/bin/python3"))
    assert "env" not in config["servers"]["waveflow"]


def test_write_creates_the_vscode_config(tmp_path):
    output_path = write_mcp_config(workspace=tmp_path, python_path="/usr/bin/python3")
    assert output_path == tmp_path / ".vscode" / "mcp.json"
    config = json.loads(output_path.read_text())
    assert config["servers"]["waveflow"]["command"] == "/usr/bin/python3"


def test_write_refuses_to_clobber_without_force(tmp_path):
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    (vscode / "mcp.json").write_text('{"servers": {"mine": {}}}')

    with pytest.raises(FileExistsError):
        write_mcp_config(workspace=tmp_path, python_path="/usr/bin/python3")

    assert "mine" in json.loads((vscode / "mcp.json").read_text())["servers"]


def test_write_with_force_replaces_it(tmp_path):
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    (vscode / "mcp.json").write_text('{"servers": {"mine": {}}}')

    write_mcp_config(workspace=tmp_path, python_path="/usr/bin/python3", force=True)
    config = json.loads((vscode / "mcp.json").read_text())
    assert "waveflow" in config["servers"]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_writes_the_config(tmp_path, no_interpreter_probe):
    argv = ["waveflow_mcp_setup", "--workspace", str(tmp_path)]
    with patch("sys.argv", argv):
        assert main() == 0
    assert (tmp_path / ".vscode" / "mcp.json").exists()


def test_main_dry_run_prints_and_writes_nothing(tmp_path, no_interpreter_probe, capsys):
    argv = ["waveflow_mcp_setup", "--workspace", str(tmp_path), "--dry-run"]
    with patch("sys.argv", argv):
        assert main() == 0

    assert not (tmp_path / ".vscode").exists()
    config = json.loads(capsys.readouterr().out)
    assert "waveflow" in config["servers"]
