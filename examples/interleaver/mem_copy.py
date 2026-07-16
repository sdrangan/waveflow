"""mem_copy.py — the Phase-2 composition de-risk: a hierarchical :class:`MemCopy` component and the
**graph-derived** composite codegen (``plans/mem_stream_impl.md`` Phase 2).

``MemCopy`` composes three pre-written sub-components — a pure-stream :class:`Sequencer`, a
:class:`~waveflow.hw.mem_stream.MemRStream`, and a :class:`~waveflow.hw.mem_stream.MemWStream`
(``emit_done``) — into ONE free-running (``ap_ctrl_none``) ``hls::task`` top that memcpy's a word run
from one buffer to another.  No ``stream_of_blocks``, no compute: it exists only to prove the codegen
can emit a genuinely-*generated* (not copied) multi-task top and wire the tasks with internal FIFOs,
**derived from the component/interface graph**.

The real deliverable is :func:`composite_top_spec`: it walks the parent's ``sub_comps`` + internal
``interfaces`` (``add_comp`` / ``add_if``) and its boundary ports, and resolves each sub-component's
``hls::task`` signature (endpoint attr names, from :meth:`~waveflow.hw.mem_stream.KernelTask`) to
either a top-level port or an internal ``hls_thread_local`` FIFO.  The standalone mem-stream kernel is
the degenerate 1-node case of exactly this generator — this is the seam Phase 4 builds on.

Run (project venv, from repo root)::

    PYTHONPATH=. pysilicon-venv/Scripts/python.exe examples/interleaver/mem_copy.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.build.build import BuildConfig, BuildDag  # noqa: E402
from waveflow.build.streamutils import MemMgrStep, MemStreamStep, StreamUtilsStep  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.dataschema import DataList, DataSchemaStep, IntField  # noqa: E402
from waveflow.hw.hw_component import HwParam  # noqa: E402
from waveflow.hw.hw_composite import CompositeComp  # noqa: E402
from waveflow.hw.hw_freerun import FreeRunComp  # noqa: E402
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave  # noqa: E402
from waveflow.hw.synth import synthesizable  # noqa: E402
from waveflow.hw.mem_stream import (  # noqa: E402
    KernelTask,
    MRCmd,
    MWCmd,
    MemComplete,
    MemRStream,
    MemWStream,
    WORD_BW_SUPPORTED,
    XferMsgArr,
)
from waveflow.simulation.simobj import ProcessGen  # noqa: E402

from examples.interleaver.mem_stream_gen import (  # noqa: E402
    DEFAULT_MEM_DW,
    GEN_DIR,
    INCLUDE_DIR,
    render_tcl,
    render_top,
)
from examples.interleaver.composite_gen import (  # noqa: E402
    StreamEdge,
    composite_top_spec,
)

# --- command field type (fixed width — element/word coordinates, the word_index convention) -------
Word32 = IntField.specialize(bitwidth=32, signed=False)


class CopyCmd(DataList):
    """One :class:`MemCopy` app command (host -> ``s_cmd``): copy ``n_words`` packed words from the
    source word offset ``src_off`` to the destination word offset ``dst_off``.  All three are
    **element/word coordinates** relative to their buffer bases (the addressing convention —
    ``plans/component.md``; the physical bases live in the two ``offset=slave`` registers)."""
    elements = {
        "src_off": {"schema": Word32, "description": "source element/word offset"},
        "dst_off": {"schema": Word32, "description": "destination element/word offset"},
        "n_words": {"schema": Word32, "description": "number of packed words to copy"},
    }


#: Schema classes the gen-include step emits C++ headers for (the composite's command structs).
SCHEMA_CLASSES = [CopyCmd, MRCmd, MWCmd, MemComplete, XferMsgArr]


@dataclass
class Sequencer(FreeRunComp):
    """Pure-stream command sequencer: dequeue one :class:`CopyCmd` and issue one :class:`MRCmd` +
    one :class:`MWCmd` (a straight copy needs no demux — Phase 4 splits P/X).  Active, touches ONLY
    streams, so it composes as an internal ``hls::task``.

    Endpoints: ``s_cmd`` (:class:`StreamIFSlave` carrying :class:`CopyCmd`, the top boundary),
    ``mr_cmd`` / ``mw_cmd`` (:class:`StreamIFMaster`, internal edges to the two mem-streams).

    **What generates today, and what does not.**  The C++ that MemCopy actually builds with is the
    hand-written ``mem_seq_task.h`` named by :meth:`kernel_task` — copied verbatim, not lowered from
    this class (the same contract as MemRStream/MemWStream).  :meth:`run_iter` is the **pysim
    golden**, and nothing mechanically ties the two: keeping them in agreement is on you, and the
    only thing that checks it is the tests.

    :meth:`run_iter` is nevertheless written in the *extractable* shape — ``get`` -> hook -> ``write``
    — so it lowers today as a leaf (``extract_kernel``/``kernel_files_to_str`` both succeed; pinned
    by ``test_sequencer_run_iter_is_extractable``).  It is not wired into the composite because the
    emitted code has two known gaps: it emits an ``s_axilite``/``ap_ctrl_hs`` top rather than
    ``ap_ctrl_none`` (``free_running_kernel`` is a declared-but-unimplemented target), and it emits
    ``streamutils::axi4s_word<W>`` streams where the composite's tasks use ``hls::stream<ap_uint<W>>``
    + ``read_stream<W>``.  Closing those two is what would let this class replace ``mem_seq_task.h``.

    **Why the state lives behind a hook.**  The extractor forbids reading mutable ``self.X`` in a
    lowered body (``_validate_no_implicit_capture``), and FreeRunComp cross-iteration state is not
    wired — so the per-job counter cannot appear in :meth:`run_iter` directly.  Putting it behind the
    ``@synthesizable`` :meth:`next_xfer_msg` boundary is what makes the body extractable: the hook is
    a *declaration*, and its hand-written C++ owns the ``static ap_uint<32> job_idx`` (which is
    exactly what ``mem_seq_task.h`` already does)."""

    cpp_kernel_name: ClassVar[str | None] = "mem_seq"
    cpp_namespace: ClassVar[str | None] = "mem_seq_impl"

    mem_dwidth: HwParam[int] = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.s_cmd = StreamIFSlave(
            name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=w, has_tlast=False)
        self.mr_cmd = StreamIFMaster(
            name=f"{self.name}_mr_cmd", sim=self.sim, bitwidth=w, has_tlast=False)
        self.mw_cmd = StreamIFMaster(
            name=f"{self.name}_mw_cmd", sim=self.sim, bitwidth=w, has_tlast=False)
        for ep in (self.s_cmd, self.mr_cmd, self.mw_cmd):
            self.add_endpoint(ep)
        #: Per-job correlation cookie: the job index, so xfer_msg is genuinely exercised (round-tripped
        #: on MemComplete) rather than merely tolerated.  Length from MRCmd's own max_xfer_len default
        #: (introspected, not hardcoded — both MRCmd/MWCmd share it).
        self._job_idx = 0
        self._xfer_msg_len = MRCmd._params["max_xfer_len"].default

    @property
    def Cmd(self) -> type[CopyCmd]:
        return CopyCmd

    def kernel_task(self) -> KernelTask:
        return KernelTask("mem_seq_task", "mem_seq_task.h", ("s_cmd", "mr_cmd", "mw_cmd"),
                          template_args=(int(self.mem_dwidth),))

    @synthesizable
    def next_xfer_msg(self) -> XferMsgArr:
        """Hook: the per-job correlation cookie — ``xfer_msg[0] = job_idx``, then advance.

        The counter is the component's only cross-firing state, and it lives here rather than in
        :meth:`run_iter` because a lowered body may not read mutable ``self.X``.  The hand-written
        C++ owns it as a ``static ap_uint<32> job_idx``; there is no ``self._job_idx`` in the
        generated code, and nothing lowers this Python."""
        msg = np.zeros(self._xfer_msg_len, dtype=np.uint32)
        msg[0] = self._job_idx
        self._job_idx += 1
        return msg

    @synthesizable
    def make_mr_cmd(self, cmd: CopyCmd, msg: XferMsgArr) -> MRCmd:
        """Hook: ``CopyCmd`` -> the read command.  Element coordinates pass through verbatim (no
        byte<->word conversion).  Building the command is a hook, not inline in :meth:`run_iter`,
        because constructing a DataSchema is not in the extractor's vocabulary."""
        return MRCmd(addr=int(cmd.src_off), len=int(cmd.n_words), xfer_len=1, xfer_msg=msg)

    @synthesizable
    def make_mw_cmd(self, cmd: CopyCmd, msg: XferMsgArr) -> MWCmd:
        """Hook: ``CopyCmd`` -> the write command, carrying the same job cookie as the read."""
        return MWCmd(addr=int(cmd.dst_off), len=int(cmd.n_words), xfer_len=1, xfer_msg=msg)

    def run_iter(self) -> ProcessGen[None]:
        """The pysim golden — one firing = one :class:`CopyCmd` (the ``hls::task`` runtime re-fires
        it; there is no command loop here).  Read a command, stamp a job cookie, and issue
        ``MRCmd{src_off, n}`` then ``MWCmd{dst_off, n}`` carrying that same cookie, so the
        ``MemComplete`` echo can be correlated back to the job that issued it."""
        cmd: CopyCmd = yield from self.s_cmd.get(CopyCmd)
        msg = self.next_xfer_msg()
        mr = self.make_mr_cmd(cmd, msg)
        yield from self.mr_cmd.write(mr)
        mw = self.make_mw_cmd(cmd, msg)
        yield from self.mw_cmd.write(mw)


@dataclass
class MemCopy(CompositeComp):
    """Hierarchical memcpy composite: ``Sequencer -> MemRStream -> MemWStream`` over internal FIFOs.

    Top-level endpoints (the composite boundary): ``s_cmd`` (:class:`CopyCmd` in), ``m_in``
    (:class:`~waveflow.hw.memif.MMIFMaster` ``@port_read`` -> ``gmem0``), ``m_out`` (``@port_write``
    -> ``gmem1``), ``s_done`` (completion token out).  Sub-components are wired by three internal
    :class:`StreamIF` edges (``mr_cmd`` / ``mw_cmd`` command FIFOs + a ``copy_data`` word FIFO); the
    two sub-component ``m_mem`` masters are the top ``m_in`` / ``m_out``.  Passive at this level (the
    sub-components own the processes); :meth:`composite_top_spec` derives the generated top from this
    graph."""

    cpp_kernel_name: ClassVar[str | None] = "mem_copy"
    cpp_namespace: ClassVar[str | None] = "mem_copy_impl"

    mem_dwidth: HwParam[int] = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)

        # --- sub-components (add_comp; insertion order == codegen task order) ---
        self.seq = Sequencer(name=f"{self.name}_seq", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.rstream = MemRStream(name=f"{self.name}_r", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.wstream = MemWStream(
            name=f"{self.name}_w", sim=self.sim, mem_dwidth=w, emit_done=True, clk=self.clk)
        for c in (self.seq, self.rstream, self.wstream):
            self.add_comp(c)
        self.ordered_subcomps = [self.seq, self.rstream, self.wstream]

        # --- internal interfaces (add_if + bind): each an on-chip FIFO in codegen ---
        self._mr_if = StreamIF(
            name=f"{self.name}_mr_cmd_if", sim=self.sim, clk=self.clk, bitwidth=w)
        self._mr_if.bind("master", self.seq.mr_cmd)
        self._mr_if.bind("slave", self.rstream.s_cmd)
        self._mw_if = StreamIF(
            name=f"{self.name}_mw_cmd_if", sim=self.sim, clk=self.clk, bitwidth=w)
        self._mw_if.bind("master", self.seq.mw_cmd)
        self._mw_if.bind("slave", self.wstream.s_cmd)
        self._data_if = StreamIF(
            name=f"{self.name}_copy_data_if", sim=self.sim, clk=self.clk, bitwidth=w)
        self._data_if.bind("master", self.rstream.m_out)
        self._data_if.bind("slave", self.wstream.s_in)
        for i in (self._mr_if, self._mw_if, self._data_if):
            self.add_if(i)

        # --- graph descriptors the composite generator walks -------------------------------------
        #: Internal edges -> one hls_thread_local hls::stream FIFO each (all StreamEdge here).
        self.internal_edges = [
            StreamEdge("mr_cmd", self.seq.mr_cmd, self.rstream.s_cmd),
            StreamEdge("mw_cmd", self.seq.mw_cmd, self.wstream.s_cmd),
            StreamEdge("copy_data", self.rstream.m_out, self.wstream.s_in),
        ]
        #: Boundary ports (name, endpoint, kind, bundle) — order == top signature order.
        self.boundary = [
            ("s_cmd", self.seq.s_cmd, "axis_in", None),
            ("m_in", self.rstream.m_mem, "maxi_read", "gmem0"),
            ("m_out", self.wstream.m_mem, "maxi_write", "gmem1"),
            ("s_done", self.wstream.s_done, "axis_out", None),
        ]
        #: Command-struct headers the generated top #includes (single source with the pysim .get()).
        self.cmd_headers = tuple(dict.fromkeys(c.resolved_include_filename() for c in SCHEMA_CLASSES))

        # convenience refs for the sim harness (the boundary endpoints live on the children)
        self.s_cmd = self.seq.s_cmd
        self.m_in = self.rstream.m_mem
        self.m_out = self.wstream.m_mem
        self.s_done = self.wstream.s_done


# The graph -> composite TopSpec derivation lives in composite_gen.composite_top_spec (imported
# above and re-exported here for callers/tests); MemCopy just supplies the graph descriptors above.


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def gen_headers(config: BuildConfig) -> None:
    """Generate the command-struct headers + memmgr.hpp + streamutils_hls.h + the fixed task-body
    headers (mem_seq / mem_r_stream / mem_w_stream_done) into ``include/``."""
    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(MemStreamStep(output_dir=INCLUDE_DIR))
    for cls in SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED,
                                 include_dir=INCLUDE_DIR))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")


def generate(out_dir: Path = HERE, width: int = DEFAULT_MEM_DW) -> dict[str, Path]:
    """Generate headers + the MemCopy composite top .cpp + its csynth .tcl into *out_dir*."""
    from waveflow.build.elaborate import elaborate

    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config)

    comp = elaborate(MemCopy, {"mem_dwidth": width}, name="mem_copy")
    spec = composite_top_spec(comp, width=width)

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    tcl = out_dir / f"{spec.top_name}.tcl"
    tcl.write_text(render_tcl(spec.top_name), encoding="utf-8")
    print(f"generated {cpp.relative_to(out_dir)} + {tcl.name}")
    return {spec.top_name: cpp}


if __name__ == "__main__":
    generate()
