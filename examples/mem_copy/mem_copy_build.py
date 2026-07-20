"""mem_copy_build.py — the introspectable build DAG for the mem_copy example.

Mirrors the other examples' ``*_build.py`` (regmap, block_scale, fir): a :class:`BuildDag` driven by
:func:`run_dag_cli`, so the example runs through the standard CLI with ``--through`` / ``--list-steps``
/ ``--status`` rather than a bespoke ``__main__``.

Steps, in the order the flow teaches them:

    pysim   -> run the MemCopyTB graph in SimPy, check every copy bit-exact, record timing.
               The fast, no-toolchain checkpoint -- this is the default ``--through`` target.
    gen     -> generate the ap_ctrl_none composite top + the XSI harness/main + headers.
    csynth  -> Vitis HLS C-synthesis of the generated top (needs Vitis; produces the RTL the
               ``-m xsi`` gate drives).

The RTL rung itself (drive the synthesized top through XSI, exact cycle count) is the ``-m xsi`` gate
in ``tests/examples/test_xsi_bfm.py`` -- it needs Vivado xsim + a prior csynth and is orchestrated
there rather than duplicated here.

    python mem_copy_build.py --through pysim      # default; the golden + timing
    python mem_copy_build.py --list-steps
    python mem_copy_build.py --through csynth      # needs Vitis
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.toolchain import toolchain

try:
    from examples.mem_copy.mem_copy import DEFAULT_MEM_DW, XSI_JOBS, generate
    from examples.mem_copy.mem_copy_sim import MemCopySim
except ModuleNotFoundError:  # run as a script from the example directory
    from mem_copy import DEFAULT_MEM_DW, XSI_JOBS, generate  # type: ignore[no-redef]
    from mem_copy_sim import MemCopySim  # type: ignore[no-redef]

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
class GenStep(BuildStep):
    """Generate the ap_ctrl_none composite top, the XSI harness + two-line main, and the headers.

    Every artifact is derived from the ``MemCopy`` / ``MemCopyTB`` graph (see
    ``examples/mem_copy/mem_copy.py::generate``); nothing here is hand-written C++.
    """

    description = "Generate the composite top, XSI harness/main, and headers."
    consumes = ["mem_copy_source"]
    produces = {"mem_copy_cpp": Path("gen/mem_copy.cpp"), "run_tcl": Path("mem_copy.tcl")}
    params = {"mem_dwidth": DEFAULT_MEM_DW}

    def run(self, config: BuildConfig, mem_dwidth, **_) -> dict:
        generate(out_dir=config.root_dir, width=int(mem_dwidth))
        return {
            "mem_copy_cpp": config.root_dir / "gen" / "mem_copy.cpp",
            "run_tcl": config.root_dir / "mem_copy.tcl",
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
    dag = BuildDag()
    dag.add(SourceStep(artifact="mem_copy_source", path=HERE / "mem_copy.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(GenStep(name="gen"))
    dag.add(CSynthStep(name="csynth"))
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
