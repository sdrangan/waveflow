"""
Shared MCP tool registry for waveflow.

``REGISTRY`` is the single source of truth for all tool definitions.  Both
the MCP server (``server.py``) and the blind-user harness
(``headless.py``) import from here so that tool metadata is never
duplicated.

Tools are tagged with one or more *profiles* (``"workspace"`` and/or
``"headless"``) that determine in which mode they are exposed.

Usage
-----
MCP server (workspace mode)::

    from waveflow.mcp.registry import REGISTRY
    REGISTRY.register_all(mcp, profile="workspace")

MCP server (headless mode)::

    from waveflow.mcp.registry import REGISTRY
    REGISTRY.register_all(mcp, profile="headless")

Blind-user harness (all tools)::

    from waveflow.mcp.registry import REGISTRY
    schemas = REGISTRY.tool_schemas()
    result  = REGISTRY.dispatch("waveflow_get_components", {})
"""
from __future__ import annotations

from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from waveflow.mcp.components import get_components
from waveflow.mcp.example_rag import search_schema_examples
from waveflow.mcp.knowledge import (
    waveflow_browse,
    waveflow_find_usage,
    waveflow_get_doc,
    waveflow_get_example,
    waveflow_list_examples,
    waveflow_search,
)
from waveflow.mcp.schema_tools import get_schema_draft_plan, validate_schema_from_file


# ---------------------------------------------------------------------------
# ToolDef dataclass
# ---------------------------------------------------------------------------


class _ToolDef:
    """Internal holder for a single tool definition."""

    __slots__ = ("name", "description", "parameters", "fn", "profiles")

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        fn: Callable[..., Any],
        profiles: frozenset[str],
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.fn = fn
        self.profiles = profiles


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

_ALL_PROFILES: frozenset[str] = frozenset({"workspace", "headless"})


class ToolRegistry:
    """Registry of waveflow MCP tools.

    Responsibilities
    ----------------
    * Store tool definitions (name, description, JSON-Schema parameters, callable).
    * Register tools with a :class:`~mcp.server.fastmcp.FastMCP` instance,
      optionally filtered by *profile* (``"workspace"`` or ``"headless"``).
    * Return OpenAI ``function``-call style schemas for use in the blind-user harness.
    * Dispatch tool calls by name.
    """

    def __init__(self) -> None:
        self._tools: dict[str, _ToolDef] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def add(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        fn: Callable[..., Any],
        profiles: frozenset[str] | set[str] | None = None,
    ) -> None:
        """Add a tool definition to the registry.

        Parameters
        ----------
        name:
            Unique tool name.
        description:
            Human-readable description surfaced to the LLM.
        parameters:
            JSON Schema ``object`` describing the tool's arguments.
        fn:
            Callable invoked when the tool is dispatched.
        profiles:
            Set of mode names (``"workspace"``, ``"headless"``) in which this
            tool should be exposed.  Defaults to all profiles.
        """
        resolved_profiles: frozenset[str] = (
            frozenset(profiles) if profiles is not None else _ALL_PROFILES
        )
        self._tools[name] = _ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            fn=fn,
            profiles=resolved_profiles,
        )

    def register_all(self, mcp: FastMCP, profile: str | None = None) -> None:
        """Register tools with *mcp*, optionally filtered by *profile*.

        Parameters
        ----------
        mcp:
            The :class:`~mcp.server.fastmcp.FastMCP` instance to register
            tools with.
        profile:
            When given (``"workspace"`` or ``"headless"``), only tools whose
            ``profiles`` set contains *profile* are registered.  When
            ``None``, all tools are registered.
        """
        for tool in self._tools.values():
            if profile is None or profile in tool.profiles:
                # FastMCP.tool() can be used as a decorator factory; we apply it
                # manually so the function stays importable as a plain callable.
                mcp.tool(name=tool.name, description=tool.description)(tool.fn)

    # ------------------------------------------------------------------
    # OpenAI / function-call schema export
    # ------------------------------------------------------------------

    def tool_schemas(self, profile: str | None = None) -> list[dict[str, Any]]:
        """Return a list of OpenAI-style function-call tool schemas.

        Parameters
        ----------
        profile:
            When given, return only schemas for tools in that profile.
            When ``None``, return all schemas.

        Each entry has the shape::

            {
                "type": "function",
                "function": {
                    "name": "<tool-name>",
                    "description": "...",
                    "strict": True,
                    "parameters": { <JSON Schema object> },
                },
            }
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "strict": True,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
            if profile is None or profile in t.profiles
        ]

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call the tool registered under *name* with *arguments*.

        Parameters
        ----------
        name:
            Tool name as registered (e.g. ``"waveflow_get_components"``).
        arguments:
            Keyword arguments forwarded to the tool function.

        Raises
        ------
        ValueError
            If *name* is not registered.
        """
        tool = self._tools.get(name)
        if tool is None:
            known = sorted(self._tools.keys())
            raise ValueError(
                f"Unknown tool name: {name!r}. Registered tools: {known}"
            )
        return tool.fn(**arguments)


# ---------------------------------------------------------------------------
# Global registry instance and tool registrations
# ---------------------------------------------------------------------------

REGISTRY = ToolRegistry()

REGISTRY.add(
    name="waveflow_get_schema_draft_plan",
    description=(
        "Return a deterministic step-by-step workflow for drafting a new "
        "waveflow schema from a natural-language request. This helper does "
        "not search, rank, or recommend specific example IDs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": ["string", "null"],
                "description": "Optional natural-language description of the schema the user wants to draft.",
            },
            "workspace_root": {
                "type": ["string", "null"],
                "description": "Optional workspace root path accepted for consistency with other helper tools.",
            },
        },
        "required": ["task", "workspace_root"],
        "additionalProperties": False,
    },
    fn=get_schema_draft_plan,
    profiles={"workspace", "headless"},
)

REGISTRY.add(
    name="waveflow_validate_schema",
    description=(
        "Validate a waveflow schema source file and write a structured "
        "JSON report to the specified output path. Returns a compact result "
        "with ok/error_count/warning_count/report_path/summary."
    ),
    parameters={
        "type": "object",
        "properties": {
            "schema_name": {
                "type": "string",
                "description": "Human-readable label for the schema being validated.",
            },
            "input_path": {
                "type": "string",
                "description": "Path to the Python source file containing the schema definition.",
            },
            "output_path": {
                "type": "string",
                "description": "Path where the JSON validation report will be written.",
            },
        },
        "required": ["schema_name", "input_path", "output_path"],
        "additionalProperties": False,
    },
    fn=validate_schema_from_file,
    profiles={"workspace", "headless"},
)

REGISTRY.add(
    name="waveflow_get_components",
    description=(
        "Return the canonical waveflow schema vocabulary glossary. "
        "Includes all core schema classes (DataSchema, DataList, DataArray, "
        "DataField, IntField, FloatField, EnumField, MemAddr, IntEnum) and "
        "common design patterns with descriptions and keywords. "
        "Call this first to select relevant keywords for "
        "waveflow_rag_search_examples. Deterministic; no network access."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    fn=get_components,
    profiles={"workspace", "headless"},
)

REGISTRY.add(
    name="waveflow_rag_search_examples",
    description=(
        "Search the OpenAI-hosted vector store of waveflow example corpus files. "
        "Returns the top-k most relevant example snippets for the given task. "
        "Requires WAVEFLOW_EXAMPLES_VECTOR_STORE_ID env var to be set. "
        "Use waveflow_get_components first to obtain good keywords."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Natural-language description of the schema you want to build.",
            },
            "keywords": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": (
                    "Optional waveflow vocabulary keywords (from waveflow_get_components) "
                    "to augment the search query."
                ),
            },
            "k": {
                "type": ["integer", "null"],
                "description": "Maximum number of matches to return (default 5, max 20).",
            },
        },
        "required": ["task", "keywords", "k"],
        "additionalProperties": False,
    },
    fn=search_schema_examples,
    profiles={"workspace", "headless"},
)



# ---------------------------------------------------------------------------
# Knowledge tools (Stage 1): local search and read over the guide and examples
# ---------------------------------------------------------------------------

REGISTRY.add(
    name="waveflow_browse",
    description=(
        "Browse the Waveflow documentation tree. Returns each page's path, "
        "title and summary, plus the child sections. Call with no section for "
        "the top level, then 'guide', 'examples', or a subsection such as "
        "'guide/custom_hooks'. Use this when you know what you want to do but "
        "not what Waveflow calls it -- the summaries let you match on meaning. "
        "section='examples' lists the example cards instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "section": {
                "type": ["string", "null"],
                "description": (
                    "Section to list: 'guide', 'examples', or a path such as "
                    "'guide/custom_hooks'. Omit for the top level."
                ),
            },
        },
        "required": ["section"],
        "additionalProperties": False,
    },
    fn=waveflow_browse,
    profiles={"workspace", "headless"},
)

REGISTRY.add(
    name="waveflow_search",
    description=(
        "Keyword search over the Waveflow guide and the reference examples. "
        "Returns ranked pointers: path, heading, line range and a short "
        "snippet -- then read the whole file with waveflow_get_doc or "
        "waveflow_get_example. Best when the query uses Waveflow's own "
        "vocabulary (DataList, HostActivated, TLAST, cosim); if a plain-English "
        "query misses, use waveflow_browse instead. Generated code is excluded "
        "unless include_generated is true."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search terms; identifiers work well.",
            },
            "scope": {
                "type": ["string", "null"],
                "enum": ["all", "docs", "examples", None],
                "description": "Restrict to 'docs' or 'examples'. Default 'all'.",
            },
            "k": {
                "type": ["integer", "null"],
                "description": "Maximum hits to return (default 8, max 50).",
            },
            "include_generated": {
                "type": ["boolean", "null"],
                "description": (
                    "Include generated code and other build output. Default "
                    "false. These files must never be hand-edited."
                ),
            },
        },
        "required": ["query", "scope", "k", "include_generated"],
        "additionalProperties": False,
    },
    fn=waveflow_search,
    profiles={"workspace", "headless"},
)

REGISTRY.add(
    name="waveflow_find_usage",
    description=(
        "Show every place a Waveflow symbol is actually used in the reference "
        "examples, grouped by example: file, line and how it is used (import, "
        "base class, decorator, port, call). Accepts a Python name "
        "('HostActivated', 'DataList', 'synthesizable'), a C++ namespace call "
        "('streamutils::read_stream'), or an HLS pragma "
        "('#pragma HLS pipeline'). Use this rather than search when you want "
        "working code for a specific name. Misses return close alternatives."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": (
                    "The exact symbol, namespace call or pragma to look up."
                ),
            },
            "include_generated": {
                "type": ["boolean", "null"],
                "description": (
                    "Include generated code and build output. Default false."
                ),
            },
        },
        "required": ["symbol", "include_generated"],
        "additionalProperties": False,
    },
    fn=waveflow_find_usage,
    profiles={"workspace", "headless"},
)

REGISTRY.add(
    name="waveflow_list_examples",
    description=(
        "List every Waveflow reference example with its synopsis, source "
        "directory, module classes and their kinds, ports, hand-written hook "
        "files and build script. These are the designs worth copying; "
        "directories under examples/ that are not listed here are older work "
        "and must not be used as models. Call this before starting a new "
        "design to choose the closest reference."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    fn=waveflow_list_examples,
    profiles={"workspace", "headless"},
)

REGISTRY.add(
    name="waveflow_get_example",
    description=(
        "Get one reference example: its card plus the full file list, or -- "
        "with 'file' -- the whole contents of one of its files. Read files "
        "whole rather than working from search snippets. Files tagged "
        "generated are codegen output and must never be hand-edited."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Example name from waveflow_list_examples, e.g. "
                    "'stream_inband'."
                ),
            },
            "file": {
                "type": ["string", "null"],
                "description": (
                    "A file from the example, as a full path or a bare "
                    "filename such as 'poly.py'. Omit for the card."
                ),
            },
        },
        "required": ["name", "file"],
        "additionalProperties": False,
    },
    fn=waveflow_get_example,
    profiles={"workspace", "headless"},
)

REGISTRY.add(
    name="waveflow_get_doc",
    description=(
        "Get a whole documentation page, or one section of it by heading. "
        "Takes a path from waveflow_browse or waveflow_search, such as "
        "'docs/guide/custom_hooks/writing.md'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Page path, e.g. 'docs/guide/custom_hooks/writing.md'."
                ),
            },
            "heading": {
                "type": ["string", "null"],
                "description": "Return only this section. Omit for the whole page.",
            },
        },
        "required": ["path", "heading"],
        "additionalProperties": False,
    },
    fn=waveflow_get_doc,
    profiles={"workspace", "headless"},
)
