"""bram_access_build.py — build the shared-memory example: pysim -> codegen -> csynth.

``plans/bram_access.md``.  The rungs, in the order a failure is cheapest to diagnose:

    pysim       -> the graph runs in SimPy: the witness's five values, and the two refusals
    codegen_dut -> the ap_ctrl_none top (with its two `mode=bram` ports), its schema headers and
                   its tcl
    csynth      -> Vitis HLS (needs the toolchain); stamps the sources the RTL was built from

The RTL rung — the same scenario through real Verilog, plus the deliberate collision — is the
**system flow** (``bram_access_system_build.py``): the system top instantiates the kernel as its
wrapper (the kernel joined to its hand-written ``bram_t2p`` memory), and the ``-m xsi`` gate is
``tests/examples/test_bram_access_xsi.py``.  ``rtl_trace`` (on demand) is that system run, traced,
and the two figure steps render from its waveform.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

HERE = Path(__file__).resolve().parent

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep  # noqa: E402
from waveflow.build.composite_gen import (  # noqa: E402
    GEN_DIR,
    composite_top_spec,
    render_tcl,
    render_top,
)
from waveflow.build.elaborate import elaborate  # noqa: E402
from waveflow.build.rtl_digest import write_stamp  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.bram_access.bram_access import (  # noqa: E402
    DEPTH,
    SCHEMA_CLASSES,
    WORD_BW,
    BramAccess,
)
from examples.bram_access.bram_access_figures import (  # noqa: E402
    ActivityFiguresStep,
    SyncDocsFiguresStep,
)

#: The generated kernel's name.  What a simulator elaborates is the system top, which instantiates the
#: kernel as its wrapper (``wrapper_gen``): the kernel joined to its hand-written ``bram_t2p``.
TOP = "bram_access"
INCLUDE_DIR = "include"

#: Hand-written ``hls::task`` bodies (and the range check both share) copied verbatim from ``src/``
#: — the example-local twin of ``MemStreamStep``.  Kept OUT of ``include/`` in the source tree so
#: nothing there is half-generated: everything in ``include/`` is a build product.
#:
#: The **message layouts are not here**.  Those are generated from the Python schemas by
#: :class:`~waveflow.hw.dataschema.DataSchemaStep` (see :data:`SCHEMA_CLASSES`), which is what lets
#: the task bodies say ``c.read_stream<W>(cmd)`` instead of restating the field order.
FIXED_TASK_BODIES = ("bram_cmd_range.h", "bram_write_compute_task.h", "bram_read_cmd_task.h")

#: The gated geometry, stated rather than defaulted — **64-bit words**, so the byte/word address
#: convention is exercised rather than assumed (see ``bram_access.WORD_BW``).  The recorded XSI cycle
#: count belongs to exactly this configuration.
_ELAB = {"bitwidth": WORD_BW, "depth": DEPTH}

#: A small Zynq part, because nothing about this example is device-specific: one RAMB18 pair, two
#: tasks and six streams fit anywhere.  The example is about a structure, not a platform.
PART = "xc7z020clg484-1"
PERIOD_NS = 10


def copy_fixed_task_bodies(root_dir: Path) -> None:
    import shutil

    dst = Path(root_dir) / INCLUDE_DIR
    dst.mkdir(parents=True, exist_ok=True)
    for name in FIXED_TASK_BODIES:
        src = HERE / "src" / name
        if not src.is_file():
            raise FileNotFoundError(f"fixed task body missing: {src}")
        shutil.copyfile(src, dst / name)


def generate_dut(out_dir: Path = HERE) -> Path:
    """Generate the schema headers, the top and its tcl -- what csynth compiles.

    The wrapper joining the kernel to its memory is not emitted here: the system flow renders it
    into the system top, from the *same* ``TopSpec`` that emits the kernel's interface pragmas, so
    the two cannot disagree about the kernel's port list (see :func:`wrapper_text`).
    """
    config = BuildConfig(root_dir=out_dir, params={})
    copy_fixed_task_bodies(out_dir)

    inner = BuildDag()
    # Framework headers the generated top includes unconditionally.  This design uses neither a
    # stream utility nor the memory manager, but the include is emitted for every free-running top
    # and a missing file is a csynth error, not a warning.
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    # The four messages' C++ headers, from the same Python declarations pysim reads through.  One
    # author for every field layout on this design's boundary; `BramStatusField` is listed in its own
    # right so the status reaches the kernel as a real `enum class`.
    for cls in SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=[WORD_BW], include_dir=INCLUDE_DIR))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    comp = elaborate(BramAccess, dict(_ELAB), name=TOP)
    spec = composite_top_spec(comp, width=WORD_BW)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, part=PART, period_ns=PERIOD_NS), encoding="utf-8")
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl")
    return cpp


def wrapper_text() -> str:
    """The wrapper Verilog for the gated configuration — for tests that want it without a build."""
    from waveflow.build.wrapper_gen import render_wrapper, wrapper_spec

    comp = elaborate(BramAccess, dict(_ELAB), name=TOP)
    return render_wrapper(wrapper_spec(comp, composite_top_spec(comp, width=WORD_BW)))


# ---------------------------------------------------------------------------
# The build DAG
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the graph in SimPy and check the witness's five values plus the two refusals.

    The first checkpoint and the one that needs no toolchain: if the design does not read back a ramp
    in Python, nothing downstream is worth running.
    """

    description = "Run the BramAccessTB pysim golden (the witness's five values + two refusals)."
    consumes = ["bram_access_source"]
    produces: ClassVar[dict] = {"pysim_results": Path("results/bram_access_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        from examples.bram_access.bram_access import (
            captured,
            check_outputs,
            run_pysim,
            scenario_zero,
        )

        sc = scenario_zero()
        tb = run_pysim(sc=sc)
        resp_w, data_r, resp_r = captured(tb)
        check_outputs(resp_w, data_r, resp_r, sc, where="pysim: ")
        out = config.root_dir / "results" / "bram_access_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "bitwidth": WORD_BW, "depth": DEPTH,
            "write_cmds": len(sc.cmd_w), "read_cmds": len(sc.cmd_r),
            "resp_w": [int(v) for v in resp_w], "resp_r": [int(v) for v in resp_r],
            "data_r": [int(v) for v in data_r],
            "first_data_cycle": int(tb.data_r_snk.cycles[0]),
            "last_data_cycle": int(tb.data_r_snk.cycles[-1])}, indent=2), encoding="utf-8")
        return {"pysim_results": out}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower BramAccess to its ap_ctrl_none top, its schema headers and its tcl."
    consumes = ["bram_access_source"]
    produces: ClassVar[dict] = {"bram_access_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"bram_access_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis of the kernel — and the source stamp the system flow checks.

    The stamp (:func:`~waveflow.build.rtl_digest.write_stamp`) records the sources this RTL was built
    from, so the system run's ``synth="check"`` accepts it, and refuses it once a source changes.
    """

    description = "Run Vitis HLS C-synthesis of the generated top."
    consumes = ["bram_access_cpp", "run_tcl"]
    produces: ClassVar[dict] = {"report_dir": Path(f"{TOP}_proj/solution1")}
    params: ClassVar[dict] = {"live_output": False}

    def run(self, config: BuildConfig, live_output, **_) -> dict:
        result = toolchain.run_vitis_hls(config.root_dir / f"{TOP}.tcl",
                                         work_dir=config.root_dir,
                                         capture_output=not live_output)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        write_stamp(config.root_dir, TOP)
        return {"report_dir": config.root_dir / f"{TOP}_proj" / "solution1"}


@dataclass(kw_only=True)
class SystemTraceStep(BuildStep):
    """Scenario zero through the **system** flow, traced: the waveform the figures read.

    Runs ``bram_access_system_build.build_dag("zero")`` through ``system_xsi`` with
    ``synth="check"`` (the csynth upstream stamped the RTL).  The run writes
    ``<work>/<top>_trace.vcd`` (the wrapper's scope inside the system top) and
    ``<work>/hazards.json`` (the wrapper's hazard manifest, carrying that scope), which
    :class:`~examples.bram_access.bram_access_figures.ActivityFiguresStep` reads.

    Never fresh, like the system steps it runs: the run reads Python and C++ this DAG cannot see,
    and a waveform left behind by the collision gate must not stand in for scenario zero's.
    """

    description = "Run scenario zero through the system flow, traced (VCD + hazard manifest)."
    consumes = ["report_dir"]
    produces: ClassVar[dict] = {"trace_vcd": None}
    params: ClassVar[dict] = {}

    def is_fresh(self, config: BuildConfig, paths: dict) -> bool | None:
        return False

    def run(self, config: BuildConfig, **_) -> dict:
        from examples.bram_access import bram_access_system_build as sysb

        root = Path(config.root_dir)
        res = sysb.build_dag("zero").run(BuildConfig(root_dir=root, params={"synth": "check"}),
                                         through="system_xsi")
        bad = {n: r.message for n, r in res.items() if not r.success}
        if bad:
            raise RuntimeError(f"the traced {sysb.TOP} run failed: {bad}")
        vcd, _manifest = sysb.trace_of(root / sysb.WORK_DIR / sysb.TOP)
        return {"trace_vcd": vcd}


def build_bram_access_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="bram_access_source", path=HERE / "bram_access.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CSynthStep(name="csynth"))
    # The trace rung, and the two figures that read it.  All on demand: `--through rtl_trace` runs
    # scenario zero through the system flow, traced; `--through activity_figures` renders into
    # results/ (gitignored); `--through sync_docs_figures` promotes them into docs/ as committed
    # assets — so a docs figure only changes when you mean it to.
    dag.add(SystemTraceStep(name="rtl_trace"))
    dag.add(ActivityFiguresStep(name="activity_figures"))
    dag.add(SyncDocsFiguresStep(name="sync_docs_figures"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_bram_access_dag,
                description="Build the bram_access design: pysim -> codegen -> csynth.",
                default_through="csynth",
                root_dir=HERE)
