"""The ``waveflow kb`` CLI.

CLI first, MCP as a thin wrapper -- so the CLI is not a convenience to be
tested loosely.  It is the path an agent without MCP support takes, and it has
to return the same data the MCP tool does.  That equivalence is what
:func:`test_cli_json_matches_the_tool` pins; the rest check that the subcommands
parse and that a miss exits non-zero instead of printing a traceback.
"""
from __future__ import annotations

import json

import pytest

from waveflow.cli import main
from waveflow.mcp.knowledge import waveflow_list_examples, waveflow_search


def _run(capsys, argv: list[str]) -> tuple[int, str]:
    code = main(argv)
    return code, capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["kb", "browse"],
        ["kb", "browse", "guide/custom_hooks"],
        ["kb", "search", "TLAST", "-k", "3"],
        ["kb", "search", "TLAST", "--scope", "docs"],
        ["kb", "usage", "HostActivated"],
        ["kb", "examples"],
        ["kb", "example", "stream_inband"],
        ["kb", "example", "stream_inband", "--file", "poly.py"],
        ["kb", "doc", "docs/guide/custom_hooks/writing.md"],
    ],
    ids=lambda a: " ".join(a),
)
def test_every_subcommand_succeeds(capsys, argv: list[str]) -> None:
    code, out = _run(capsys, argv)
    assert code == 0
    assert out.strip()
    json.loads(out)  # the default output is JSON


@pytest.mark.parametrize(
    "argv",
    [
        ["kb", "--text", "browse", "examples"],
        ["kb", "--text", "search", "register map"],
        ["kb", "--text", "usage", "DataList"],
        ["kb", "--text", "examples"],
        ["kb", "--text", "example", "stream_inband"],
    ],
    ids=lambda a: " ".join(a),
)
def test_text_output_renders(capsys, argv: list[str]) -> None:
    code, out = _run(capsys, argv)
    assert code == 0 and out.strip()


def test_cli_json_matches_the_tool(capsys) -> None:
    _, out = _run(capsys, ["kb", "search", "persistent loop END command", "-k", "4"])
    assert json.loads(out) == waveflow_search("persistent loop END command", k=4)

    _, out = _run(capsys, ["kb", "examples"])
    assert json.loads(out) == waveflow_list_examples()


def test_a_miss_exits_nonzero_with_an_error_object(capsys) -> None:
    code, out = _run(capsys, ["kb", "example", "conv2d"])
    assert code == 1
    payload = json.loads(out)
    assert "error" in payload and "known" in payload


def test_text_mode_prints_the_error_not_a_traceback(capsys) -> None:
    code, out = _run(capsys, ["kb", "--text", "doc", "docs/guide/nope.md"])
    assert code == 1
    assert out.startswith("error:")
