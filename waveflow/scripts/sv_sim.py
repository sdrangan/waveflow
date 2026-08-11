#!/usr/bin/env python3
"""sv_sim — run a SystemVerilog testbench through the Vivado simulator.

Two layers:

* :func:`run_sv_sim` — the library entry point.  Runs ``xvlog`` / ``xelab`` /
  ``xsim`` in a scratch directory and **raises** :class:`SvSimError` if any of
  them fails.  This is what build steps and tests call.
* :func:`main` — a thin CLI wrapper that catches :class:`SvSimError` and exits
  with the tool's return code.

The split matters: the CLI's ``sys.exit`` would tear down the interpreter of
anything that imported it, so a ``BuildDag`` calling into the CLI form would die
rather than record a failed step.

Every command is echoed before it runs.  A missing Vivado installation is by far
the most common failure here, and the echoed command line is what makes that
diagnosable at a glance rather than by guesswork.

CLI::

    sv-sim -s adder.sv -tb tb_adder.sv
    sv-sim -s a.sv b.sv --tb tb_top.sv --top tb_top --sim build/sim --keep
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from waveflow.toolchain.toolchain import find_vivado_path


class SvSimError(RuntimeError):
    """A Vivado simulation command exited non-zero.

    Attributes
    ----------
    command : str
        The command line that failed, exactly as it was run.
    returncode : int
        Process exit status.
    output : str | None
        Captured stdout+stderr, when the caller asked for capture.  ``None``
        when the tools streamed straight to the console.
    """

    def __init__(self, command: str, returncode: int, output: str | None = None) -> None:
        self.command = command
        self.returncode = returncode
        self.output = output
        detail = f"\n{output.rstrip()}" if output else ""
        super().__init__(f"command failed (exit {returncode}): {command}{detail}")


@dataclass
class SvSimResult:
    """What a completed :func:`run_sv_sim` produced."""

    top: str
    sim_dir: Path
    commands: list[str] = field(default_factory=list)
    xelab_log: Path | None = None
    xsim_log: Path | None = None
    output: str | None = None


def sim_tool(name: str) -> str:
    """Resolve an xsim-family tool (``xvlog`` / ``xelab`` / ``xsim``) to a runnable command.

    The simulator binaries live in the same ``bin`` directory as ``vivado``, so
    :func:`~waveflow.toolchain.toolchain.find_vivado_path` locates them without the caller
    having to source ``settings64``.  Falls back to the bare name, which is correct when an
    environment module or a sourced settings script has already put them on ``PATH``.
    """
    exe = f"{name}.bat" if platform.system() == "Windows" else name
    vivado = find_vivado_path()
    if vivado:
        candidate = Path(vivado).parent / exe
        if candidate.exists():
            return str(candidate)
    return name


def _run(cmd: str, cwd: Path, *, capture_output: bool, echo: bool) -> str | None:
    """Run one shell command, echoing it first; raise :class:`SvSimError` on failure."""
    if echo:
        print(f"\n>>> {cmd}", flush=True)
    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        capture_output=capture_output,
        text=True,
    )
    out = None
    if capture_output:
        out = (proc.stdout or "") + (proc.stderr or "")
        if echo and out:
            print(out, end="", flush=True)
    if proc.returncode != 0:
        raise SvSimError(cmd, proc.returncode, out)
    return out


def run_sv_sim(
    sources: Sequence[str | Path],
    tb: str | Path,
    *,
    top: str | None = None,
    sim_dir: str | Path = "sim",
    keep: bool = False,
    tcl: str | Path | None = None,
    plusargs: dict[str, str | Path] | None = None,
    capture_output: bool = False,
    echo: bool = True,
) -> SvSimResult:
    """Compile, elaborate and run a SystemVerilog testbench with Vivado's simulator.

    Parameters
    ----------
    sources:
        Design sources.  Resolved to absolute paths, so *sim_dir* may live
        anywhere — it need not be a child of the current directory.
    tb:
        The testbench file.
    top:
        Top module name.  Defaults to the testbench filename without extension.
    sim_dir:
        Scratch directory for the simulator's outputs.
    keep:
        Keep an existing *sim_dir* instead of deleting it first.
    tcl:
        Optional ``run.tcl`` for xsim.  Without it, xsim is given ``--runall``.
    plusargs:
        ``name -> value`` pairs passed to xsim as ``-testplusarg name=value`` and read in the
        testbench with ``$value$plusargs``.  Chiefly useful for handing a testbench absolute
        paths to its input data, so it does not have to guess where *sim_dir* sits relative to
        the rest of the project.
    capture_output:
        Capture stdout+stderr and attach it to :class:`SvSimError` on failure.
        Off by default so the tools stream live, which is usually what you want
        interactively.
    echo:
        Print each command before running it.

    Returns
    -------
    SvSimResult

    Raises
    ------
    SvSimError
        If ``xvlog``, ``xelab`` or ``xsim`` exits non-zero.
    """
    tb_path = Path(tb).resolve()
    src_paths = [Path(s).resolve() for s in sources]
    if top is None:
        top = tb_path.stem

    sim_path = Path(sim_dir)
    if not keep and sim_path.exists():
        shutil.rmtree(sim_path)
    sim_path.mkdir(parents=True, exist_ok=True)
    (sim_path / "logs").mkdir(exist_ok=True)

    def q(p: Path) -> str:
        return f'"{p}"'

    xvlog, xelab, xsim = (sim_tool(n) for n in ("xvlog", "xelab", "xsim"))

    vlog_cmd = f'"{xvlog}" -sv ' + " ".join(q(p) for p in [*src_paths, tb_path])
    elab_cmd = f'"{xelab}" {top} -s {top}_sim -debug typical -log logs/xelab.log'
    if tcl is not None:
        sim_cmd = f'"{xsim}" {top}_sim -t "{Path(tcl).resolve()}" -log logs/xsim.log'
    else:
        sim_cmd = f'"{xsim}" {top}_sim --runall -log logs/xsim.log'
    for key, value in (plusargs or {}).items():
        # Absolute paths keep the testbench independent of where sim_dir sits.  Paths are
        # emitted in POSIX form: a Windows path reaches $value$plusargs with its backslashes
        # interpreted as escapes, so "...\repos\..." arrives carrying a carriage return.
        # $fopen accepts forward slashes on Windows, so this costs nothing.
        text = value.as_posix() if isinstance(value, Path) else value
        sim_cmd += f' -testplusarg "{key}={text}"'

    commands = [vlog_cmd, elab_cmd, sim_cmd]
    chunks: list[str] = []
    for cmd in commands:
        out = _run(cmd, sim_path, capture_output=capture_output, echo=echo)
        if out:
            chunks.append(out)

    return SvSimResult(
        top=top,
        sim_dir=sim_path,
        commands=commands,
        xelab_log=sim_path / "logs" / "xelab.log",
        xsim_log=sim_path / "logs" / "xsim.log",
        output="".join(chunks) if chunks else None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI wrapper: parse arguments, call :func:`run_sv_sim`, translate failure to an exit code."""
    parser = argparse.ArgumentParser(description="Simple Vivado SystemVerilog simulator wrapper")
    parser.add_argument("--source", "-s", nargs="+", required=True,
                        help="SystemVerilog source files (one or more)")
    parser.add_argument("--tb", required=True,
                        help="Testbench file (single file)")
    parser.add_argument("--top", default=None,
                        help="Optional top module name (defaults to testbench filename without extension)")
    parser.add_argument("--sim", default="sim",
                        help="Simulation directory (default: sim)")
    parser.add_argument("--keep", action="store_true",
                        help="Keep existing sim directory (default: False, deletes before running)")
    parser.add_argument("--t", default=None,
                        help="Optional path to a run.tcl file for xsim (if not provided, uses --runall)")
    args = parser.parse_args(argv)

    try:
        run_sv_sim(
            args.source,
            args.tb,
            top=args.top,
            sim_dir=args.sim,
            keep=args.keep,
            tcl=args.t,
        )
    except SvSimError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return exc.returncode or 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
