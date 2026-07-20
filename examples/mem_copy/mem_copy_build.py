"""mem_copy_build.py — the introspectable build DAG for the mem_copy example.

Mirrors the other examples' ``*_build.py`` (regmap, block_scale, fir): a :class:`BuildDag` driven by
:func:`run_dag_cli`, so the example runs through the standard CLI with ``--through`` / ``--list-steps``
/ ``--status`` rather than a bespoke ``__main__``.

Steps, in the order the flow teaches them:

    pysim       -> run the MemCopyTB graph in SimPy, check every copy bit-exact, record timing.
                   The fast, no-toolchain checkpoint -- this is the default ``--through`` target.
    codegen_dut -> lower the MemCopy DUT graph to the ap_ctrl_none composite top (+ tcl, ports, headers).
    codegen_tb  -> lower the MemCopyTB graph to the XSI BFM harness + main + scenario constants.
    csynth      -> Vitis HLS C-synthesis of the generated top (needs Vitis; produces the RTL the
                   ``-m xsi`` gate drives).

The RTL rung itself (drive the synthesized top through XSI, exact cycle count) is the ``-m xsi`` gate
in ``tests/examples/test_xsi_bfm.py`` -- it needs Vivado xsim + a prior csynth and is orchestrated
there rather than duplicated here.

    python mem_copy_build.py --through pysim        # default; the golden + timing
    python mem_copy_build.py --list-steps
    python mem_copy_build.py --through codegen_dut  # the DUT top
    python mem_copy_build.py --through codegen_tb   # + the XSI harness
    python mem_copy_build.py --through csynth       # needs Vitis
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.build.trace_steps import (
    AddVcdTopStep,
    ExtractBurstsStep,
    RtlSimStep,
    TraceManifestStep,
)
from waveflow.toolchain import toolchain

try:
    from examples.mem_copy.mem_copy import (DEFAULT_MEM_DW, XSI_JOBS, generate_dut, generate_tb,
                                            write_mem_copy_xsi_bundles)
    from examples.mem_copy.mem_copy_sim import MemCopySim
except ModuleNotFoundError:  # run as a script from the example directory
    from mem_copy import (DEFAULT_MEM_DW, XSI_JOBS, generate_dut,  # type: ignore[no-redef]
                          generate_tb, write_mem_copy_xsi_bundles)
    from mem_copy_sim import MemCopySim  # type: ignore[no-redef]

try:
    from examples.mem_copy.mem_copy_figures import SyncDocsFiguresStep, TimingFiguresStep
except ModuleNotFoundError:  # run as a script from the example directory
    from mem_copy_figures import (SyncDocsFiguresStep,  # type: ignore[no-redef]
                                  TimingFiguresStep)

HERE = Path(__file__).resolve().parent


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the MemCopyTB graph in SimPy, check every copy bit-exact, and record timing.

    The **first checkpoint**: no toolchain, seconds not minutes, and the place most functional
    mistakes surface before any C++ exists.  Runs the canonical 16-job scenario (``XSI_JOBS`` — the
    same one the RTL gate drives), so the timing it records is directly comparable to the 2908-cycle
    RTL number.  Fails the build on any mismatch, so "it ran" is never mistaken for "it is correct".
    """

    description = "Run the MemCopyTB pysim golden and record correctness + timing."
    consumes = ["mem_copy_source"]
    produces = {"pysim_results": Path("results/pysim.json")}
    params = {"mem_dwidth": DEFAULT_MEM_DW}

    def run(self, config: BuildConfig, mem_dwidth, **_) -> dict:
        w = int(mem_dwidth)
        bpw = w // 8
        msim = MemCopySim(jobs=XSI_JOBS, mem_dwidth=w)
        tb = msim.tb
        # Materialize the command bundle under the build root; the driver loads it in pre_sim.
        msim.write_scenario(config.root_dir)
        tb.sim.run_sim()

        # Correctness: each destination region equals the source pattern the arena was seeded with.
        fails = []
        for j, (job, exp) in enumerate(zip(tb._jobs, msim.expected)):
            got = tb.mem._mem.read(job.dst_off * bpw, job.n_words)
            if not (got == exp).all():
                fails.append(j)
        ndone = len(tb.done_sink.words)
        if fails or ndone != len(tb._jobs):
            raise RuntimeError(
                f"pysim golden failed: mismatched jobs {fails}, done_tokens {ndone}/{len(tb._jobs)}")

        cycles = tb.sim.env.now * tb.clk.freq
        out = config.root_dir / "results" / "pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "jobs": len(tb._jobs),
            "n_words": tb._jobs[0].n_words,
            "end_cycles": round(cycles),
            "done_tokens": ndone,
            "clk_freq": float(tb.clk.freq),
            "note": "end-of-simulation, not time-to-last-completion; cf. the RTL 2908 (see pysim.md)",
        }, indent=2), encoding="utf-8")
        print(f"[pysim] {len(tb._jobs)} jobs, all bit-exact, {ndone} done tokens, "
              f"end={round(cycles)} cycles")
        return {"pysim_results": out}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    """Lower the **DUT** ``MemCopy`` graph to an ``ap_ctrl_none`` ``hls::task`` top: the composite top
    .cpp, its csynth .tcl, the port map, and the headers.

    Every artifact is derived from the ``MemCopy`` graph (``mem_copy.py::generate_dut``); nothing here
    is hand-written C++.
    """

    description = "Lower the MemCopy graph to the ap_ctrl_none composite top (+ tcl, ports, headers)."
    consumes = ["mem_copy_source"]
    produces = {"mem_copy_cpp": Path("gen/mem_copy.cpp"), "run_tcl": Path("mem_copy.tcl"),
                "dut_ports": Path("xsi/mem_copy_ports.h")}
    params = {"mem_dwidth": DEFAULT_MEM_DW}

    def run(self, config: BuildConfig, mem_dwidth, **_) -> dict:
        generate_dut(out_dir=config.root_dir, width=int(mem_dwidth))
        return {
            "mem_copy_cpp": config.root_dir / "gen" / "mem_copy.cpp",
            "run_tcl": config.root_dir / "mem_copy.tcl",
            "dut_ports": config.root_dir / "xsi" / "mem_copy_ports.h",
        }


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    """Lower the **testbench** ``MemCopyTB`` graph to the XSI **harness**: the BFM harness, the two-line
    main, and the scenario constants.

    Derived from the ``MemCopyTB`` graph (``mem_copy.py::generate_tb``); the harness ``#include``s the
    DUT's ``xsi/mem_copy_ports.h``, so this runs after :class:`CodegenDutStep`.
    """

    description = "Lower the MemCopyTB graph to the XSI BFM harness + main + scenario constants."
    consumes = ["mem_copy_source", "dut_ports"]
    produces = {"tb_harness": Path("xsi/mem_copy_tb_harness.h"),
                "tb_main": Path("xsi/mem_copy_bfm_tb.cpp")}
    params = {"mem_dwidth": DEFAULT_MEM_DW}

    def run(self, config: BuildConfig, mem_dwidth, **_) -> dict:
        generate_tb(out_dir=config.root_dir, width=int(mem_dwidth))
        return {
            "tb_harness": config.root_dir / "xsi" / "mem_copy_tb_harness.h",
            "tb_main": config.root_dir / "xsi" / "mem_copy_bfm_tb.cpp",
        }


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis of the generated top — produces the RTL the ``-m xsi`` gate drives.

    Toolchain-gated (needs Vitis).  The RTL rung itself (XSI, exact cycle count) is the ``-m xsi``
    gate in ``tests/examples/test_xsi_bfm.py``; it needs this step to have run first.
    """

    description = "Run Vitis HLS C-synthesis of the generated composite top."
    consumes = ["mem_copy_cpp", "run_tcl"]
    produces = {"report_dir": Path("mem_copy_proj/solution1")}
    params = {"live_output": False}

    def run(self, config: BuildConfig, live_output, **_) -> dict:
        try:
            result = toolchain.run_vitis_hls(
                config.root_dir / "mem_copy.tcl",
                work_dir=config.root_dir,
                capture_output=not live_output,
            )
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr)
        except Exception as exc:
            raise RuntimeError(str(exc))
        return {"report_dir": config.root_dir / "mem_copy_proj" / "solution1"}


def build_mem_copy_dag() -> BuildDag:
    from examples.mem_copy.mem_copy import MemCopy

    dag = BuildDag()
    dag.add(SourceStep(artifact="mem_copy_source", path=HERE / "mem_copy.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    # The trace pair.  Both are pure elaborate() -- no RTL input, no Vitis -- so they sit beside the
    # codegen steps rather than downstream of csynth: the RTL net names are known before anything is
    # synthesized, which is the whole premise.  `vcd_dumper` is what `run.bat <top> <tb> trace`
    # elaborates as a second top; the manifest names what lands in the resulting waveform.
    dag.add(AddVcdTopStep(name="vcd_dumper", comp_class=MemCopy,
                          source_artifact="mem_copy_source", output_dir="xsi"))
    dag.add(TraceManifestStep(name="trace_manifest", comp_class=MemCopy,
                              source_artifact="mem_copy_source",
                              output_path="results/mem_copy_trace.json"))
    dag.add(CSynthStep(name="csynth"))
    # The timing rung.  `rtlsim` runs but never asserts -- the exact cycle count stays with the
    # `-m xsi` gate; this one exists to produce a waveform.  `extract_bursts` then turns the
    # waveform plus the manifest into a per-firing timing table.
    dag.add(RtlSimStep(name="rtlsim", top="mem_copy", tb="mem_copy_bfm_tb", xsi_dir="xsi",
                       prepare=write_mem_copy_xsi_bundles))
    dag.add(ExtractBurstsStep(name="extract_bursts",
                              output_path="results/mem_copy_timing.json"))
    # Figures are on-demand: `--through timing_figures` renders into results/ (gitignored), and
    # `--through sync_docs_figures` promotes them into docs/ as committed assets, so a docs figure
    # only changes when you mean it to and the change is a reviewable diff.
    dag.add(TimingFiguresStep(name="timing_figures"))
    dag.add(SyncDocsFiguresStep(name="sync_docs_figures"))
    return dag


def main() -> None:
    run_dag_cli(
        build_mem_copy_dag,
        description="Build and check the mem_copy example.",
        default_through="pysim",
        root_dir=HERE,
        extra_args=[
            (("--mem-dwidth",), {"type": int, "default": DEFAULT_MEM_DW,
                                 "help": "Memory data width in bits (default 64)."}),
            (("--live-output",), {"action": "store_true",
                                  "help": "Stream Vitis output live (csynth)."}),
        ],
        params_from_args=lambda a: {"mem_dwidth": a.mem_dwidth, "live_output": a.live_output},
    )


if __name__ == "__main__":
    main()
