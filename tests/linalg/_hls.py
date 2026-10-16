"""C-simulation helpers for the linear-algebra tests.

A test writes a ``main()`` testbench against generated headers; :func:`run_csim` builds a Vitis
HLS project around it and runs C-simulation.  Part and clock are arguments: the framework itself
holds no part or clock constant.  Nothing here imports from ``examples/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.toolchain import toolchain

#: The part these tests use; any installed part serves C-simulation.
PART = "xczu48dr-ffvg1517-2-e"
PERIOD_NS = 4

_TCL = """\
open_project -reset csim_proj
set_top main
add_files -tb tb.cpp -cflags "-std=c++14 -I{include}"
open_solution -reset solution1
set_part {{{part}}}
create_clock -period {period}
if {{[catch {{csim_design -argv "{out}"}} res]}} {{
    puts "WAVEFLOW_ERROR: C-simulation failed."
    puts $res
    exit 1
}}
puts "WAVEFLOW_SUCCESS"
exit 0
"""


def require_vitis() -> None:
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis HLS not found; the C-simulation tests need it")


def run_csim(
    work_dir: Path,
    tb_cpp: str,
    include_dir: Path,
    part: str = PART,
    period_ns: float = PERIOD_NS,
) -> str:
    """Write ``tb.cpp`` into ``work_dir``, C-simulate it, and return what it wrote to ``out.txt``
    (its path is ``argv[1]``).  Raises with the log on a failure."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "tb.cpp").write_text(tb_cpp, encoding="utf-8")
    out = work_dir / "out.txt"
    tcl = work_dir / "run.tcl"
    tcl.write_text(
        _TCL.format(
            include=Path(include_dir).resolve().as_posix(),
            part=part,
            period=period_ns,
            out=out.resolve().as_posix(),
        ),
        encoding="utf-8",
    )
    run = toolchain.run_vitis_hls(tcl, work_dir=work_dir, capture_output=True)
    log = (run.stdout or "") + (run.stderr or "")
    (work_dir / "csim.log").write_text(log, encoding="utf-8")
    if run.returncode != 0 or "WAVEFLOW_SUCCESS" not in log or not out.is_file():
        raise RuntimeError(f"C-simulation failed in {work_dir}:\n{log[-3000:]}")
    return out.read_text(encoding="utf-8")
