from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from waveflow.mcp.registry import REGISTRY

#: Injected into the model's context by most MCP clients, before it has called
#: anything.  It is the only text guaranteed to be read, so it carries the two
#: facts an agent cannot recover on its own: that Waveflow is probably not in
#: its training data, and which single call starts an accelerator.
INSTRUCTIONS = """Waveflow is a Python-native hardware design platform: one Python source is the single source of truth for simulation, the Vitis HLS kernel, its testbench, the firmware and the docs.

Waveflow is recent and specialised, so assume you do not know its API. Do not guess at a name -- look it up. The tools fall into four families:

- **Process.** `waveflow_get_process(frame)` returns the ordered steps for   building an accelerator, and `waveflow_list_frames()` says which   architectures exist. **Asked to build an accelerator? Call   `waveflow_get_process` first**, before reading source or writing anything.
- **Search and browse.** `waveflow_search` for Waveflow's own words   (`DataList`, `TLAST`, `VitisRegMap`, `cosim`); `waveflow_browse` when you   know what you want to do but not what Waveflow calls it -- it returns each   page's summary so you can match on meaning.
- **Read.** `waveflow_get_doc` for a whole page, `waveflow_list_examples` and   `waveflow_get_example` for the reference designs, `waveflow_find_usage` to   see a name used in real code. Search returns pointers; read whole files   before writing anything that depends on them.
- **Schemas.** `waveflow_get_components` for the vocabulary,   `waveflow_validate_schema` to check one you drafted.

Two rules that hold everywhere: anything a tool tags as **generated** is code-generation output that must never be hand-edited, and headers, footers and sample bursts are always serialized through their schema or the Waveflow array utilities, never packed into words by hand. If the machinery cannot express something, stop and report it rather than working around it.
"""

def build_mcp(
    mode: str = "workspace",
    work_dir: str | os.PathLike[str] | None = None,
) -> FastMCP:
    """Create and configure a waveflow MCP server for the given *mode*.

    Parameters
    ----------
    mode:
        ``"workspace"`` – for hosts (VS Code, Claude Code, …) that already
        provide workspace file and editing tools. Domain-specific helpers
        (schema drafting/validation, component glossary, and RAG example
        search) are registered; generic file tools are **not** exposed.

        ``"headless"`` – for standalone execution (unit tests, CI, API
        calls, …) where the host does not supply file tools.  Exposes all
        workspace-mode tools **plus** generic file tools scoped to
        *work_dir*.

    work_dir:
        Root directory for file tools.  **Required** when ``mode="headless"``.
        All file-tool paths are resolved relative to (and must stay within)
        this directory.

    Returns
    -------
    FastMCP
        A fully configured MCP server instance ready to be run with
        ``mcp.run(transport="stdio")``.

    Raises
    ------
    ValueError
        If *mode* is not ``"workspace"`` or ``"headless"``, or if
        ``mode="headless"`` but *work_dir* is ``None``.
    """
    if mode not in ("workspace", "headless"):
        raise ValueError(
            f"mode must be 'workspace' or 'headless', got {mode!r}"
        )
    if mode == "headless" and work_dir is None:
        raise ValueError("work_dir is required for headless mode")

    mcp_instance = FastMCP("waveflow", instructions=INSTRUCTIONS)
    REGISTRY.register_all(mcp_instance, profile=mode)

    if mode == "headless":
        from waveflow.mcp.file_tools import make_file_tools

        work_root = Path(work_dir).resolve()  # type: ignore[arg-type]
        list_files_fn, read_file_fn, write_file_fn, edit_file_fn = make_file_tools(
            work_root
        )

        mcp_instance.tool(
            name="list_files",
            description=(
                "List files and directories under a path within the configured "
                "work directory. path defaults to the work directory root."
            ),
        )(list_files_fn)
        mcp_instance.tool(
            name="read_file",
            description=(
                "Read the UTF-8 text content of a file within the configured "
                "work directory."
            ),
        )(read_file_fn)
        mcp_instance.tool(
            name="write_file",
            description=(
                "Write UTF-8 text content to a file within the configured "
                "work directory. Parent directories are created automatically. "
                "Existing files are overwritten."
            ),
        )(write_file_fn)
        mcp_instance.tool(
            name="edit_file",
            description=(
                "Replace a unique occurrence of old_str with new_str in a file "
                "within the configured work directory. Fails if old_str is not "
                "found or appears more than once."
            ),
        )(edit_file_fn)

    return mcp_instance


# Module-level instance used by the stdio entrypoint and backward-compatible
# imports (e.g. ``from waveflow.mcp.server import mcp``).
mcp = build_mcp(mode="workspace")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
