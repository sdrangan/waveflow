"""Thin JSON CLI and four-tool stdio MCP transport for bound execution."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import redirect_stdout

from .service import ExecutionService, make_execution_registry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", choices=("fir", "mem_copy"), default="fir")
    parser.add_argument("--root", required=True)
    parser.add_argument("--conditions", default="{}", help="Host-only fixed conditions as JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("describe", "results", "serve"):
        commands.add_parser(name)
    call = commands.add_parser("call")
    call.add_argument("operation")
    call.add_argument("--args", default="{}")
    args = parser.parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            service = ExecutionService(args.root, args.design, json.loads(args.conditions))
        if args.command == "serve":
            from mcp.server.fastmcp import FastMCP
            server = FastMCP("waveflow-execution")
            make_execution_registry(service).register_all(server)
            server.run(transport="stdio")
            return 0
        if args.command == "describe":
            result = service.describe()
        elif args.command == "results":
            result = service.results()
        else:
            result = service.invoke(args.operation, json.loads(args.args))
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return int(args.command == "call" and result["status"] not in ("ok", "success"))
    except (ValueError, TypeError, OSError) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, allow_nan=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
