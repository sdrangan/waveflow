"""JSON-first command line transport for the bounded FIR service."""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import sys
from pathlib import Path


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv: list[str] | None = None) -> int:
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--root", type=Path, help="Host-owned durable run root (or WAVEFLOW_DSE_ROOT)")
        parser.add_argument("--config", type=Path, help="Host-only frozen configuration JSON file")
        commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
        commands.add_parser("context")
        commands.add_parser("schemas")
        call = commands.add_parser("call")
        call.add_argument("tool")
        call.add_argument("--args", default="{}")
        results = commands.add_parser("results")
        results.add_argument("--format", choices=("json", "csv"), default="json")
        results.add_argument("--offset", type=int, default=0)
        results.add_argument("--limit", type=int, default=100)
        commands.add_parser("serve")
        args = parser.parse_args(argv)
        arguments = json.loads(args.args) if args.command == "call" else {}
        if not isinstance(arguments, dict):
            raise TypeError("--args must be a JSON object")
        with contextlib.redirect_stdout(sys.stderr):
            from examples.dse_fir.dse_tools import make_dse_registry, make_service
            if args.command == "schemas":
                output = make_dse_registry(None).tool_schemas("dse")
            else:
                config = json.loads(args.config.read_text()) if args.config else None
                if config is not None and not isinstance(config, dict):
                    raise ValueError("--config must contain a JSON object")
                registry = make_dse_registry(make_service(args.root, config))
                if args.command == "serve":
                    from mcp.server.fastmcp import FastMCP
                    server = FastMCP("waveflow-fir-dse")
                    registry.register_all(server, "dse")
                elif args.command == "context":
                    output = registry.dispatch("dse_get_dse_context", {})
                elif args.command == "results":
                    output = registry.dispatch("dse_get_results", {"offset": args.offset, "limit": args.limit})
                else:
                    output = registry.dispatch(args.tool, arguments)
        if args.command == "serve":
            server.run(transport="stdio")
            return 0
        failed = isinstance(output, dict) and (
            output.get("ok") is False or output.get("status", "ok") != "ok"
            or (args.command == "call" and any(row.get("status", "ok") != "ok" for row in output.get("rows", [])))
        )
        if args.command == "results" and args.format == "csv" and not failed:
            data = output.get("data", output)
            rows = data["rows"]
            fields = list(dict.fromkeys(key for row in rows for key in row))
            buffer = io.StringIO(newline="")
            writer = csv.DictWriter(buffer, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: json.dumps(value, sort_keys=True) if isinstance(value, (list, dict)) else value for key, value in row.items()} for row in rows)
            sys.stdout.write(buffer.getvalue())
        else:
            print(json.dumps(output, allow_nan=False))
        return 1 if failed else 0
    except Exception as exc:  # noqa: BLE001 -- CLI errors must preserve the JSON stdout contract
        print(json.dumps({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
