"""state_toy.py — the smallest design that *needs* cross-firing state, and its XSI gate.

The Stage-2 gate of ``plans/add_state.md``.  csynth can only say the emitted ``static`` compiles and
infers a memory; it cannot say the value **survives between firings** in RTL.  Only a real RTL run
can, and that is the one open risk on the reset-semantics trap (an initialized static swept by
``ap_rst`` would still csynth perfectly).

The design is a per-lane running total: one ``Vec4`` in, the accumulated ``Vec4`` out, with the
accumulator declared via :meth:`~waveflow.hw.hw_module.HwModule.add_state`.  It is chosen because
the failure mode is **loud**: feed it all-ones vectors and a working design emits 1,2,3,4,...  while
a design whose state is reset (or never persisted) emits 1,1,1,1.  A gate that can only fail by an
off-by-one would not be worth the toolchain.

Structure mirrors ``examples/mem_copy``: the DUT is a leaf ``FreeRunMod`` (the 1-task degenerate
composite), the testbench is a component **graph** (:class:`StateAccumTB`), and the scenario is
written once as burst bundles that both backends read.  ``python -m examples.state_toy.state_toy``
generates the DUT (task body, top, csynth TCL); the RTL run is the **system** flow,
``python -m examples.state_toy.state_toy_system_build``, which builds its own XSI workspace.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent

from waveflow.build.build import BuildConfig, BuildDag  # noqa: E402
from waveflow.build.composite_gen import (  # noqa: E402
    GEN_DIR,
    composite_top_spec,
    render_tcl,
    render_top,
)
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep  # noqa: E402
from waveflow.utils.burst_io import write_burst_bundle  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB  # noqa: E402
from waveflow.hw.dataschema import DataArray, DataSchemaStep, IntField  # noqa: E402
from waveflow.hw.hw_freerun import FreeRunMod  # noqa: E402
from waveflow.hw.hw_module import HwParam  # noqa: E402
from waveflow.hw.hw_state import HwState  # noqa: E402
from waveflow.hw.hw_system import GraphSystem  # noqa: E402
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave  # noqa: E402
from waveflow.hw.mem_stream import KernelTask  # noqa: E402
from waveflow.hw.synth import synthesizable  # noqa: E402
from waveflow.simulation.simobj import ProcessGen  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.simulation.stream_tb import StreamDriver, StreamSink  # noqa: E402

WORD_BW = 32
NLANE = 4

Word32 = IntField.specialize(bitwidth=WORD_BW, signed=False)


class Vec4(DataArray):
    """The stream payload: one 4-lane vector per firing."""

    element_type = Word32
    static = True
    max_shape = (NLANE,)


#: The accumulator's storage.  ``cpp_storage="raw"`` is what makes it lower to a bare
#: ``ap_uint<32> total[4]`` rather than a struct — the form a ``static`` declaration wants.
AccArray = DataArray.specialize(Word32, max_shape=(NLANE,), cpp_storage="raw")

SCHEMA_CLASSES = [Vec4, AccArray]

#: The hand-written hook body (sticky at the example root; never regenerated over).
HOOK_IMPL = "state_accum_accumulate_impl.cpp"


@dataclass
class StateAccum(FreeRunMod):
    """Per-lane running total: ``total += x``, emit ``total``.  One firing = one vector."""

    cpp_kernel_name: ClassVar[str | None] = "state_accum"
    cpp_namespace: ClassVar[str | None] = "state_accum_impl"

    mem_dwidth: HwParam[int] = WORD_BW
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        # has_tlast=False: the generated top carries plain ``hls::stream<ap_uint<32>>`` ports
        # (no axi4s_word), and a firing reads a fixed 4 words — there is no packet boundary to
        # signal.  It also has to match the BFM driver/sink, which are tlast-free by default.
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=w,
                                  has_tlast=False)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.m_out)

        # THE point of this example: storage that outlives a firing, declared rather than captured.
        self.total = HwState(AccArray())
        self.add_state(self.total)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "state_accum_task", "state_accum_task.h", ("s_in", "m_out"),
            # No template args: the generated body bakes the concrete width (the endpoints were
            # built from int(mem_dwidth), so nothing stayed symbolic for task_template_params to
            # template on).  The top must instantiate it bare or the two disagree.
            template_args=(),
        )

    def run_iter(self) -> ProcessGen[None]:
        x = yield from self.s_in.get_schema(Vec4)
        y = self.accumulate(x, self.total)
        yield from self.m_out.write(y)

    @synthesizable
    def accumulate(self, x: Vec4, total: HwState) -> Vec4:
        """Add ``x`` into the running total and emit it.  The pysim twin of the hand-written hook."""
        total.val[:] = total.val + x.val  # uint32 arrays wrap mod 2**32, like the ap_uint<32> add
        return Vec4(total.val.copy())


@dataclass
class StateAccumTB(FreeRunMod):
    """The testbench as a **graph**: driver -> DUT -> sink.  Walkable, so the XSI harness generates."""

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    nvec: int = 5
    n_cycles: int = 400
    mem_dwidth: HwParam[int] = WORD_BW
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.dut = StateAccum(name=f"{self.name}_dut", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.driver = StreamDriver(sim=self.sim, bitwidth=w, in_bundle="vectors/s_in")
        self.sink = StreamSink(sim=self.sim, bitwidth=w, out_bundle="vectors/m_out")
        for c in (self.dut, self.driver, self.sink):
            self.add_comp(c)

        in_if = StreamIF(name=f"{self.name}_in_if", sim=self.sim, clk=self.clk, bitwidth=w)
        in_if.bind(ep_name="master", endpoint=self.driver.stream_ep)
        in_if.bind(ep_name="slave", endpoint=self.dut.s_in)
        self.add_if(in_if)

        out_if = StreamIF(name=f"{self.name}_out_if", sim=self.sim, clk=self.clk, bitwidth=w)
        out_if.bind(ep_name="master", endpoint=self.dut.m_out)
        out_if.bind(ep_name="slave", endpoint=self.sink.stream_ep)
        self.add_if(out_if)

        self.boundary = []


def input_words(nvec: int = 5) -> list[np.ndarray]:
    """The scenario: ``nvec`` all-ones vectors, one burst each (one burst = one firing)."""
    return [np.ones(NLANE, dtype=np.uint64) for _ in range(nvec)]


def expected_words(nvec: int = 5) -> np.ndarray:
    """1,2,3,... per lane — the running total.  Flat, in arrival order."""
    return np.concatenate(
        [np.full(NLANE, i + 1, dtype=np.uint64) for i in range(nvec)]
    )


def write_scenario(root, nvec: int = 5) -> None:
    """Materialize the input bundle under ``<root>/vectors`` — the one source both backends read."""
    write_burst_bundle(input_words(nvec), Path(root) / "vectors" / "s_in")


@dataclass
class StateAccumSystem(GraphSystem):
    """state_toy as a **system** (``plans/system_model.md`` S3e): the :class:`StateAccumTB` graph,
    the cut the accumulator alone -- its two streams top ports facing the driver and the sink."""

    nvec: int = 5
    n_cycles: int = 400

    def graph(self) -> StateAccumTB:
        return StateAccumTB(name="tb", sim=Simulation(), nvec=int(self.nvec),
                            n_cycles=int(self.n_cycles))

    def dut_modules(self, g) -> list:
        return [g.dut]

    def write_inputs(self, root) -> None:
        write_scenario(root, int(self.nvec))

    def check(self) -> np.ndarray:
        """The sink's words, flat, in arrival order."""
        return np.concatenate([np.asarray(b).ravel() for b in self.tb.sink.words]).astype(np.uint64)


def gen_headers(config: BuildConfig) -> None:
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir="include"))
    dag.add(MemMgrStep(output_dir="include"))
    for cls in SCHEMA_CLASSES:
        dag.add(DataSchemaStep(cls, word_bw_supported=[WORD_BW], include_dir="include"))
    results = dag.run(config)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"header generation failed: {failed}")


def generate_dut(out_dir: Path = HERE) -> Path:
    """Generate the task body, the ``ap_ctrl_none`` top, and its csynth TCL."""
    from waveflow.build.elaborate import elaborate
    from waveflow.build.hwgen import task_files_to_str

    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config)

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    inc = out_dir / "include"
    # The generated task body carries the static; the hook stub is NOT written (the hand-written
    # body at the example root is the real one — hooks are sticky, never regenerated over).
    for name, content in task_files_to_str(StateAccum).items():
        if name.endswith("_impl.cpp"):
            continue
        (inc / name).write_text(content, encoding="utf-8")

    comp = elaborate(StateAccum, {"mem_dwidth": WORD_BW}, name="state_accum")
    spec = composite_top_spec(comp, width=WORD_BW)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    # The hook lives in its own translation unit (the generated body only declares it), so the
    # csynth project needs it added explicitly — see render_tcl's extra_sources.
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, extra_sources=(HOOK_IMPL,),
                   part="xc7z020clg484-1", period_ns=10), encoding="utf-8")
    print(f"generated DUT {cpp.name} + {spec.top_name}.tcl")
    return cpp


if __name__ == "__main__":
    generate_dut()
