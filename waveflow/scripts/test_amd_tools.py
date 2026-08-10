"""
test_amd_tools.py — Check that Waveflow can find the AMD/Xilinx tools

Waveflow runs Python simulations with no AMD tools installed.  Synthesis and RTL-level
simulation need Vitis and Vivado, which Waveflow locates through the
``WAVEFLOW_VITIS_PATH`` environment variable (see the *Connecting Vitis and Vivado* page in
the guide).  This script reports whether that lookup succeeds, and which release it lands on.

Usage
-----
    test_amd_tools                # locate both tools and report their versions
    test_amd_tools --fast         # skip launching the tools; read versions from the paths

Exit status is 0 when both tools are found at a supported release, and 1 otherwise, so the
check can gate a CI job or a lab setup script.
"""

import argparse
import platform
import sys
from typing import List, Optional, Sequence

from waveflow.toolchain.toolchain import (
    MIN_TOOL_VERSION,
    VITIS_PATH_ENV,
    ToolInfo,
    probe_amd_tools,
)

_MIN_VERSION_STR = f"{MIN_TOOL_VERSION[0]}.{MIN_TOOL_VERSION[1]}"


def _status(info: ToolInfo) -> str:
    if not info.found:
        return "NOT FOUND"
    if info.meets_min is False:
        return "TOO OLD"
    if info.meets_min is None:
        return "FOUND (version unknown)"
    return "OK"


def _format_report(infos: Sequence[ToolInfo]) -> List[str]:
    lines = ["", "Waveflow AMD/Xilinx tool check", "=" * 30, ""]

    for info in infos:
        lines.append(f"{info.name}")
        lines.append(f"  status   : {_status(info)}")
        lines.append(f"  version  : {info.version or 'unknown'}")
        lines.append(f"  path     : {info.path or '-'}")
        lines.append("")

    return lines


def _hint_lines() -> List[str]:
    """Platform-appropriate instructions for pointing Waveflow at an install."""
    if platform.system() == "Windows":
        example = r"C:\Xilinx"
        setter = f'  setx {VITIS_PATH_ENV} "{example}"      (then open a new terminal)'
    else:
        example = "/tools/Xilinx"
        setter = (
            f"  export {VITIS_PATH_ENV}={example}          # bash / zsh\n"
            f"  setenv {VITIS_PATH_ENV} {example}          # tcsh / csh"
        )

    return [
        f"Set {VITIS_PATH_ENV} to the directory holding your AMD installs — the one",
        f"containing a version directory such as {example}/{_MIN_VERSION_STR}/Vitis:",
        "",
        setter,
        "",
        "Vivado is found automatically next to Vitis; only a split install needs",
        "WAVEFLOW_VIVADO_PATH as well.  Add the setting to your shell startup file so it",
        "persists.  See the 'Connecting Vitis and Vivado' page in the Waveflow guide.",
    ]


def report_amd_tools(check_versions: bool = True) -> int:
    """
    Print the tool report and return the process exit status.

    Parameters
    ----------
    check_versions : bool, optional
        If ``True`` (default), run each tool to read its release. This takes a few seconds
        per tool.

    Returns
    -------
    int
        ``0`` when both tools were found at a supported release, ``1`` otherwise.
    """
    infos = probe_amd_tools(check_versions=check_versions)
    lines = _format_report(infos)

    missing = [i for i in infos if not i.found]
    outdated = [i for i in infos if i.meets_min is False]
    unknown = [i for i in infos if i.found and i.meets_min is None]

    if missing:
        names = " and ".join(i.name for i in missing)
        lines.append(f"WARNING: {names} could not be found.")
        lines.append("")
        lines.append("Python simulation works without the AMD tools, but synthesis and")
        lines.append("RTL-level simulation will not run until they are set up.")
        lines.append("")
        lines.extend(_hint_lines())
    elif outdated:
        names = " and ".join(f"{i.name} {i.version}" for i in outdated)
        lines.append(f"WARNING: {names} is older than the supported {_MIN_VERSION_STR}.")
        lines.append("Waveflow may still work, but this configuration is not tested.")
    elif unknown:
        names = " and ".join(i.name for i in unknown)
        lines.append(f"WARNING: found {names}, but could not read a version from it.")
        lines.append(f"Waveflow expects {_MIN_VERSION_STR} or newer.")
    else:
        versions = ", ".join(f"{i.name} {i.version}" for i in infos)
        lines.append(f"All tools found: {versions}.")
        lines.append(f"Waveflow is ready for synthesis (minimum {_MIN_VERSION_STR}).")

    lines.append("")
    print("\n".join(lines))

    return 1 if (missing or outdated or unknown) else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="test_amd_tools",
        description="Check that Waveflow can find the AMD/Xilinx Vitis and Vivado tools.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="do not launch the tools; read versions from their install paths instead",
    )
    args = parser.parse_args(argv)

    return report_amd_tools(check_versions=not args.fast)


if __name__ == "__main__":
    sys.exit(main())
