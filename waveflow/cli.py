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

Output is JSON by default, so the CLI and the MCP tools return the same thing;
``--text`` prints the human-readable rendering instead.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

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
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="waveflow", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="group", required=True)

    kb = sub.add_parser("kb", help="search and read the guide and the examples")
    kb.add_argument("--text", action="store_true", help="human-readable output")
    kb.add_argument("--root", help="repository root to index (default: this checkout)")
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

    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root
    if args.cmd == "browse":
        return waveflow_browse(args.section, root=root)
    if args.cmd == "search":
        return waveflow_search(
            args.query,
            scope=args.scope,
            k=args.k,
            include_generated=args.generated,
            root=root,
        )
    if args.cmd == "usage":
        return waveflow_find_usage(
            args.symbol, include_generated=args.generated, root=root
        )
    if args.cmd == "examples":
        return waveflow_list_examples(root=root)
    if args.cmd == "example":
        return waveflow_get_example(args.name, file=args.file, root=root)
    if args.cmd == "doc":
        return waveflow_get_doc(args.path, heading=args.heading, root=root)
    raise AssertionError(f"unhandled kb command {args.cmd!r}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data = _dispatch(args)
    if args.text:
        print(_render(args.cmd, data))
    else:
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 1 if "error" in data else 0


if __name__ == "__main__":
    raise SystemExit(main())
