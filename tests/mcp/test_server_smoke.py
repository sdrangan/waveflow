"""The server smoke test: start it over stdio, list the tools, call each one.

Everything else in ``tests/mcp/`` calls the tool functions directly.  That
misses the whole layer this exercises: a tool whose JSON-Schema the registry
declares wrongly, or whose return value FastMCP cannot serialize, passes every
direct test and then fails the first time a client touches it.  The only way
to catch that is to be a client.

So this launches ``waveflow.mcp.server`` as a subprocess, speaks MCP to it over
stdio, and calls every tool once with arguments a model would plausibly send --
including the nulls a ``strict`` schema forces through for optional arguments.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: Every tool, with one call a model would actually make.  Optional arguments
#: are passed as ``None`` on purpose: the registry marks these schemas
#: ``strict``, which makes every property required, so a client that wants a
#: default sends null.  A tool that cannot take null is broken in practice.
CALLS: list[tuple[str, dict]] = [
    ("waveflow_list_frames", {}),
    ("waveflow_get_process", {"frame": None}),
    ("waveflow_get_process", {"frame": "stream_inband"}),
    ("waveflow_browse", {"section": None}),
    ("waveflow_browse", {"section": "guide/custom_hooks"}),
    (
        "waveflow_search",
        {"query": "TLAST", "scope": None, "k": None, "include_generated": None},
    ),
    (
        "waveflow_search",
        {"query": "DataList", "scope": "docs", "k": 3, "include_generated": False},
    ),
    ("waveflow_find_usage", {"symbol": "HostActivated", "include_generated": None}),
    ("waveflow_list_examples", {}),
    ("waveflow_get_example", {"name": "stream_inband", "file": None}),
    ("waveflow_get_example", {"name": "stream_inband", "file": "poly.py"}),
    (
        "waveflow_get_doc",
        {"path": "docs/guide/custom_hooks/writing.md", "heading": None},
    ),
    ("waveflow_get_components", {}),
]

EXPECTED_TOOLS = {
    "waveflow_browse",
    "waveflow_search",
    "waveflow_find_usage",
    "waveflow_list_examples",
    "waveflow_get_example",
    "waveflow_get_doc",
    "waveflow_list_frames",
    "waveflow_get_process",
    "waveflow_get_components",
    "waveflow_validate_schema",
}

#: Removed by D1 and D2.  Asserted absent so a revert is caught here too.
REMOVED_TOOLS = {"waveflow_rag_search_examples", "waveflow_get_schema_draft_plan"}


#: (tool, seconds) for every call in the session, filled in by `_drive`.
SECONDS: list[tuple[str, float]] = []


async def _drive() -> tuple[set[str], str, list[tuple[str, str]]]:
    """Run one stdio session; return the tool names, instructions, and results."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "waveflow.mcp.server"],
        cwd=str(REPO),
    )
    results: list[tuple[str, str]] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            instructions = init.instructions or ""
            listed = await session.list_tools()
            names = {t.name for t in listed.tools}

            for tool, args in CALLS:
                t0 = time.monotonic()
                response = await session.call_tool(tool, args)
                SECONDS.append((tool, time.monotonic() - t0))
                text = "".join(
                    getattr(c, "text", "") for c in response.content
                )
                results.append((tool, text if not response.isError else f"ERROR: {text}"))
    return names, instructions, results


@pytest.fixture(scope="module")
def session():
    pytest.importorskip("mcp", reason="the MCP client is needed to speak to the server")
    return asyncio.run(asyncio.wait_for(_drive(), timeout=180))


def test_server_lists_every_tool(session) -> None:
    names, _, _ = session
    missing = EXPECTED_TOOLS - names
    assert not missing, f"the server did not register: {sorted(missing)}"
    assert not (REMOVED_TOOLS & names), "a removed tool came back"


def test_server_sends_instructions(session) -> None:
    """Most clients inject these before the model has called anything."""
    _, instructions, _ = session
    assert "waveflow_get_process" in instructions, (
        "the instructions must say which call starts an accelerator"
    )
    assert "generated" in instructions


def test_every_tool_returns_something_usable(session) -> None:
    _, _, results = session
    failures = [(tool, text[:300]) for tool, text in results if text.startswith("ERROR:")]
    assert not failures, "tools that errored over stdio:\n" + "\n".join(
        f"  {t}: {m}" for t, m in failures
    )
    empty = [tool for tool, text in results if not text.strip()]
    assert not empty, f"tools that returned nothing: {empty}"


def test_search_over_stdio_actually_found_the_page(session) -> None:
    """Not just 'no exception' -- the payload has to survive the round trip."""
    _, _, results = session
    hits = next(text for tool, text in results if tool == "waveflow_search")
    assert "docs/guide/custom_hooks/stream.md" in hits


def test_no_tool_call_stalls_inside_the_server(session) -> None:
    """The first index-backed call builds the index -- about 2 s.

    Inside the server, not from the CLI, `git ls-files` inherited the MCP
    JSON-RPC pipe as its stdin and blocked until its 30 s timeout.  The CLI
    never showed it, and a blind-test agent sat waiting.  Only a real stdio
    session can catch that, so it is timed here.
    """
    slow = [(tool, round(s, 1)) for tool, s in SECONDS if s > 15]
    assert not slow, f"tool calls that stalled over stdio: {slow}"


def test_get_example_over_stdio_returns_the_whole_file(session) -> None:
    _, _, results = session
    bodies = [text for tool, text in results if tool == "waveflow_get_example"]
    whole = max(bodies, key=len)
    assert "class PolyAccel" in whole and "def body" in whole
