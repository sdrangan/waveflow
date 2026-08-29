"""bram_access_build.py — build the shared-memory example: pysim -> codegen -> csynth.

``plans/bram_access.md``.  The rungs, in the order a failure is cheapest to diagnose:

    pysim       -> the graph runs in SimPy: the witness's five values, and the two refusals
    codegen_dut -> the ap_ctrl_none top (with its two `mode=bram` ports), its tcl, its port map,
                   the memory placed beside it, and the WRAPPER that joins them
    codegen_tb  -> the XSI harness + main + scenario bundles
    csynth      -> Vitis HLS (needs the toolchain); re-emits rtl_<wrapper>.f from the RTL on disk

The RTL rung — the same scenario through real Verilog, plus the deliberate collision — is the
``-m xsi`` gate in ``tests/examples/test_bram_access_xsi.py``.

**What a simulator elaborates is not the kernel.**  ``bram_access_top.v`` instantiates ``bram_access``
plus its hand-written ``bram_t2p`` memory, so the ``.f``, the snapshot and the shared library are
named for the wrapper — while csynth's project, its report and its generated Verilog keep the
kernel's name.  One artifact keeps the name it has; the new one is visibly the outer layer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

HERE = Path(__file__).resolve().parent

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep  # noqa: E402
from waveflow.build.composite_gen import (  # noqa: E402
    GEN_DIR,
    composite_top_spec,
    render_ports_h,
    render_rtl_f,
    render_tb_harness,
    render_tb_main,
    render_tcl,
    render_top,
    tb_top_spec,
)
from waveflow.build.elaborate import elaborate  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.build.wrapper_gen import bram_hazard_manifest  # noqa: E402
from waveflow.build.rtl_steps import GenRtlStep, GenWrapperStep  # noqa: E402
from waveflow.build.trace_steps import AddVcdTopStep, RtlSimStep  # noqa: E402
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep, XsiHarnessStep  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.bram_access.bram_access import (  # noqa: E402
    DEPTH,
    SCHEMA_CLASSES,
    WORD_BW,
    XSI_N_CYCLES,
    BramAccess,
    BramAccessTB,
    Scenario,
    scenario_zero,
    write_scenario,
)
from examples.bram_access.bram_access_figures import (  # noqa: E402
    ActivityFiguresStep,
    SyncDocsFiguresStep,
)

#: The generated kernel's name, and the wrapper's.  The wrapper is what a simulator elaborates.
TOP = "bram_access"
WRAPPER = f"{TOP}_top"
INCLUDE_DIR = "include"

#: Hand-written ``hls::task`` bodies (and the range check both share) copied verbatim from ``src/``
#: — the example-local twin of ``MemStreamStep``.  Kept OUT of ``include/`` in the source tree so
#: nothing there is half-generated: everything in ``include/`` is a build product.
#:
#: The **message layouts are not here**.  Those are generated from the Python schemas by
#: :class:`~waveflow.hw.dataschema.DataSchemaStep` (see :data:`SCHEMA_CLASSES`), which is what lets
#: the task bodies say ``c.read_stream<W>(cmd)`` instead of restating the field order.
FIXED_TASK_BODIES = ("bram_cmd_range.h", "bram_write_compute_task.h", "bram_read_cmd_task.h")

#: The RTL that must land in ``xsi/`` beside the ``.f`` naming it, in elaboration reading order: the
#: memory, then the wrapper that instantiates it.
RTL_FILES = ("bram_t2p.v", f"{WRAPPER}.v")

#: The gated geometry, stated rather than defaulted — **64-bit words**, so the byte/word address
#: convention is exercised rather than assumed (see ``bram_access.WORD_BW``).  The recorded XSI cycle
#: count belongs to exactly this configuration.
_ELAB = {"bitwidth": WORD_BW, "depth": DEPTH}

#: A small Zynq part, because nothing about this example is device-specific: one RAMB18 pair, two
#: tasks and six streams fit anywhere.  The example is about a structure, not a platform.
PART = "xc7z020clg484-1"
PERIOD_NS = 10

#: Where the hazard manifest lands — the nets a read-during-write collision is visible on.  Emitted
#: beside the wrapper because it names the **wrapper's** wires, and read back by
#: :func:`waveflow.utils.bram_hazard.find_read_during_write` after a traced run.
HAZARD_JSON = f"xsi/{TOP}_hazard.json"


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
    """Generate the top, its tcl, the XSI port map, the memory, and the wrapper.

    Note the order of dependence: the wrapper is derived from the *same* ``TopSpec`` that emits the
    kernel's interface pragmas, so the two cannot disagree about the kernel's port list — which for a
    ``bram`` port means fourteen signal names nobody typed twice.
    """
    config = BuildConfig(root_dir=out_dir, params={})
    copy_fixed_task_bodies(out_dir)

    inner = BuildDag()
    # The dumper step consumes the design source (it is derived from the class), so the inner DAG
    # needs the same source node the outer one has.
    inner.add(SourceStep(artifact="bram_access_source", path=HERE / "bram_access.py"))
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
    inner.add(XsiHarnessStep(output_dir="xsi"))
    # The memory lands in xsi/ rather than rtl/: it is compiled by the same xvlog invocation as the
    # wrapper and the kernel's RTL, and the `.f` names it relative to the directory it lives in.
    inner.add(GenRtlStep(name="place_memory", comp_class=_memory_class(), output_dir="xsi"))
    inner.add(GenWrapperStep(name="wrapper", comp_class=BramAccess, elab_params=dict(_ELAB),
                             width=WORD_BW, output_dir="xsi"))
    # The $dumpvars second top, named for the WRAPPER rather than the kernel: that is what xsim
    # elaborates here, `run.bat` picks `vcd_dumper_%TOP%.v`, and a $dumpvars naming a scope outside
    # this elaboration is a hard error.  Level 1 of the wrapper is exactly the right depth -- the
    # memory's address, enable and write-enable wires are declared in the wrapper's own scope.
    inner.add(AddVcdTopStep(name="vcd_dumper", comp_class=BramAccess,
                            source_artifact="bram_access_source", top=WRAPPER, output_dir="xsi"))
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
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    (out_dir / HAZARD_JSON).write_text(json.dumps(bram_hazard_manifest(comp, spec), indent=2),
                                       encoding="utf-8")
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl + xsi/{spec.top_name}_ports.h "
          f"+ xsi/{WRAPPER}.v + {HAZARD_JSON} (elaborated top: {spec.elab_top})")
    return cpp


def hazard_manifest() -> dict:
    """The nets a read-during-write collision is visible on, for the gated configuration.

    Derived from the same elaborated graph the wrapper is, so the scan cannot be looking at nets the
    wrapper does not drive — which is the failure that would make an empty scan read as "no
    collisions".
    """
    comp = elaborate(BramAccess, dict(_ELAB), name=TOP)
    return bram_hazard_manifest(comp, composite_top_spec(comp, width=WORD_BW))


def _memory_class():
    """The memory class the design instantiates — read off the elaborated graph, not restated.

    A second mention of ``T2pBram`` here would be a place the build could disagree with the design
    about which memory it is placing.
    """
    comp = elaborate(BramAccess, dict(_ELAB))
    mems = {type(m) for m in comp.rtl_mods.values()}
    if len(mems) != 1:
        raise RuntimeError(f"expected exactly one RTL module class in {TOP}, got {mems}")
    return mems.pop()


def make_xsi_tb(n_cycles: int = XSI_N_CYCLES) -> BramAccessTB:
    """The graph the XSI testbench is generated from — the same class the pysim golden runs."""
    return BramAccessTB(name="xsi_tb", sim=Simulation(), n_cycles=int(n_cycles))


def generate_tb(out_dir: Path = HERE, n_cycles: int = XSI_N_CYCLES,
                sc: Scenario | None = None) -> None:
    """Generate the XSI harness + main from the TB graph, and write the scenario bundles.

    ``sc`` is a parameter because the **negative** gate is a different scenario through the same
    binary: the vectors are data the ``AxisMaster``s read at run time, so driving the design into a
    deliberate read-during-write collision needs no second design and no second build.
    """
    tb = make_xsi_tb(n_cycles)
    spec = tb_top_spec(tb)
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{TOP}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{TOP}_bfm_tb.cpp").write_text(render_tb_main(spec, int(n_cycles)), encoding="utf-8")
    written = write_scenario(xsi, sc)
    print(f"generated TB xsi/{TOP}_tb_harness.h + xsi/{TOP}_bfm_tb.cpp "
          f"({written.label or 'scenario'}: {len(written.cmd_w)} write + "
          f"{len(written.cmd_r)} read commands)")


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
    description = "Lower BramAccess to its ap_ctrl_none top + the memory + the wrapper."
    consumes = ["bram_access_source"]
    produces: ClassVar[dict] = {"bram_access_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl"),
                                "dut_ports": Path(f"xsi/{TOP}_ports.h"),
                                "wrapper_v": Path(f"xsi/{WRAPPER}.v"),
                                "memory_v": Path("xsi/bram_t2p.v"),
                                "vcd_dumper": Path(f"xsi/vcd_dumper_{WRAPPER}.v"),
                                "hazard_manifest": Path(HAZARD_JSON)}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"bram_access_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl",
                "dut_ports": root / "xsi" / f"{TOP}_ports.h",
                "wrapper_v": root / "xsi" / f"{WRAPPER}.v",
                "memory_v": root / "xsi" / "bram_t2p.v",
                "vcd_dumper": root / "xsi" / f"vcd_dumper_{WRAPPER}.v",
                "hazard_manifest": root / HAZARD_JSON}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower BramAccessTB to the XSI harness + main + scenario bundles."
    consumes = ["bram_access_source", "dut_ports"]
    produces: ClassVar[dict] = {"tb_harness": Path(f"xsi/{TOP}_tb_harness.h"),
                                "tb_main": Path(f"xsi/{TOP}_bfm_tb.cpp")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_tb(config.root_dir)
        return {"tb_harness": config.root_dir / "xsi" / f"{TOP}_tb_harness.h",
                "tb_main": config.root_dir / "xsi" / f"{TOP}_bfm_tb.cpp"}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """Vitis HLS C-synthesis of the kernel — and the ``.f`` for the **wrapper**.

    The ``.f`` is named for what xsim elaborates, and it carries three things: csynth's generated
    files (all of them — a list naming only the top does not elaborate), the memory, and the wrapper.
    Re-emitted from the RTL that is actually on disk, because a stale file list plus a cached
    ``xsimk.dll`` is how an XSI run goes green while proving nothing.
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
        xsi = config.root_dir / "xsi"
        xsi.mkdir(parents=True, exist_ok=True)
        (xsi / f"rtl_{WRAPPER}.f").write_text(
            render_rtl_f(TOP, config.root_dir, extra=RTL_FILES), encoding="utf-8")
        return {"report_dir": config.root_dir / f"{TOP}_proj" / "solution1"}


def _write_scenario_zero(xsi_dir: Path, _config) -> None:
    """Materialize scenario zero's bundles before a traced run.

    The scenario is an **input** to the run, and the RTL gate leaves the *collision* vectors on disk
    behind it — so a trace step that did not write its own would render figures of whichever run went
    last.  That is the same stale-input failure ``RtlSimStep``'s own docstring records.
    """
    write_scenario(xsi_dir, scenario_zero())


def build_bram_access_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="bram_access_source", path=HERE / "bram_access.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    # The trace rung, and the two figures that read it.  Both are on-demand: `--through rtl_trace`
    # produces the waveform, `--through activity_figures` renders into results/ (gitignored), and
    # `--through sync_docs_figures` promotes them into docs/ as committed assets — so a docs figure
    # only changes when you mean it to.
    dag.add(RtlSimStep(name="rtl_trace", top=WRAPPER, tb=f"{TOP}_bfm_tb",
                       prepare=_write_scenario_zero))
    dag.add(ActivityFiguresStep(name="activity_figures"))
    dag.add(SyncDocsFiguresStep(name="sync_docs_figures"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_bram_access_dag,
                description="Build the bram_access design: pysim -> codegen -> csynth.",
                default_through="csynth",
                root_dir=HERE)
