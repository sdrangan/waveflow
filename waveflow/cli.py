"""The ``waveflow`` command.

CLI first, MCP as a thin wrapper.  Every knowledge tool is a plain Python
function; this module gives each one a terminal entry point so an agent with no
MCP support -- or a person debugging why a query missed -- can reach it without
starting a server.

    waveflow kb browse guide/custom_hooks
    waveflow kb search "error when TLAST arrives early"
    waveflow kb usage HostActivated
    waveflow kb examples
    waveflow kb example stream_inband --file poly.py
    waveflow kb doc docs/guide/custom_hooks/writing.md
    waveflow frames
    waveflow process stream_inband

Output is JSON by default, so the CLI and the MCP tools return the same thing;
``--text`` prints the human-readable rendering instead.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from waveflow.mcp.frames import waveflow_get_process, waveflow_list_frames
from waveflow.mcp.knowledge import (
    waveflow_browse,
    waveflow_find_usage,
    waveflow_get_doc,
    waveflow_get_example,
    waveflow_list_examples,
    waveflow_search,
)

__all__ = ["main", "build_parser"]


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def _render(cmd: str, data: dict[str, Any]) -> str:
    if "error" in data:
        extra = {k: v for k, v in data.items() if k != "error"}
        tail = f"\n  {json.dumps(extra)}" if extra else ""
        return f"error: {data['error']}{tail}"

    out: list[str] = []
    if cmd == "search":
        for hit in data["hits"]:
            out.append(
                f"{hit['path']}:{hit['lines'][0]}-{hit['lines'][1]}"
                f"  [{hit['score']}]  {hit['heading']}"
            )
            for line in hit["snippet"].splitlines():
                out.append(f"    {line}")
            if hit.get("note"):
                out.append(f"    ({hit['note']})")
        if not data["hits"]:
            out.append("(no hits)")
    elif cmd == "browse":
        for section in data.get("sections", []):
            out.append(f"{section['section']:<10} {section['summary']}")
        for page in data.get("pages", []):
            out.append(f"{page['path']}\n    {page['title']} -- {page['summary']}")
        for sub in data.get("subsections", []):
            out.append(f"{sub['section']:<40} ({sub['n_pages']} pages)")
        for ex in data.get("examples", []):
            out.append(f"{ex['name']:<18} {ex['synopsis']}")
    elif cmd == "usage":
        if not data.get("found"):
            out.append(f"{data['symbol']}: not found")
            if data.get("suggestions"):
                out.append(f"  did you mean: {', '.join(data['suggestions'])}")
        else:
            out.append(f"{data['symbol']}: {data['n_uses']} uses")
            for example, entries in data["by_example"].items():
                out.append(f"  {example}")
                for e in entries:
                    where = f" in {e['in']}" if e.get("in") else ""
                    out.append(f"    {e['path']}:{e['line']}  {e['how']}{where}")
    elif cmd == "examples":
        for ex in data["examples"]:
            out.append(f"{ex['name']:<18} {ex['example_dir']}")
            out.append(f"    {ex['synopsis']}")
    elif cmd == "example":
        if "content" in data:
            return data["content"]
        ex = data["example"]
        out.append(f"{ex['name']}  ({ex['example_dir']})")
        out.append(f"  {ex['synopsis']}")
        for m in ex["modules"]:
            ports = ", ".join(f"{p['name']}:{p['interface']}" for p in m["ports"])
            out.append(f"  {m['name']:<24} {m['kind']}" + (f"  [{ports}]" if ports else ""))
        if ex["hook_files"]:
            out.append("  hooks: " + ", ".join(ex["hook_files"]))
        if ex["build_script"]:
            out.append(f"  build: {ex['build_script']}")
    elif cmd == "doc":
        return data["content"]
    elif cmd == "frames":
        for frame in data["frames"]:
            out.append(f"{frame['name']:<18} {frame['synopsis']}")
            for prompt in frame["prompts"]:
                out.append(f"    {prompt['file']:<22} {prompt['synopsis']}")
    elif cmd == "process":
        return data["process"]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="waveflow", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="group", required=True)

    kb = sub.add_parser("kb", help="search and read the guide and the examples")
    kb.add_argument("--text", action="store_true", help="human-readable output")
    kb.add_argument(
        "--root",
        help=(
            "repository root to index (default: this checkout). Sets "
            "WAVEFLOW_KB_ROOT, which is also how the MCP server is pointed "
            "at a different tree -- the tools themselves take no such "
            "argument, so a model cannot repoint the index."
        ),
    )
    kbsub = kb.add_subparsers(dest="cmd", required=True)

    p = kbsub.add_parser("browse", help="the doc tree, with titles and summaries")
    p.add_argument("section", nargs="?", help="e.g. guide, examples, guide/custom_hooks")

    p = kbsub.add_parser("search", help="BM25 over heading-sized chunks")
    p.add_argument("query")
    p.add_argument("--scope", default="all", choices=["all", "docs", "examples"])
    p.add_argument("-k", type=int, default=8)
    p.add_argument("--generated", action="store_true", help="include generated files")

    p = kbsub.add_parser("usage", help="every place a symbol is used")
    p.add_argument("symbol")
    p.add_argument("--generated", action="store_true")

    kbsub.add_parser("examples", help="every example card")

    p = kbsub.add_parser("example", help="one example card, or one whole file")
    p.add_argument("name")
    p.add_argument("--file", help="a file from the example, returned whole")

    p = kbsub.add_parser("doc", help="a whole doc page, or one section")
    p.add_argument("path")
    p.add_argument("--heading")

    # `frames` and `process` sit beside `kb` rather than under it: they are
    # the entry point for building something, not a way to look something up.
    frames = sub.add_parser("frames", help="the accelerator architectures on offer")
    frames.add_argument("--text", action="store_true")

    process = sub.add_parser(
        "process", help="the ordered steps for building in a frame"
    )
    process.add_argument("frame", nargs="?", default="stream_inband")
    process.add_argument("--text", action="store_true")

    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.group == "frames":
        return waveflow_list_frames()
    if args.group == "process":
        return waveflow_get_process(args.frame)

    if args.root:
        os.environ["WAVEFLOW_KB_ROOT"] = args.root

    if args.cmd == "browse":
        return waveflow_browse(args.section)
    if args.cmd == "search":
        return waveflow_search(
            args.query,
            scope=args.scope,
            k=args.k,
            include_generated=args.generated,
        )
    if args.cmd == "usage":
        return waveflow_find_usage(args.symbol, include_generated=args.generated)
    if args.cmd == "examples":
        return waveflow_list_examples()
    if args.cmd == "example":
        return waveflow_get_example(args.name, file=args.file)
    if args.cmd == "doc":
        return waveflow_get_doc(args.path, heading=args.heading)
    raise AssertionError(f"unhandled kb command {args.cmd!r}")


def _utf8_stdout() -> None:
    """Make stdout able to carry the docs.

    Everything this command prints is documentation or source, and both are
    full of ``->``-as-arrow, em dashes and box drawing.  A default Windows
    console is cp1252, so ``waveflow process`` died with a UnicodeEncodeError
    on character 5305 of its own output.  Replacing rather than failing: a
    mangled arrow is a far better outcome than no process text at all.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _utf8_stdout()
    data = _dispatch(args)
    if args.text:
        print(_render(getattr(args, "cmd", args.group), data))
    else:
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 1 if "error" in data else 0


if __name__ == "__main__":
    raise SystemExit(main())
