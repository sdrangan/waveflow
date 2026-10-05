"""vitis_fft_build.py — generate, synthesize and lay out the XSI workspace for ``VitisFft``.

    python -m examples.vitis_fft.vitis_fft_build             # generate, then csynth
    python -m examples.vitis_fft.vitis_fft_build --no-synth  # generate only (Python, seconds)

Nothing here is C++ someone maintains.  The task body is the framework's
``waveflow/build/vitis_fft_task.h``, copied into ``include/`` by ``VitisL1Step``; the vendor FFT stays
in the Vitis install and is reached by an include path; the ``ap_ctrl_none`` top comes from
``composite_top_spec`` on the module itself; the XSI harness comes from ``tb_top_spec`` on the
testbench graph in ``vitis_fft.py``.

``include/``, ``gen/`` (with the ``.tcl``) and ``xsi/`` are build output, untracked: delete them and
:func:`generate` writes them back (``plans/source_layout.md``).  This example has no ``src/``,
because it has no hand-written C++ of its own.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.composite_gen import (
    GEN_DIR,
    INCLUDE_DIR,
    composite_top_spec,
    render_ports_h,
    render_rtl_f,
    render_tb_harness,
    render_tb_main,
    render_tcl,
    render_top,
    tb_top_spec,
    tcl_path,
)
from waveflow.build.streamutils import MemMgrStep, XsiHarnessStep
from waveflow.build.vitis_l1_step import VitisL1Step, vitis_fft_include_dir
from waveflow.simulation.simulation import Simulation

from examples.vitis_fft.vitis_fft import VitisFftTB, write_scenario

HERE = Path(__file__).resolve().parent
TOP = "vitis_fft"
XSI_DIR = "xsi"
TB = f"{TOP}_bfm_tb"


def make_tb() -> VitisFftTB:
    """The graph both the DUT top and the XSI harness are derived from."""
    return VitisFftTB(name="tb", sim=Simulation())


def generate(root: Path = HERE) -> str:
    """Everything before csynth: headers, the top and its ``.tcl``, the XSI workspace and the
    scenario.  Python only, no toolchain beyond locating the vendor headers.

    The XSI gate calls this before asking whether the RTL is stale, so a pulled change to the body,
    a generator or the framework shows up as a changed source (``trace_steps.rtl_staleness``).
    """
    dag = BuildDag()
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))       # the generated top includes memmgr.hpp
    dag.add(VitisL1Step(output_dir=INCLUDE_DIR))      # the body, copied verbatim
    dag.add(XsiHarnessStep(output_dir=XSI_DIR))       # framework BFMs + loader + run scripts
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")

    tb = make_tb()
    dut = tb.dut
    # The output word is wider than the input word (the FFT's output format grows with log2 L), so
    # each port takes its own endpoint's width rather than one design width.
    widths = {f"s_in_{j}": ep.bitwidth for j, ep in enumerate(dut.s_in)}
    widths |= {f"m_out_{j}": ep.bitwidth for j, ep in enumerate(dut.m_out)}
    spec = composite_top_spec(dut, width=int(dut.s_in[0].bitwidth), port_widths=widths)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    tcl_path(root, spec.top_name).write_text(
        render_tcl(spec.top_name, include_dirs=(str(vitis_fft_include_dir()),)), encoding="utf-8")

    xsi = root / XSI_DIR
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    tb_spec = tb_top_spec(tb)
    (xsi / f"{TOP}_tb_harness.h").write_text(render_tb_harness(tb_spec), encoding="utf-8")
    (xsi / f"{TB}.cpp").write_text(render_tb_main(tb_spec, int(tb.n_cycles)), encoding="utf-8")
    write_scenario(xsi)
    return spec.top_name


def synth(root: Path = HERE) -> str:
    """csynth the top, stamp the sources it was built from, and list its RTL for ``xvlog``."""
    from waveflow.build.rtl_digest import write_stamp
    from waveflow.toolchain.toolchain import run_vitis_hls

    r = run_vitis_hls(tcl_path(root, TOP), work_dir=root)
    out = (r.stdout or "") + (r.stderr or "")
    if "WAVEFLOW_CSYNTH_OK" not in out:
        raise RuntimeError(f"csynth of {TOP} failed:\n{out[-6000:]}")
    write_stamp(root, TOP)
    (root / XSI_DIR / f"rtl_{TOP}.f").write_text(render_rtl_f(TOP, root), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-synth", action="store_true")
    a = ap.parse_args()
    top = generate()
    print(f"generated {GEN_DIR}/{top}.cpp, {GEN_DIR}/{top}.tcl and {XSI_DIR}/")
    if a.no_synth:
        return
    synth()
    print(f"csynth {top} OK")


if __name__ == "__main__":
    main()
