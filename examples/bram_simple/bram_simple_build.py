"""bram_simple_build.py — build the shared-memory example: pysim -> codegen -> csynth.

``plans/bram_simple.md``.  The rungs, in the order a failure is cheapest to diagnose:

    pysim       -> the graph runs in SimPy: the witness's five values, and the two refusals
    codegen_dut -> the ap_ctrl_none top (with its two `mode=bram` ports), its tcl, its port map,
                   the memory placed beside it, and the WRAPPER that joins them
    codegen_tb  -> the XSI harness + main + scenario bundles
    csynth      -> Vitis HLS (needs the toolchain); re-emits rtl_<wrapper>.f from the RTL on disk

The RTL rung — the same scenario through real Verilog, plus the deliberate collision — is the
``-m xsi`` gate in ``tests/examples/test_bram_simple_xsi.py``.

**What a simulator elaborates is not the kernel.**  ``bram_simple_top.v`` instantiates ``bram_simple``
plus its hand-written ``bram_t2p`` memory, so the ``.f``, the snapshot and the shared library are
named for the wrapper — while csynth's project, its report and its generated Verilog keep the
kernel's name.  One artifact keeps the name it has; the new one is visibly the outer layer.
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
    render_ports_h,
    render_rtl_f,
    render_tb_harness,
    render_tb_main,
    render_tcl,
    render_top,
    tb_top_spec,
)
from waveflow.build.elaborate import elaborate  # noqa: E402
from waveflow.build.rtl_steps import GenRtlStep, GenWrapperStep  # noqa: E402
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep, XsiHarnessStep  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402

from examples.bram_simple.bram_simple import (  # noqa: E402
    DEPTH,
    WORD_BW,
    XSI_N_CYCLES,
    BramSimple,
    BramSimpleTB,
    Scenario,
    write_scenario,
)

#: The generated kernel's name, and the wrapper's.  The wrapper is what a simulator elaborates.
TOP = "bram_simple"
WRAPPER = f"{TOP}_top"
INCLUDE_DIR = "include"

#: Hand-written ``hls::task`` bodies (and the status header both share) copied verbatim from
#: ``src/`` — the example-local twin of ``MemStreamStep``.  Kept OUT of ``include/`` in the source
#: tree so nothing there is half-generated: everything in ``include/`` is a build product.
FIXED_TASK_BODIES = ("bram_cmd_status.h", "bram_write_cmd_task.h", "bram_read_cmd_task.h")

#: The RTL that must land in ``xsi/`` beside the ``.f`` naming it, in elaboration reading order: the
#: memory, then the wrapper that instantiates it.
RTL_FILES = ("bram_t2p.v", f"{WRAPPER}.v")

#: The gated geometry, stated rather than defaulted — **64-bit words**, so the byte/word address
#: convention is exercised rather than assumed (see ``bram_simple.WORD_BW``).  The recorded XSI cycle
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
    """Generate the top, its tcl, the XSI port map, the memory, and the wrapper.

    Note the order of dependence: the wrapper is derived from the *same* ``TopSpec`` that emits the
    kernel's interface pragmas, so the two cannot disagree about the kernel's port list — which for a
    ``bram`` port means fourteen signal names nobody typed twice.
    """
    config = BuildConfig(root_dir=out_dir, params={})
    copy_fixed_task_bodies(out_dir)

    inner = BuildDag()
    # Framework headers the generated top includes unconditionally.  This design uses neither a
    # stream utility nor the memory manager, but the include is emitted for every free-running top
    # and a missing file is a csynth error, not a warning.
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(XsiHarnessStep(output_dir="xsi"))
    # The memory lands in xsi/ rather than rtl/: it is compiled by the same xvlog invocation as the
    # wrapper and the kernel's RTL, and the `.f` names it relative to the directory it lives in.
    inner.add(GenRtlStep(name="place_memory", comp_class=_memory_class(), output_dir="xsi"))
    inner.add(GenWrapperStep(name="wrapper", comp_class=BramSimple, elab_params=dict(_ELAB),
                             width=WORD_BW, output_dir="xsi"))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    comp = elaborate(BramSimple, dict(_ELAB), name=TOP)
    spec = composite_top_spec(comp, width=WORD_BW)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, part=PART, period_ns=PERIOD_NS), encoding="utf-8")
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl + xsi/{spec.top_name}_ports.h "
          f"+ xsi/{WRAPPER}.v (elaborated top: {spec.elab_top})")
    return cpp


def _memory_class():
    """The memory class the design instantiates — read off the elaborated graph, not restated.

    A second mention of ``T2pBram`` here would be a place the build could disagree with the design
    about which memory it is placing.
    """
    comp = elaborate(BramSimple, dict(_ELAB))
    mems = {type(m) for m in comp.rtl_mods.values()}
    if len(mems) != 1:
        raise RuntimeError(f"expected exactly one RTL module class in {TOP}, got {mems}")
    return mems.pop()


def make_xsi_tb(n_cycles: int = XSI_N_CYCLES) -> BramSimpleTB:
    """The graph the XSI testbench is generated from — the same class the pysim golden runs."""
    return BramSimpleTB(name="xsi_tb", sim=Simulation(), n_cycles=int(n_cycles))


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
          f"({written.label or 'scenario'}: {len(written.cmd_w) // 2} write + "
          f"{len(written.cmd_r) // 2} read commands)")


def wrapper_text() -> str:
    """The wrapper Verilog for the gated configuration — for tests that want it without a build."""
    from waveflow.build.wrapper_gen import render_wrapper, wrapper_spec

    comp = elaborate(BramSimple, dict(_ELAB), name=TOP)
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

    description = "Run the BramSimpleTB pysim golden (the witness's five values + two refusals)."
    consumes = ["bram_simple_source"]
    produces: ClassVar[dict] = {"pysim_results": Path("results/bram_simple_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        import json

        from examples.bram_simple.bram_simple import (
            captured,
            check_outputs,
            run_pysim,
            scenario_zero,
        )

        sc = scenario_zero()
        tb = run_pysim(sc=sc)
        resp_w, data_r, resp_r = captured(tb)
        check_outputs(resp_w, data_r, resp_r, sc, where="pysim: ")
        out = config.root_dir / "results" / "bram_simple_pysim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "bitwidth": WORD_BW, "depth": DEPTH,
            "write_cmds": len(sc.cmd_w) // 2, "read_cmds": len(sc.cmd_r) // 2,
            "resp_w": [int(v) for v in resp_w], "resp_r": [int(v) for v in resp_r],
            "data_r": [int(v) for v in data_r],
            "first_data_cycle": int(tb.data_r_snk.cycles[0]),
            "last_data_cycle": int(tb.data_r_snk.cycles[-1])}, indent=2), encoding="utf-8")
        return {"pysim_results": out}


@dataclass(kw_only=True)
class CodegenDutStep(BuildStep):
    description = "Lower BramSimple to its ap_ctrl_none top + the memory + the wrapper."
    consumes = ["bram_simple_source"]
    produces: ClassVar[dict] = {"bram_simple_cpp": Path(f"{GEN_DIR}/{TOP}.cpp"),
                                "run_tcl": Path(f"{TOP}.tcl"),
                                "dut_ports": Path(f"xsi/{TOP}_ports.h"),
                                "wrapper_v": Path(f"xsi/{WRAPPER}.v"),
                                "memory_v": Path("xsi/bram_t2p.v")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        generate_dut(config.root_dir)
        root = config.root_dir
        return {"bram_simple_cpp": root / GEN_DIR / f"{TOP}.cpp",
                "run_tcl": root / f"{TOP}.tcl",
                "dut_ports": root / "xsi" / f"{TOP}_ports.h",
                "wrapper_v": root / "xsi" / f"{WRAPPER}.v",
                "memory_v": root / "xsi" / "bram_t2p.v"}


@dataclass(kw_only=True)
class CodegenTbStep(BuildStep):
    description = "Lower BramSimpleTB to the XSI harness + main + scenario bundles."
    consumes = ["bram_simple_source", "dut_ports"]
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
    consumes = ["bram_simple_cpp", "run_tcl"]
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


def build_bram_simple_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="bram_simple_source", path=HERE / "bram_simple.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(CodegenDutStep(name="codegen_dut"))
    dag.add(CodegenTbStep(name="codegen_tb"))
    dag.add(CSynthStep(name="csynth"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_bram_simple_dag,
                description="Build the bram_simple design: pysim -> codegen -> csynth.",
                default_through="csynth",
                root_dir=HERE)
