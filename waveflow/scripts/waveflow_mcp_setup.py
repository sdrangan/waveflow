#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
from importlib import resources
from pathlib import Path



TEMPLATE_PACKAGE = "waveflow.mcp.resources"
TEMPLATE_NAME = "mcp.json"
PYTHON_PLACEHOLDER = "__WAVEFLOW_PYTHON__"
IMPORT_PROBE = "import waveflow.mcp.server"


def load_template() -> dict:
    template_text = resources.files(TEMPLATE_PACKAGE).joinpath(TEMPLATE_NAME).read_text(
        encoding="utf-8"
    )
    return json.loads(template_text)


def render_mcp_config(*, python_path: str) -> str:
    config = load_template()
    servers = config.setdefault("servers", {})

    if "waveflow" not in servers:
        copied_server = None
        if len(servers) == 1:
            copied_server = next(iter(servers.values()))

        servers["waveflow"] = copied_server or {
            "type": "stdio",
            "args": ["-m", "waveflow.mcp.server"],
        }

    servers["waveflow"]["command"] = python_path

    return json.dumps(config, indent=2) + "\n"


def validate_python_interpreter(python_path: str) -> None:
    try:
        completed = subprocess.run(
            [python_path, "-c", IMPORT_PROBE],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(
            f"The selected Python interpreter is not runnable: {python_path}\n{exc}"
        ) from exc

    if completed.returncode == 0:
        return

    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    details = stderr or stdout or "Unknown error while importing waveflow.mcp.server"
    raise RuntimeError(
        "The selected Python interpreter cannot import waveflow.mcp.server: "
        f"{python_path}\n{details}"
    )


def write_mcp_config(
    *, workspace: Path, python_path: str, force: bool = False
) -> Path:
    vscode_dir = workspace / ".vscode"
    target_path = vscode_dir / "mcp.json"

    if target_path.exists() and not force:
        raise FileExistsError(
            f"Refusing to overwrite existing MCP config: {target_path}. Use --force to replace it."
        )

    vscode_dir.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        render_mcp_config(python_path=python_path),
        encoding="utf-8",
    )
    return target_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a VS Code MCP configuration for waveflow in a workspace."
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace folder where .vscode/mcp.json will be created. Defaults to the current directory.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter to use for the MCP server. Defaults to the interpreter running this command.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .vscode/mcp.json file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated config instead of writing it.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    python_path = str(Path(args.python).resolve())

    validate_python_interpreter(python_path)

    if args.dry_run:
        print(render_mcp_config(python_path=python_path), end="")
        return 0

    output_path = write_mcp_config(
        workspace=workspace,
        python_path=python_path,
        force=args.force,
    )
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())