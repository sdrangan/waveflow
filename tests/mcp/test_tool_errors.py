"""What a failing tool shows the model, through ``MCPServer.call_tool()``.

mcp 2 shows the model the message of a ``ToolError`` and nothing else: any
other exception arrives as just "Error executing tool <name>".  The registry
re-raises the errors Waveflow's tools raise on purpose (``ANTICIPATED_ERRORS``)
as ``ToolError``, so their messages -- written for the model -- get through,
while a genuine crash stays generic.  These tests hold both halves.
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

import waveflow.mcp.knowledge.index as index_mod
from waveflow.mcp.registry import ToolRegistry
from waveflow.mcp.scaffold import ScaffoldError
from waveflow.mcp.server import build_mcp

_EMPTY = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def _call(mcp: MCPServer, name: str, args: dict):
    return asyncio.run(mcp.call_tool(name, args))


def _server_raising(exc: Exception) -> MCPServer:
    def boom() -> dict:
        raise exc

    reg = ToolRegistry()
    reg.add(name="boom", description="raises", parameters=_EMPTY, fn=boom)
    mcp = MCPServer("test")
    reg.register_all(mcp)
    return mcp


@pytest.mark.parametrize(
    "exc",
    [ValueError("Path must not be empty."), ScaffoldError("directory exists")],
    ids=["ValueError", "ScaffoldError"],
)
def test_anticipated_error_reaches_the_model_with_its_message(exc) -> None:
    with pytest.raises(ToolError, match=str(exc)) as info:
        _call(_server_raising(exc), "boom", {})
    assert not isinstance(info.value, UnexpectedToolError)


def test_a_crash_stays_generic() -> None:
    """Not anticipated, so mcp 2 withholds the text from the model on purpose."""
    with pytest.raises(UnexpectedToolError) as info:
        _call(_server_raising(KeyError("internal detail")), "boom", {})
    assert "internal detail" not in str(info.value)
    assert "Error executing tool boom" in str(info.value)


def test_no_checkout_reaches_the_model_through_a_real_tool(monkeypatch, tmp_path) -> None:
    """``RootNotFound`` from the index build, through the registered search tool."""
    monkeypatch.setenv("WAVEFLOW_KB_ROOT", str(tmp_path))
    monkeypatch.setattr(index_mod, "_INDEX", {})
    mcp = build_mcp(mode="workspace")
    with pytest.raises(ToolError, match="has no docs/guide and examples/") as info:
        _call(
            mcp,
            "waveflow_search",
            {"query": "TLAST", "scope": None, "k": None, "include_generated": None},
        )
    assert not isinstance(info.value, UnexpectedToolError)


def test_errors_returned_as_data_are_still_data() -> None:
    """An unknown frame is answered, not raised: the reply lists the known ones."""
    result = _call(build_mcp(mode="workspace"), "waveflow_get_process", {"frame": "nope"})
    assert not result.is_error
    assert result.structured_content["error"] == "no frame 'nope'"
    assert "stream_inband" in result.structured_content["known"]


def test_wrapping_keeps_the_tool_schema() -> None:
    """``MCPServer`` builds the schema from the signature the wrapper must keep."""
    tools = {t.name: t for t in asyncio.run(build_mcp(mode="workspace").list_tools())}
    props = tools["waveflow_search"].input_schema["properties"]
    assert set(props) == {"query", "scope", "k", "include_generated"}
    assert tools["waveflow_search"].input_schema["required"] == ["query"]


def test_concurrent_first_calls_build_the_index_once(monkeypatch) -> None:
    """mcp 2 runs sync tools on worker threads, concurrently.

    Two first calls arriving together must share one build, not race two.
    """
    import threading
    import time

    builds: list[int] = []
    sentinel = object()

    def slow_build(root, refresh):
        builds.append(1)
        time.sleep(0.2)
        return sentinel

    monkeypatch.setattr(index_mod, "_INDEX", {})
    monkeypatch.setattr(index_mod, "_build", slow_build)
    got: list[object] = []
    threads = [
        threading.Thread(target=lambda: got.append(index_mod.get_index()))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(builds) == 1
    assert got == [sentinel] * 4
