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

    PYTHONPATH=. pysilicon-venv/Scripts/python.exe examples/mem_copy/mem_copy.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent

from waveflow.build.build import BuildConfig, BuildDag, SourceStep  # noqa: E402
from waveflow.build.hwcodegen_steps import TaskBodyStep  # noqa: E402
from waveflow.build.streamutils import (  # noqa: E402
    MemMgrStep,
    MemStreamStep,
    StreamUtilsStep,
    XsiHarnessStep,
)
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.dataschema import DataList, DataSchemaStep, IntField  # noqa: E402
from waveflow.hw.hw_component import HwParam  # noqa: E402
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

from waveflow.build.composite_gen import (  # noqa: E402
    DEFAULT_MEM_DW,
    GEN_DIR,
    INCLUDE_DIR,
    composite_top_spec,
    render_ports_h,
    render_tb_harness,
    render_tb_main,
    tb_top_spec,
    render_tcl,
    render_top,
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
        "tx_id":   {"schema": Word32, "description": "host transaction ID, echoed on completion"},
    }


@dataclass(frozen=True)
class CopyJob:
    """One copy in a test **scenario**: copy ``n_words`` from word offset ``src_off`` to word offset
    ``dst_off``.  All three are **word coordinates** (not bytes).

    This is the scenario-level spec, distinct from :class:`CopyCmd` (the serialized on-wire command
    the driver actually plays).  Named fields so a scenario reads without memorizing tuple order; a
    plain ``(src_off, dst_off, n_words)`` tuple is still accepted anywhere a ``CopyJob`` is, via
    :meth:`coerce`."""
    src_off: int
    dst_off: int
    n_words: int

    @classmethod
    def coerce(cls, job: "CopyJob | tuple[int, int, int]") -> "CopyJob":
        """Accept a ``CopyJob`` or a bare ``(src, dst, n)`` triple; return a ``CopyJob``.

        The last branch rebuilds by field, which also handles a ``CopyJob`` from a *different import*
        of this module — running ``mem_copy.py`` as a script makes its ``CopyJob`` a distinct class
        object from the package-imported one, so a plain ``isinstance`` would spuriously fail."""
        if isinstance(job, cls):
            return job
        if isinstance(job, tuple):
            return cls(*job)
        return cls(job.src_off, job.dst_off, job.n_words)


#: Schema classes the gen-include step emits C++ headers for (the composite's command structs).
SCHEMA_CLASSES = [CopyCmd, MRCmd, MWCmd, MemComplete, XferMsgArr]


@dataclass
class Sequencer(FreeRunComp):
    """Pure-stream command sequencer: dequeue one :class:`CopyCmd` and issue one :class:`MRCmd` +
    one :class:`MWCmd` (a straight copy needs no demux — Phase 4 splits P/X).  Active, touches ONLY
    streams, so it composes as an internal ``hls::task``.

    Endpoints: ``s_cmd`` (:class:`StreamIFSlave` carrying :class:`CopyCmd`, the top boundary),
    ``mr_cmd`` / ``mw_cmd`` (:class:`StreamIFMaster`, internal edges to the two mem-streams).

    **This body is GENERATED from :meth:`run_iter`** — the first one in the codebase that is.
    ``TaskBodyStep`` lowers it to ``include/mem_seq_task.h`` (templated, ``static``, pragma-free over
    ``hls::stream<ap_uint<W>>``), which the composite top instantiates as ``mem_seq_task<64>``.
    Verified end-to-end: identical csynth (latency/II 21, 283 LUT, 526 FF; same RTL module set) and
    identical XSI behaviour (16/16 jobs, zero ``xfer_msg`` echo mismatches; ``cycles=3347`` as the
    gate then reported it — the same run reads 2835 since the reporting fix, which subtracted the
    testbench's fixed drain tail) against the hand-written body it replaced.  Contrast MemRStream/MemWStream, whose bodies own ``m_axi``
    and stay hand-written and copied.

    **What is still hand-written: the three hooks.**  Codegen derives the body's *structure* — the
    stream read, the calls, the two writes, in ``run_iter``'s order — not its leaf computation.
    ``mem_seq_{make_xfer_msg,make_mr_cmd,make_mw_cmd}_impl.cpp`` live at the example root and are
    **sticky**: ``TaskBodyStep`` writes each as a TODO stub once, only if absent, then never touches
    it again.  Nothing mechanically ties a hook's C++ to the Python above it — that agreement is
    yours to keep, and only the tests check it.

    **Why the shape is `get` -> hook -> `write`.**  Two extractor rules force it, and neither is
    stylistic.  Constructing a ``DataSchema`` is not in the extractor's vocabulary at all
    (``x = MRCmd(...)`` and even bare ``x = MRCmd()`` are rejected), so the correlation cookie and the
    two commands are all built in ``@synthesizable`` hooks — the cookie in :meth:`make_xfer_msg` (from
    ``cmd.tx_id``), the commands in :meth:`make_mr_cmd` / :meth:`make_mw_cmd`.  (A lowered body also may
    not read mutable ``self.X`` — ``_validate_no_implicit_capture`` — which is why the Sequencer keeps
    no cross-firing state now that the cookie comes from the command rather than a counter.)
    Pinned by ``test_sequencer_run_iter_is_extractable``."""

    cpp_kernel_name: ClassVar[str | None] = "mem_seq"
    cpp_namespace: ClassVar[str | None] = "mem_seq_impl"

    mem_dwidth: HwParam[int] = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        # Pass the HwParamValue itself, NOT int(self.mem_dwidth): the parameter identity is what
        # lets codegen template the task body on `MEM_DWIDTH` rather than bake the default width.
        # `int()` here would silently produce a 64-wide body for a 32-wide instance.
        self.s_cmd = StreamIFSlave(
            name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=self.mem_dwidth, has_tlast=False)
        self.mr_cmd = StreamIFMaster(
            name=f"{self.name}_mr_cmd", sim=self.sim, bitwidth=self.mem_dwidth, has_tlast=False)
        self.mw_cmd = StreamIFMaster(
            name=f"{self.name}_mw_cmd", sim=self.sim, bitwidth=self.mem_dwidth, has_tlast=False)
        for ep in (self.s_cmd, self.mr_cmd, self.mw_cmd):
            self.add_endpoint(ep)
        #: Correlation-cookie length, from MRCmd's own max_xfer_len default (introspected, not
        #: hardcoded — both MRCmd/MWCmd share it).  The cookie VALUE is the command's ``tx_id`` (the
        #: host's transaction ID), stamped in :meth:`make_xfer_msg` and echoed on ``MemComplete`` — so
        #: the Sequencer holds no cross-firing state.
        self._xfer_msg_len = MRCmd._params["max_xfer_len"].default

    @property
    def Cmd(self) -> type[CopyCmd]:
        return CopyCmd

    def kernel_task(self) -> KernelTask:
        return KernelTask("mem_seq_task", "mem_seq_task.h", ("s_cmd", "mr_cmd", "mw_cmd"),
                          template_args=(int(self.mem_dwidth),))

    @synthesizable
    def make_xfer_msg(self, cmd: CopyCmd) -> XferMsgArr:
        """Hook: the correlation cookie — ``xfer_msg[0] = cmd.tx_id``, the host's transaction ID.

        The value comes from the *command*, so the Sequencer keeps no cross-firing state: a completion
        on ``s_done`` echoes this cookie, letting the host match it to the exact request it issued.
        Building the array is a hook (not inline in :meth:`run_iter`) because array construction is not
        in the extractor's vocabulary."""
        msg = np.zeros(self._xfer_msg_len, dtype=np.uint32)
        msg[0] = int(cmd.tx_id)
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
        msg = self.make_xfer_msg(cmd)
        mr = self.make_mr_cmd(cmd, msg)
        yield from self.mr_cmd.write(mr)
        mw = self.make_mw_cmd(cmd, msg)
        yield from self.mw_cmd.write(mw)


@dataclass
class MemCopy(FreeRunComp):
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
        # The internal edges ARE the add_if calls above (each -> one hls_thread_local hls::stream),
        # so there is nothing to declare: derive_internal_edges reads them off the graph.
        #
        # Boundary port NAMES only.  Nothing else is ours to say: the endpoints and their order are
        # the unwired child ports in add_comp x add_endpoint order, the endpoint's TYPE gives the
        # direction, and the gmem bundles are assigned by policy in this order (bundle_map) --
        # m_in -> gmem0, m_out -> gmem1.  The names must be stated because the children's local names
        # collide (both mem streams call their AXI port m_mem).
        # See plans/endpoint_types_not_tags.md.
        self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
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

#: The Sequencer's hand-written hook bodies, at the example root (where every other hand-written hook
#: lives — cf. ``examples/shared_mem/hist_compute_impl.cpp``).  ``TaskBodyStep`` writes each as a TODO
#: stub **once, only if absent**, then never touches it again; these are edited source, not output.
#: They must be added to the csynth project: the generated ``mem_seq_task.h`` calls across a TU
#: boundary, so unlike a self-contained hand-written body the link cannot resolve them otherwise.
SEQ_HOOK_SOURCES = (
    "mem_seq_make_xfer_msg_impl.cpp",
    "mem_seq_make_mr_cmd_impl.cpp",
    "mem_seq_make_mw_cmd_impl.cpp",
)


def gen_headers(config: BuildConfig) -> None:
    """Generate the command-struct headers + memmgr.hpp + streamutils_hls.h + the task-body headers
    into ``include/``.

    Two kinds of task body land here, and the difference is the point: ``MemStreamStep`` **copies**
    the fixed hand-written ``mem_r_stream_task.h`` / ``mem_w_stream_done_task.h`` (they own ``m_axi``,
    which task-body codegen does not do), while ``TaskBodyStep`` **generates** ``mem_seq_task.h`` from
    :meth:`Sequencer.run_iter` and drops its hook stubs at the example root."""
    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(MemStreamStep(output_dir=INCLUDE_DIR))
    # The XSI harness (BFM + loader + run.bat) is framework; copy it beside the testbench.
    inner.add(XsiHarnessStep(output_dir="xsi"))
    for cls in SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED,
                                 include_dir=INCLUDE_DIR))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")


#: The XSI testbench's scenario: NUM_CMDS back-to-back copies of N words, each from SRC_W[j] to
#: DST_W[j] (element/word coordinates in one flat arena).  Back-to-back jobs are the point — they
#: exercise the ``hls::task`` re-fire, and because the driver never waits for a completion they
#: overlap, which is the ~1.9x the gate measures.
XSI_N        = 128
XSI_NUM_CMDS = 16
XSI_SRC_W = [64 + 128 * j for j in range(XSI_NUM_CMDS)]
XSI_DST_W = [4096 + 128 * j for j in range(XSI_NUM_CMDS)]

#: The same scenario as ``MemCopyTB``'s own ``jobs`` parameter.  This is what "one statement, two
#: backends" actually means: **one class, two instantiations** — pysim builds a ``MemCopyTB`` with
#: its jobs, the XSI generator builds one with these.  Nothing about the scenario is stated twice.
XSI_JOBS = tuple(CopyJob(s, d, XSI_N) for s, d in zip(XSI_SRC_W, XSI_DST_W))


def make_xsi_tb(width: int = DEFAULT_MEM_DW):
    """The :class:`~examples.mem_copy.mem_copy_sim.MemCopyTB` instance the XSI testbench is
    generated from.

    Imported lazily: ``mem_copy_sim`` imports this module for :class:`MemCopy`/:class:`CopyCmd`, so a
    module-level import here would be circular.  The testbench graph depends on the design; the
    design's *generator* depends on the graph.
    """
    from waveflow.simulation.simulation import Simulation
    from examples.mem_copy.mem_copy_sim import MemCopyTB

    return MemCopyTB(name="xsi_tb", sim=Simulation(), mem_dwidth=width, jobs=XSI_JOBS)


def render_xsi_vectors(width: int = DEFAULT_MEM_DW) -> str:
    """Render ``mem_copy_vectors.h``'s contents **from the testbench graph**.

    Everything here is read off a real :class:`~examples.mem_copy.mem_copy_sim.MemCopyTB` — the same
    class the pysim golden runs — rather than restated: the words are the very ones its
    ``StreamDriver`` will send (the schema-packed command bursts); the offsets are its ``jobs``; the
    arena is the one its ``MemComponent`` declares.  So the XSI testbench and the pysim harness
    cannot describe different tests.

    Split from :func:`gen_xsi_vectors` so a test can compare the committed header against what the
    graph produces *now* without writing anything — that is what catches a schema or scenario change
    leaving the header stale, and it needs no toolchain.
    """
    from waveflow.build.composite_gen import render_vectors_h

    tb = make_xsi_tb(width)
    jobs = [CopyJob.coerce(j) for j in tb.jobs]

    return render_vectors_h(
        "mem_copy_vectors",
        scalars={
            "MEM_DW": width,
            # The arena the graph's MemComponent declares -- not a second, hand-picked number.
            "MEM_NW": int(tb.mem.nwords_tot),
            "N": int(jobs[0].n_words),
            "NUM_CMDS": len(jobs),
            # MemComplete{len, xfer_len, xfer_msg[8]} -> nwords_per_inst(64) == 5.  Introspected:
            # the s_done framing is the schema's business, not the testbench's.
            "DONE_WORDS": MemComplete.nwords_per_inst(width),
        },
        arrays={
            "SRC_W": ("int", [job.src_off for job in jobs]),
            "DST_W": ("int", [job.dst_off for job in jobs]),
        },
        note=("Derived from the MemCopyTB graph (examples/mem_copy/mem_copy_sim.py) built with\n"
              "XSI_JOBS -- the same class the pysim golden runs, instantiated with this scenario.\n"
              "The command words are no longer baked here: they are the burst bundle xsi/vectors/s_cmd\n"
              "that the harness loads in pre_sim -- written by write_mem_copy_xsi_bundles."),
    )


def write_mem_copy_xsi_bundles(xsi_dir: Path, width: int = DEFAULT_MEM_DW) -> None:
    """Write mem_copy's XSI input + golden **bundles** into ``<xsi_dir>/vectors/``.

    - ``vectors/s_cmd``  — the command stream (the StreamDriver's own bursts, schema-packed); the
      harness's ``AxisMaster`` loads it in ``pre_sim`` (the baked ``CMD_WORDS`` literal is gone);
    - ``vectors/mem_in`` — the source arena (each source region filled with its known pattern); the
      harness's memory loads it in ``pre_sim`` (``mem.load_segs``);
    - ``vectors/golden`` — the expected arena after the copy (each destination region = the source
      pattern); the TB compares the written destination regions against it.

    The whole scenario — commands *and* the memory pattern — is stated exactly once, in
    :class:`~examples.mem_copy.mem_copy_sim.MemCopyTB`.  This function only *serializes* it: each
    bundle is taken off the testbench (``cmd_words`` / ``mem_image`` / ``golden_image``), never
    recomputed, so the pysim run and the XSI run cannot diverge.
    """
    from waveflow.utils.burst_io import write_burst_bundle

    tb = make_xsi_tb(width)
    vdir = Path(xsi_dir) / "vectors"

    # Every bundle is taken FROM the testbench, never recomputed here: the commands are the words the
    # driver plays, and the arena/golden are read off the memory the pysim run seeds.  The scenario is
    # therefore stated exactly once -- in MemCopyTB -- so pysim and XSI cannot start from different bytes.
    write_burst_bundle(tb.cmd_words, vdir / "s_cmd")
    write_burst_bundle([tb.mem_image], vdir / "mem_in")
    write_burst_bundle([tb.golden_image], vdir / "golden")


def check_mem_copy_xsi_outputs(xsi_dir: Path, want_cycles: int, width: int = DEFAULT_MEM_DW) -> None:
    """Check mem_copy's XSI run from the dumped output bundles — the golden, in Python.

    The generated C++ main only runs and dumps; every check lives here.  Reads what the run wrote:
    ``vectors/out`` (the memory arena after the copy) and ``vectors/s_done`` (the completion stream +
    per-word arrival ``cycles.bin``).  Asserts:

    - **correctness** — every destination region equals the source pattern (a memcpy), vs
      ``vectors/golden``;
    - **completion** — one ``MemComplete`` per job, each echoing ``tx_id == j``;
    - **timing** — the cycle the *last* ``MemComplete`` landed (time-to-last-completion, NOT the loop
      bound) equals *want_cycles*.

    Raises ``AssertionError`` on any mismatch; a missing output bundle (a run that did not regenerate
    it) fails loudly on read.
    """
    from waveflow.hw.mem_stream import MemComplete
    from waveflow.utils.burst_io import read_burst_bundle

    vdir = Path(xsi_dir) / "vectors"
    tb = make_xsi_tb(width)
    jobs = [CopyJob.coerce(j) for j in tb.jobs]
    done_words = MemComplete.nwords_per_inst(width)

    # 1) Correctness: each destination region equals the golden (the source pattern, copied).
    out = read_burst_bundle(vdir / "out")[0]
    golden = read_burst_bundle(vdir / "golden")[0]
    for j, job in enumerate(jobs):
        dst, n = job.dst_off, job.n_words
        if not np.array_equal(out[dst:dst + n], golden[dst:dst + n]):
            bad = int(np.argmax(out[dst:dst + n] != golden[dst:dst + n]))
            raise AssertionError(
                f"mem_copy job {j} word {bad}: dst 0x{int(out[dst + bad]):016x} != "
                f"golden 0x{int(golden[dst + bad]):016x}")

    # 2) Completion: one MemComplete per job, tx_id echoed (xfer_msg[0] low 32b == j).
    s_done = read_burst_bundle(vdir / "s_done")[0]
    assert len(s_done) == len(jobs) * done_words, (
        f"mem_copy: s_done has {len(s_done)} words, expected {len(jobs)}*{done_words}")
    for j in range(len(jobs)):
        got_tx = int(s_done[j * done_words + 1]) & 0xFFFFFFFF
        assert got_tx == j, f"mem_copy job {j}: tx_id echo got {got_tx}, expected {j}"

    # 3) Timing: the cycle the LAST MemComplete word landed (cycle_of_word(n) == cycles[n-1]).  This is
    #    time-to-last-completion, not the run's loop bound; a change here is a real behaviour change.
    cycles = np.fromfile(vdir / "s_done" / "cycles.bin", dtype="<u8")
    done_cycle = int(cycles[len(jobs) * done_words - 1])
    assert done_cycle == want_cycles, (
        f"mem_copy completion cycle moved: got {done_cycle}, expected {want_cycles} — a real behaviour "
        f"change (regression, or an improvement worth re-recording).")


def gen_xsi_vectors(out_dir: Path = HERE, width: int = DEFAULT_MEM_DW) -> Path:
    """Emit ``xsi/mem_copy_vectors.h`` — the XSI testbench's scenario + its command words.

    The command words come from :meth:`CopyCmd.serialize`, the same call the pysim golden packs with.
    The testbench previously hand-packed ``src | dst<<32`` in C++, which was a second implementation
    of a schema rule with nothing checking the two agreed.  It could not simply *call* the schema:
    an XSI TB is host-compiled and cannot include ``copy_cmd.h`` (ap_int/hls_stream).  Emitting
    serialize()'s output resolves that without creating a second implementation.

    ``DONE_WORDS`` is introspected from :class:`MemComplete` rather than hardcoded to 5 for the same
    reason — it is ``nwords_per_inst(width)``, a schema fact, not a testbench constant.

    **Still duplicated, deliberately:** the memory pattern (``known_word``) is stated both here (in
    ``mem_copy_sim.py``'s ``run_copy``) and in the TB's C++.  Unifying it means emitting the whole
    arena image as data (16 x 128 words), which is a different trade than the command words: a
    drifted *pattern* still tests a copy, whereas a drifted *packing rule* sends malformed commands
    and makes the test meaningless.  The latter is what this function removes.
    """
    h = render_xsi_vectors(width)
    path = out_dir / "xsi" / "mem_copy_vectors.h"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(h, encoding="utf-8")
    return path


def gen_task_bodies(config: BuildConfig) -> None:
    """Generate ``mem_seq_task.h`` from :meth:`Sequencer.run_iter` into ``include/``, and its hook
    stubs at the example root (``impl_dir="."``) — never into ``include/`` or ``gen/``, which are
    regenerated.  Same shape as ``fir_build.py``'s sticky ``fir_pipeline_impl.tpp``."""
    inner = BuildDag()
    inner.add(SourceStep(artifact="mem_copy_source", path=HERE / "mem_copy.py"))
    inner.add(TaskBodyStep(name="gen_seq_task", comp_class=Sequencer,
                           source_artifact="mem_copy_source",
                           output_dir=INCLUDE_DIR, impl_dir="."))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"task-body gen failed: {failed}")


def generate(out_dir: Path = HERE, width: int = DEFAULT_MEM_DW) -> dict[str, Path]:
    """Generate headers + the MemCopy composite top .cpp + its csynth .tcl into *out_dir*."""
    from waveflow.build.elaborate import elaborate

    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config)
    gen_task_bodies(config)

    comp = elaborate(MemCopy, {"mem_dwidth": width}, name="mem_copy")
    spec = composite_top_spec(comp, width=width)

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    tcl = out_dir / f"{spec.top_name}.tcl"
    tcl.write_text(render_tcl(spec.top_name, extra_sources=SEQ_HOOK_SOURCES), encoding="utf-8")
    ports_h = out_dir / "xsi" / f"{spec.top_name}_ports.h"
    ports_h.parent.mkdir(parents=True, exist_ok=True)
    ports_h.write_text(render_ports_h(spec), encoding="utf-8")
    vec_h = gen_xsi_vectors(out_dir, width=width)
    # The testbench harness AND main: both derived from the TB graph.  The main is now just
    # construct-run-close (participants load/dump bundles), so it is generated too -- the only
    # hand-written half is Python: the scenario (write_mem_copy_xsi_bundles) and the golden checker.
    tb = make_xsi_tb(width)
    tb_spec = tb_top_spec(tb)
    harness_h = out_dir / "xsi" / f"{spec.top_name}_tb_harness.h"
    harness_h.write_text(render_tb_harness(tb_spec), encoding="utf-8")
    main_cpp = out_dir / "xsi" / f"{spec.top_name}_bfm_tb.cpp"
    main_cpp.write_text(render_tb_main(tb_spec, tb.n_cycles), encoding="utf-8")
    print(f"generated {cpp.relative_to(out_dir)} + {tcl.name} + xsi/{ports_h.name} "
          f"+ xsi/{vec_h.name} + xsi/{harness_h.name} + xsi/{main_cpp.name}")
    return {spec.top_name: cpp}


if __name__ == "__main__":
    generate()
