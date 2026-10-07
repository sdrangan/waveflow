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

import functools
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from waveflow.mcp.components import get_components
from waveflow.mcp.frames import waveflow_get_process, waveflow_list_frames
from waveflow.mcp.knowledge import (
    waveflow_browse,
    waveflow_find_usage,
    waveflow_get_doc,
    waveflow_get_example,
    waveflow_list_examples,
    waveflow_search,
)
from waveflow.mcp.knowledge.roots import RootNotFound
from waveflow.mcp.scaffold import ScaffoldError, waveflow_new_accel_project
from waveflow.mcp.schema_tools import validate_schema_from_file

#: Exceptions a tool raises on purpose, with a message written for the model:
#: a bad argument (``ValueError``), a project the scaffold cannot write
#: (``ScaffoldError``), no checkout to index (``RootNotFound``).  Since mcp 2 the
#: model reads the message of a ``ToolError`` and nothing else -- any other
#: exception reaches it as just "Error executing tool <name>" -- so these are
#: re-raised as ``ToolError``.  Everything else is a crash and stays generic, as
#: mcp 2 intends; the server still logs its traceback.
ANTICIPATED_ERRORS: tuple[type[Exception], ...] = (ValueError, ScaffoldError, RootNotFound)


def anticipated_as_tool_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap *fn* so an :data:`ANTICIPATED_ERRORS` reaches the model as a ``ToolError``.

    ``functools.wraps`` keeps the signature, which is what ``MCPServer`` builds
    the tool's JSON-Schema from.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ANTICIPATED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


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
    * Register tools with a :class:`~mcp.server.mcpserver.MCPServer` instance,
      optionally filtered by *profile* (``"workspace"`` or ``"headless"``),
      passing their anticipated errors on to the model (see
      :data:`ANTICIPATED_ERRORS`).
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

    def register_all(self, mcp: MCPServer, profile: str | None = None) -> None:
        """Register tools with *mcp*, optionally filtered by *profile*.

        Each tool is registered through :func:`anticipated_as_tool_errors`, so
        the messages its anticipated errors carry reach the model.

        Parameters
        ----------
        mcp:
            The :class:`~mcp.server.mcpserver.MCPServer` instance to register
            tools with.
        profile:
            When given (``"workspace"`` or ``"headless"``), only tools whose
            ``profiles`` set contains *profile* are registered.  When
            ``None``, all tools are registered.
        """
        for tool in self._tools.values():
            if profile is None or profile in tool.profiles:
                # MCPServer.tool() can be used as a decorator factory; we apply it
                # manually so the function stays importable as a plain callable.
                mcp.tool(name=tool.name, description=tool.description)(
                    anticipated_as_tool_errors(tool.fn)
                )

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
        "Deterministic; no network access. To find the same vocabulary in "
        "working code, use waveflow_find_usage; to find the page that "
        "explains it, waveflow_search."
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


# ---------------------------------------------------------------------------
# Frames (Stage 3): the process an agent follows to build an accelerator
# ---------------------------------------------------------------------------

REGISTRY.add(
    name="waveflow_list_frames",
    description=(
        "List the accelerator architectures Waveflow can guide you through "
        "building ('frames'). Each entry gives the frame's name, what it is "
        "for, the reference example it is modelled on, and the example "
        "function specs that come with it. Call this if you do not already "
        "know which frame you were asked for."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    fn=waveflow_list_frames,
    profiles={"workspace", "headless"},
)

REGISTRY.add(
    name="waveflow_get_process",
    description=(
        "Get the build process for an accelerator frame: the ordered steps, "
        "which tool to use at each one, the two-stage freeze rule, and the "
        "rules about generated files and hand-packing. Returns the frame's "
        "specification alongside it. **Call this first** when asked to build "
        "an accelerator, before reading any source or writing anything -- it "
        "is the same text the scaffold writes as the project's AGENTS.md."
    ),
    parameters={
        "type": "object",
        "properties": {
            "frame": {
                "type": ["string", "null"],
                "description": (
                    "Frame name from waveflow_list_frames. Defaults to "
                    "'stream_inband'."
                ),
            },
        },
        "required": ["frame"],
        "additionalProperties": False,
    },
    fn=waveflow_get_process,
    profiles={"workspace", "headless"},
)


REGISTRY.add(
    name="waveflow_new_accel_project",
    description=(
        "Scaffold a new accelerator project that runs before it is edited: "
        "the reference example for the frame, renamed, with the compute "
        "stubbed to an identity pass-through and spec/ stubs added. Writes "
        "AGENTS.md (the frame's process) and a copy of frame.md into the "
        "project. Use this instead of copying an example by hand -- the "
        "rename touches schemas, class names, the C++ namespace, the kernel "
        "name and the build DAG. After it returns, `python <name>_build.py "
        "--through py_sim` passes with no edits; confirm that before writing "
        "anything."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Project name, lower_snake_case. Becomes the Python "
                    "module, the C++ kernel name and the namespace."
                ),
            },
            "frame": {
                "type": ["string", "null"],
                "description": "Frame name. Defaults to 'stream_inband'.",
            },
            "directory": {
                "type": ["string", "null"],
                "description": "Where to write it. Defaults to ./<name>.",
            },
        },
        "required": ["name", "frame", "directory"],
        "additionalProperties": False,
    },
    fn=waveflow_new_accel_project,
    profiles={"workspace", "headless"},
)
