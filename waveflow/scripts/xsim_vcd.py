"""
xsim_vcd.py — Run Vivado simulation with VCD output

This script re-runs a Vivado HLS RTL simulation using `xsim` and generates a VCD (Value Change Dump) file
for waveform analysis. It runs on Windows and Linux; the only platform difference is the
launcher script Vitis emits beside the simulation (`run_xsim.bat` vs `run_xsim.sh`).

Usage (CLI):
First, run RTL co-simulation in Vitis HLS to generate the necessary simulation files.
In that simulation, enable trace capture with a setting such as `trace_level all`
or `trace_level port`.
Then, run this script from the command line:
```bash
    python xsim_vcd.py --top <top_function> [--comp <component_name>] [--out <output_file>]
```

Arguments:
    --top    (required) Name of the top-level function to simulate
    --comp   (optional) Name of the HLS component directory (default: 'hls_component')
    --out    (optional) Output VCD filename (default: 'dump.vcd')
        --trace_level (optional) VCD trace level. Built-in values `*`, `all`, and
            `port` reuse the trace selection from the generated simulation Tcl. You
            can also specify a custom hierarchical target such as `/top_function/*`.

Example:
```bash
    python xsim_vcd.py --top add  --out wave.vcd
```

This will run the simulation for the top function `add` in the component directory `hls_component`
and output the VCD file as `vcd/wave.vcd`.

Python API:
    You can also call this module from Python directly::

        from waveflow.scripts.xsim_vcd import run_xsim_vcd
        from pathlib import Path

        out_path = run_xsim_vcd(
            top="poly",
            comp="waveflow_poly_proj",
            out="dump.vcd",
        )
        print(f"VCD written to: {out_path}")
"""

import os
import sys
import shutil
import subprocess
import argparse
from pathlib import Path


def launcher_names(os_name: str | None = None) -> tuple[str, str]:
    """
    ``(original, vcd)`` simulation-launcher filenames for a platform.

    Vitis writes the cosim launcher beside the generated RTL as ``run_xsim.bat`` on Windows and
    ``run_xsim.sh`` on Linux.  Both contain the same two commands (``xelab`` then ``xsim``); only
    the shell differs.

    Parameters
    ----------
    os_name : str | None
        Platform in :data:`os.name` spelling.  Defaults to this host.  Exists so both forms stay
        testable from either OS; leave it ``None`` in production callers.
    """
    if (os_name or os.name) == "nt":
        return "run_xsim.bat", "run_xsim_vcd.bat"
    return "run_xsim.sh", "run_xsim_vcd.sh"


# ``log_wave`` spells "everything below here" as ``-r``/``-recursive``.  ``log_vcd`` has no such
# option: it takes ``-level``, whose default of 0 already means "this scope and every level below".
# A literal rewrite therefore produces ``log_vcd -r /``, which xsim 2025.1 rejects outright --
# ``ERROR: [Common 17-170] Unknown option '-r'`` -- and then exits 0, leaving a VCD with no ``$var``
# declarations at all.  The recursive form has to be translated, not copied.
_RECURSIVE_FLAGS = ('-recursive', '-r')


def _log_wave_to_log_vcd(command: str) -> str:
    """
    Rewrite one generated ``log_wave`` command as the ``log_vcd`` command tracing the same objects.

    Only the options ahead of the object list are touched; the object list itself is copied through
    verbatim, so the bracketed ``[get_objects -filter {...} ...]`` form Vitis emits for
    ``trace_level port`` survives intact.
    """
    args = command.strip()[len('log_wave'):].strip()

    recursive = False
    while True:
        for flag in _RECURSIVE_FLAGS:
            if args == flag or args.startswith(f'{flag} '):
                recursive = True
                args = args[len(flag):].strip()
                break
        else:
            break

    # ``log_wave -r /`` names the root scope.  ``log_vcd`` matches hdl_object *patterns*, so the
    # root has to be spelled as a wildcard.
    if args in ('', '/'):
        args = '/*'

    if recursive:
        args = f'-level 0 {args}'

    return f'log_vcd {args}'


def _get_log_vcd_command(lines, trace_level):
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('log_wave '):
            if trace_level in {'*', 'all', 'port'}:
                return f'{_log_wave_to_log_vcd(stripped)}\n'
            return f'log_vcd {trace_level}\n'

    raise RuntimeError(
        'Could not find a log_wave command in the simulation TCL. '
        'Re-run co-simulation with trace capture enabled before generating a VCD.'
    )


def modify_tcl(tcl_path, tcl_vcd_path, trace_level):
    with open(tcl_path, 'r') as f:
        lines = f.readlines()

    log_vcd_command = _get_log_vcd_command(lines, trace_level)

    # Insert VCD commands before log_wave
    for i, line in enumerate(lines):
        if line.strip().startswith('log_wave '):
            lines = lines[:i] + ['open_vcd\n', log_vcd_command] + lines[i:]
            break

    # Replace final lines
    for i in range(len(lines)):
        if lines[i].strip() == 'run all' and i + 1 < len(lines) and lines[i + 1].strip() == 'quit':
            lines[i + 1] = 'close_vcd\nquit\n'
            break

    with open(tcl_vcd_path, 'w') as f:
        f.writelines(lines)

def create_vcd_batch(top_name, original_bat, new_bat, os_name: str | None = None):
    """
    Write a launcher that re-runs only ``xsim``, pointed at the VCD-enabled Tcl.

    Just the ``xsim`` line is copied: the original cosim already ran ``xelab``, so the elaborated
    snapshot is on disk and re-elaborating would only cost time.  The header differs by platform
    (``cd /d "%~dp0"`` for cmd, ``cd "$(dirname "$0")"`` for bash) because the launcher must run
    from the simulation directory to find the snapshot and the Tcl.

    ``os_name`` overrides the platform, in :data:`os.name` spelling; leave it ``None`` in
    production callers.
    """
    with open(original_bat, 'r') as f:
        for line in f:
            if 'xsim' in line:
                xsim_line = line.replace(f'{top_name}.tcl', f'{top_name}_vcd.tcl')
                break
        else:
            raise RuntimeError("No xsim line found in batch file.")

    windows = (os_name or os.name) == "nt"
    with open(new_bat, 'w') as f:
        if windows:
            f.write('cd /d "%~dp0"\n')
        else:
            f.write('#!/usr/bin/env bash\n')
            f.write('cd "$(dirname "$0")" || exit 1\n')
        f.write(xsim_line)

    if not windows:
        os.chmod(new_bat, 0o755)


def run_batch(batch_path, os_name: str | None = None):
    """
    Execute the launcher written by :func:`create_vcd_batch`.

    On Windows the batch file is handed to the shell; on Linux it is run through ``bash``
    explicitly rather than relying on the executable bit, matching
    :func:`waveflow.build.trace_steps.xsi_runner_cmd`.
    """
    if (os_name or os.name) == "nt":
        subprocess.run(batch_path, shell=True, check=True)
    else:
        subprocess.run(["bash", str(batch_path)], check=True)

def copy_vcd(sim_dir, base_dir, component_path, output_vcd):
    src = os.path.join(sim_dir, 'dump.vcd')
    if not os.path.exists(src):
        print("WARNING: dump.vcd not found.")
        return

    vcd_dir = os.path.join(base_dir, 'vcd')
    os.makedirs(vcd_dir, exist_ok=True)

    dst = os.path.join(vcd_dir, output_vcd)
    shutil.copyfile(src, dst)
    print(f"VCD copied to {dst}")


def check_vcd_not_empty(vcd_path, sim_dir=None, trace_level=None):
    """
    Raise unless *vcd_path* declares at least one signal.

    xsim exits 0 after rejecting a ``log_vcd`` command, so a VCD that logged nothing is otherwise
    indistinguishable from a successful run: the file exists, the process succeeded, and the first
    symptom shows up somewhere downstream.  ``$var`` is the VCD keyword that declares a traced
    signal, so looking for one is the cheapest honest check.  Every ``$var`` lives in the header,
    ahead of ``$enddefinitions``, so the scan stops there rather than reading a trace that can run
    to hundreds of megabytes at ``trace_level all``.

    Parameters
    ----------
    vcd_path : str | Path
        The VCD to check.
    sim_dir : str | Path | None
        Simulation directory holding ``xsim.log``.  When given, any command xsim rejected is quoted
        in the error message.
    trace_level : str | None
        Trace level the VCD was requested with, named in the error message.

    Raises
    ------
    RuntimeError
        If the file is missing, or declares no signals.
    """
    vcd_path = Path(vcd_path)
    level = '' if trace_level is None else f' (trace_level={trace_level!r})'

    if not vcd_path.exists():
        raise RuntimeError(f"No VCD was written to {vcd_path}{level}.")

    with vcd_path.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if '$var' in line:
                return
            if '$enddefinitions' in line:
                break

    detail = ''
    if sim_dir is not None:
        log_path = Path(sim_dir) / 'xsim.log'
        if log_path.exists():
            rejected = [
                line.strip()
                for line in log_path.read_text(encoding='utf-8', errors='replace').splitlines()
                if 'ERROR: [Common 17-170]' in line
            ]
            if rejected:
                detail = '  xsim rejected a command: ' + ' '.join(rejected)

    raise RuntimeError(
        f"{vcd_path} declares no signals -- the simulation logged nothing{level}. "
        f"Check the xsim log for a rejected log_vcd command.{detail}"
    )

def parse_args():
    parser = argparse.ArgumentParser(description="Process VCD dump options.")

    parser.add_argument(
        "--comp",
        type=str,
        default="hls_component",
        help="Component name (default: hls_component)"
    )
    parser.add_argument(
        "--top",
        type=str,
        required=True,
        help="Top-level function name (required)"
    )
    parser.add_argument(
        "--out",
        type=str,
        default="dump.vcd",
        help="Output VCD filename (default: dump.vcd)"
    )
    parser.add_argument(
        "--soln",
        type=str,
            default="solution1",
        help="Solution name (default: solution1)"
    )
    parser.add_argument(
        "--trace_level",
        type=str,
        default="*",
        help="VCD trace level (default: *)"
    )
    return parser.parse_args()


def run_xsim_vcd(
    top: str,
    comp: str = "hls_component",
    out: str = "dump.vcd",
    soln: str | None = "solution1",
    trace_level: str = "*",
    workdir: str | Path | None = None,
) -> Path:
    """
    Generate a VCD file by re-running a Vivado HLS RTL simulation.

    This function performs the same steps as the CLI entry point but is
    callable from Python.  It modifies the simulation TCL and batch files
    to enable VCD logging, runs the simulation, and copies the resulting
    ``dump.vcd`` to the output location.

    Parameters
    ----------
    top : str
        Name of the top-level function to simulate (required).
    comp : str
        Name of the HLS component directory.  Default: ``'hls_component'``.
    out : str
        Output VCD filename (written inside a ``vcd/`` subdirectory of
        *workdir*).  Default: ``'dump.vcd'``.
    soln : str | None
        Solution name inside the component directory.  When ``None`` the
        single sub-directory of *comp* is used automatically.  Default:
        ``'solution1'``.
    trace_level : str
        VCD trace level.  Built-in values ``'*'``, ``'all'``, and ``'port'``
        reuse the trace selection recorded in the generated simulation TCL.
        You can also pass a specific hierarchical path for a custom
        ``log_vcd`` target.
    workdir : str | Path | None
        Working directory that contains the *comp* component folder.
        Defaults to the current working directory.

    Returns
    -------
    Path
        Absolute path to the written VCD file.

    Raises
    ------
    RuntimeError
        If required simulation files are missing, if the simulation process fails, or if the
        simulation ran but logged no signals -- see :func:`check_vcd_not_empty`.
    FileNotFoundError
        If the expected simulation directory does not exist.

    Notes
    -----
    Runs on Windows and Linux.  The launcher Vitis emits beside the RTL differs by platform
    (``run_xsim.bat`` / ``run_xsim.sh``) but is self-contained in both cases — it invokes ``xsim``
    by absolute path, so no toolchain environment needs to be set up first.
    """
    base_dir = str(Path(workdir).resolve()) if workdir is not None else os.getcwd()
    component_name = comp
    top_name = top
    output_vcd = out
    solution_name = soln
    component_path = os.path.join(base_dir, component_name)

    if solution_name is None:
        subdirs = [d for d in os.listdir(component_path) if os.path.isdir(os.path.join(component_path, d))]
        if len(subdirs) == 0:
            raise RuntimeError(
                f"No subdirectories found in {component_path}. Please specify a solution name."
            )
        elif len(subdirs) > 1:
            raise RuntimeError(
                f"Multiple subdirectories found in {component_path}: {subdirs}. "
                "Please specify a solution name via 'soln'."
            )
        else:
            solution_name = subdirs[0]
    soln_path = os.path.join(component_path, solution_name)

    sim_dir_candidates = [
        os.path.join(soln_path, 'hls', 'sim', 'verilog'),
        os.path.join(soln_path, 'sim', 'verilog')
    ]
    sim_dir = None
    for candidate in sim_dir_candidates:
        if os.path.exists(candidate):
            sim_dir = candidate
            break
    if sim_dir is None:
        raise FileNotFoundError(
            f"No valid simulation directory found. Checked: {sim_dir_candidates}"
        )

    launcher, launcher_vcd = launcher_names()
    tcl_path = os.path.join(sim_dir, f'{top_name}.tcl')
    tcl_vcd_path = os.path.join(sim_dir, f'{top_name}_vcd.tcl')
    bat_path = os.path.join(sim_dir, launcher)
    bat_vcd_path = os.path.join(sim_dir, launcher_vcd)

    if not os.path.exists(bat_path):
        raise FileNotFoundError(
            f"No simulation launcher at {bat_path}. Run the RTL co-simulation with trace capture "
            f"enabled (e.g. trace_level all) before generating a VCD."
        )

    modify_tcl(tcl_path, tcl_vcd_path, trace_level)
    create_vcd_batch(top_name, bat_path, bat_vcd_path)
    run_batch(bat_vcd_path)
    copy_vcd(sim_dir, base_dir, component_path, output_vcd)

    vcd_dir = os.path.join(base_dir, 'vcd')
    vcd_out = Path(os.path.join(vcd_dir, output_vcd)).resolve()
    check_vcd_not_empty(vcd_out, sim_dir=sim_dir, trace_level=trace_level)
    return vcd_out


def main():
    """
    CLI entry point.  Thin wrapper over :func:`run_xsim_vcd` so both paths share one flow -- in
    particular the empty-VCD check, which the CLI used to skip.
    """
    args = parse_args()

    try:
        out_path = run_xsim_vcd(
            top=args.top,
            comp=args.comp,
            out=args.out,
            soln=args.soln,
            trace_level=args.trace_level,
        )
    except (RuntimeError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print(f"VCD written to: {out_path}")


if __name__ == "__main__":
    main()
