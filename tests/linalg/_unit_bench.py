"""A test bench for the standalone systolic unit: fed from memory, its replies landed in memory.

The DUT (:class:`UnitBench`) is a :class:`~waveflow.linalg.systolic.SystolicUnit` between the
framework's in-band memory streams::

    s_req -> UnitReqFramer -> MemRStream (in-band) -> SystolicUnit -> UnitReplyFramer
          -> MemWStream (in-band, s_done)

``s_req`` carries, per job, a :class:`UnitBenchReq` (where ``A`` and ``B`` are, in words) and the
request header.  The reply framer writes each reply's payload at the word address in its tag and
the memory writer echoes the reply header on ``s_done``.  The framers are test-only
(``cpp/unit_bench_tasks.h``).

The same scenario runs in pysim (:meth:`UnitBenchSim.run`) and at RTL through XSI
(:func:`generate`, :func:`csynth`, :func:`generate_tb`, :func:`run_xsi`, :func:`check_xsi`), built
the way the framework's composites are: ``composite_gen`` for the top and the testbench harness.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.build.build import BuildConfig
from waveflow.build.composite_gen import (
    GEN_DIR,
    INCLUDE_DIR,
    TopSpec,
    composite_top_spec,
    render_ports_h,
    render_rtl_f,
    render_tb_harness,
    render_tb_main,
    render_tcl,
    render_top,
    render_vectors_h,
    tb_top_spec,
)
from waveflow.build.streamutils import MemMgrStep, MemStreamStep, XsiHarnessStep
from waveflow.build.trace_steps import xsi_runner_cmd
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.dataschema import DataList, DataSchemaStep
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask, MemRCmd, MemRStream, MemWCmd, MemWStream
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemoryMod, MemSeg
from waveflow.linalg import matmul as mm
from waveflow.linalg.build import collect_parts, linalg_headers_dag
from waveflow.linalg.lanes import to_words
from waveflow.linalg.message import U32, LinalgHeader, Status, header, header_words
from waveflow.linalg.systolic import (
    MatmulOp,
    SystolicUnit,
    reply_words,
    request_status,
    request_words,
    stored_shape,
)
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import read_burst_bundle, write_burst_bundle

CPP_DIR = Path(__file__).resolve().parent / "cpp"
BENCH_HEADER = "unit_bench_tasks.h"
TOP = "unit_bench"
#: The parameters of the unit, in the order the bench passes them on.
UNIT_KEYS = (
    "word_bits", "Mmax", "Kmax", "Nmax", "L", "R", "C", "form", "sob_depth", "lane_bits",
    "a", "b", "c",
)  # fmt: skip


class UnitBenchReq(DataList):
    """Test only: where a job's ``A`` and ``B`` are in memory, in words."""

    include_filename: ClassVar[str | None] = "unit_bench_req.h"
    elements: ClassVar[dict] = {
        "a_off": {"schema": U32, "description": "A's first word"},
        "a_len": {"schema": U32, "description": "A's words"},
        "b_off": {"schema": U32, "description": "B's first word"},
        "b_len": {"schema": U32, "description": "B's words"},
    }


# --- the framers ----------------------------------------------------------------------------------


@dataclass
class UnitReqFramer(FreeRunMod):
    """Test only: ``UnitBenchReq`` + header -> ``MemRCmd(A, fwd 1) | header | MemRCmd(B, fwd 0)``."""

    cpp_kernel_name: ClassVar[str | None] = "unit_req"
    word_bits: HwParam[int] = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.word_bits)
        self.s_req = StreamIFSlave(
            name=f"{self.name}_s_req", sim=self.sim, bitwidth=w, has_tlast=False
        )
        self.cmd_out = StreamIFMaster(
            name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w, has_tlast=True
        )
        for ep in (self.s_req, self.cmd_out):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "unit_req_task", BENCH_HEADER, ("s_req", "cmd_out"), (int(self.word_bits),)
        )

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.word_bits)
        q = yield from self.s_req.get_schema(UnitBenchReq)
        h = yield from self.s_req.get_schema(LinalgHeader)
        frames = (
            MemRCmd(addr=int(q.a_off), len=int(q.a_len), fwd_bursts=1),
            h,
            MemRCmd(addr=int(q.b_off), len=int(q.b_len), fwd_bursts=0),
        )
        for fr in frames:
            yield from self.cmd_out.write(
                np.asarray(fr.serialize(word_bw=w), np.uint64)
            )


@dataclass
class UnitReplyFramer(FreeRunMod):
    """Test only: a reply -> ``MemWCmd(0, 0, fwd 0) | MemWCmd(tag, length, fwd 1) | header |
    payload``.  The empty write makes the writer fire as often as the reader (twice per job), as
    the generated top's m_axi pointer FIFOs require (``cpp/unit_bench_tasks.h``)."""

    cpp_kernel_name: ClassVar[str | None] = "unit_reply"
    word_bits: HwParam[int] = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.word_bits)
        self.s_in = StreamIFSlave(
            name=f"{self.name}_s_in", sim=self.sim, bitwidth=w, has_tlast=True
        )
        self.s_out = StreamIFMaster(
            name=f"{self.name}_s_out", sim=self.sim, bitwidth=w, has_tlast=True
        )
        for ep in (self.s_in, self.s_out):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "unit_reply_task", BENCH_HEADER, ("s_in", "s_out"), (int(self.word_bits),)
        )

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.word_bits)
        h = yield from self.s_in.get_schema(LinalgHeader)
        empty = MemWCmd(addr=0, len=0, fwd_bursts=0)
        yield from self.s_out.write(np.asarray(empty.serialize(word_bw=w), np.uint64))
        cmd = MemWCmd(addr=int(h.tag), len=int(h.length), fwd_bursts=1)
        yield from self.s_out.write(np.asarray(cmd.serialize(word_bw=w), np.uint64))
        yield from self.s_out.write(np.asarray(h.serialize(word_bw=w), np.uint64))
        if int(h.length):
            payload = yield from self.s_in.get()
            yield from self.s_out.write(np.asarray(payload))


# --- the DUT and the testbench graph --------------------------------------------------------------


@dataclass
class UnitBench(FreeRunMod):
    """The DUT: the unit between the in-band memory streams (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = TOP
    unit: tuple = ()  # (key, value) pairs of SystolicUnit fields
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        p = dict(self.unit)
        w = int(p["word_bits"])
        self.req = UnitReqFramer(name="req", sim=self.sim, word_bits=w, clk=self.clk)
        self.rstream = MemRStream(
            name="memr", sim=self.sim, mem_dwidth=w, inband=True, clk=self.clk
        )
        self.dut_unit = SystolicUnit(name="unit", sim=self.sim, clk=self.clk, **p)
        self.reply = UnitReplyFramer(
            name="reply", sim=self.sim, word_bits=w, clk=self.clk
        )
        self.wstream = MemWStream(
            name="memw",
            sim=self.sim,
            mem_dwidth=w,
            inband=True,
            emit_done=True,
            clk=self.clk,
        )
        for c in (self.req, self.rstream, self.dut_unit, self.reply, self.wstream):
            self.add_comp(c)
        for name, m, s in (
            ("rd_cmd", self.req.cmd_out, self.rstream.s_cmd),
            ("rd_data", self.rstream.m_out, self.dut_unit.s_in),
            ("reply", self.dut_unit.s_out, self.reply.s_in),
            ("wr", self.reply.s_out, self.wstream.s_in),
        ):
            iface = StreamIF(
                name=f"{name}_if", sim=self.sim, clk=self.clk, bitwidth=w, framed=True
            )
            iface.bind("master", m)
            iface.bind("slave", s)
            self.add_if(iface)
        self.boundary = ["s_req", "m_in", "m_out", "s_done"]
        self.s_req = self.req.s_req
        self.m_in = self.rstream.m_mem
        self.m_out = self.wstream.m_mem
        self.s_done = self.wstream.s_done
        self.extra_includes = ["hls_streamofblocks.h"]


@dataclass
class UnitBenchTB(FreeRunMod):
    """The testbench graph: a driver on ``s_req``, a sink on ``s_done``, one memory arena behind
    both ``m_axi`` bundles, and the DUT."""

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    unit: tuple = ()
    arena_words: int = 1024
    n_cycles: int = 100_000
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(dict(self.unit)["word_bits"])
        self.mem = MemoryMod(
            name=f"{self.name}_mem",
            sim=self.sim,
            inline=False,
            clk=self.clk,
            word_size=w,
            addr_size=32,
            nwords_tot=int(self.arena_words),
        )
        self.mem.alloc(int(self.mem.nwords_tot))
        self.mem.load_segs = [MemSeg(0, 0, "vectors/mem_in")]
        self.mem.dump_segs = [MemSeg(0, int(self.mem.nwords_tot), "vectors/out")]
        self.dut = UnitBench(
            name=f"{self.name}_dut", sim=self.sim, unit=self.unit, clk=self.clk
        )
        self.driver = StreamDriver(sim=self.sim, bitwidth=w, in_bundle="vectors/s_req")
        self.done_sink = StreamSink(
            sim=self.sim, bitwidth=w, out_bundle="vectors/s_done", has_tlast=True
        )
        for c in (self.dut, self.driver, self.done_sink, self.mem):
            self.add_comp(c)
        for name, m, s in (
            ("req", self.driver.stream_ep, self.dut.s_req),
            ("done", self.dut.s_done, self.done_sink.stream_ep),
        ):
            iface = StreamIF(
                name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=w
            )
            iface.bind(ep_name="master", endpoint=m)
            iface.bind(ep_name="slave", endpoint=s)
            self.add_if(iface)
        xbar = AXIMMCrossBarIF(
            name=f"{self.name}_xbar",
            sim=self.sim,
            clk=self.clk,
            nports_master=2,
            nports_slave=1,
            bitwidth=w,
        )
        xbar.bind("master_0", self.dut.m_in)
        xbar.bind("master_1", self.dut.m_out)
        xbar.bind("slave_0", self.mem.s_mm)
        self.add_if(xbar)
        assign_address_ranges([self.mem.s_mm], [(0, int(self.arena_words) * (w // 8))])


# --- jobs and the scenario ------------------------------------------------------------------------


@dataclass
class UnitJob:
    """One request: the operands as the job states them (``A`` ``m × k``, or ``k × m`` for
    ``Aᴴ``; ``B`` ``k × n``) and, for a request meant to be rejected, header fields that differ
    from what the operands imply (the payload sent is always the operands)."""

    op: int
    m: int
    k: int
    n: int
    a: tuple
    b: tuple
    override: dict = field(default_factory=dict)


def random_job(
    rng,
    p: dict,
    op: int,
    m: int,
    k: int,
    n: int,
    *,
    edge: bool = False,
    override: dict | None = None,
) -> UnitJob:
    def draw(fmt, shape):
        lo, hi = -(1 << (fmt.W - 1)), (1 << (fmt.W - 1)) - 1
        return rng.integers(lo, hi + 1, size=shape), rng.integers(
            lo, hi + 1, size=shape
        )

    a = draw(p["a"], stored_shape(op, m, k))
    b = draw(p["b"], (k, n))
    if edge:
        a[1][0, :] = -(1 << (p["a"].W - 1))
        b[1][0, :] = -(1 << (p["b"].W - 1))
    return UnitJob(int(op), m, k, n, a, b, dict(override or {}))


class UnitBenchSim:
    """The procedure around a :class:`UnitBenchTB`: lay the jobs out in memory, write the
    scenario, run, and check every reply against the model."""

    def __init__(
        self, unit: dict, jobs: list[UnitJob], *, n_cycles: int = 200_000
    ) -> None:
        self.p = dict(unit)
        self.w = int(self.p["word_bits"])
        self.lb = int(self.p["lane_bits"])
        self.jobs = list(jobs)
        self.layout = (
            []
        )  # per job: (a_off, a_len, b_off, b_len, out_off, request header)
        cur = 0
        for job in self.jobs:
            a_words = to_words(*job.a, self.p["a"], self.lb, self.w)
            b_words = to_words(*job.b, self.p["b"], self.lb, self.w)
            a_off, b_off = cur, cur + len(a_words)
            out = b_off + len(b_words)
            cur = out + reply_words(job.m, job.n, self.lb, self.w)
            fields = {
                "op": job.op, "m": job.m, "k": job.k, "n": job.n, "nfollow": 0,
                "length": request_words(job.m, job.k, job.n, self.lb, self.w),
            }  # fmt: skip
            fields.update(job.override)
            tag = fields.pop("tag", out)
            h = header(tag, **fields)
            self.layout.append(
                (a_off, len(a_words), b_off, len(b_words), out, h, a_words, b_words)
            )
        self.arena_words = cur + 16
        self.tb = UnitBenchTB(
            name="tb",
            sim=Simulation(),
            unit=tuple(self.p.items()),
            arena_words=self.arena_words,
            n_cycles=n_cycles,
        )

    def status(self, h: LinalgHeader) -> Status:
        keys = ("Mmax", "Kmax", "Nmax", "L", "R", "C")
        return request_status(
            h, **{k: int(self.p[k]) for k in keys}, lane_bits=self.lb, word_bits=self.w
        )

    def scenario(self) -> dict:
        mem_in = np.zeros(self.arena_words, np.uint64)
        req, expected, done = [], [], []
        for job, (a_off, a_len, b_off, b_len, out, h, aw, bw) in zip(
            self.jobs, self.layout, strict=True
        ):
            mem_in[a_off : a_off + a_len] = aw
            mem_in[b_off : b_off + b_len] = bw
            q = UnitBenchReq()
            q.a_off, q.a_len, q.b_off, q.b_len = a_off, a_len, b_off, b_len
            req.append(np.asarray(q.serialize(word_bw=self.w), np.uint64))
            req.append(np.asarray(h.serialize(word_bw=self.w), np.uint64))
            st = self.status(h)
            if st == Status.OK:
                cr, ci = mm.matmul(
                    *job.a, self.p["a"], *job.b, self.p["b"], self.p["c"],
                    adjoint=job.op == MatmulOp.MUL_AH, form=int(self.p["form"]),
                )  # fmt: skip
                expected.append(
                    (int(h.tag), to_words(cr, ci, self.p["c"], self.lb, self.w))
                )
            n_out = reply_words(job.m, job.n, self.lb, self.w) if st == Status.OK else 0
            r = header(
                int(h.tag),
                int(h.op),
                m=int(h.m),
                k=int(h.k),
                n=int(h.n),
                length=n_out,
                status=st,
            )
            done.append(np.asarray(r.serialize(word_bw=self.w), np.uint64))
        return {"req": req, "mem_in": mem_in, "expected": expected, "done": done}

    def write_scenario(self, root: Path) -> dict:
        sc = self.scenario()
        root = Path(root)
        write_burst_bundle(sc["req"], root / "vectors" / "s_req")
        write_burst_bundle([sc["mem_in"]], root / "vectors" / "mem_in")
        self.tb.driver.root = root
        self.tb.mem.root = root
        return sc

    def run(self) -> dict:
        """Run in pysim and check; returns the scenario."""
        with tempfile.TemporaryDirectory() as root:
            sc = self.write_scenario(Path(root))
            self.tb.sim.run_sim()
        bpw = self.w // 8
        for j, (off, exp) in enumerate(sc["expected"]):
            got = self.tb.mem._mem.read(off * bpw, len(exp)).astype(np.uint64)
            assert np.array_equal(got, exp), f"pysim: reply {j} differs at word {off}"
        got_done = [np.asarray(b, np.uint64) for b in self.tb.done_sink.words]
        assert len(got_done) == len(
            sc["done"]
        ), f"{len(got_done)} replies, {len(sc['done'])} jobs"
        for j, (g, want) in enumerate(zip(got_done, sc["done"], strict=True)):
            assert np.array_equal(g, want), f"pysim: reply header {j} differs"
        return sc


# --- the RTL flow ---------------------------------------------------------------------------------


def generate(unit: dict, out_dir: Path, *, part: str, period_ns: float) -> TopSpec:
    """Headers, the top, its csynth TCL and the XSI port map of a bench, in ``out_dir``."""
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    dut = UnitBench(name=TOP, sim=Simulation(), unit=tuple(unit.items()))
    parts = collect_parts(dut)
    words = sorted({32, 64, int(unit["word_bits"])})
    dag = linalg_headers_dag(
        parts.traits, parts.bodies, INCLUDE_DIR, words, (*parts.schemas, UnitBenchReq)
    )
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
    dag.add(MemStreamStep(output_dir=INCLUDE_DIR))
    for cls in (MemRCmd, MemWCmd):
        dag.add(
            DataSchemaStep(
                cls, word_bw_supported=words, include_dir=INCLUDE_DIR, framed=True
            )
        )
    dag.add(XsiHarnessStep(output_dir="xsi"))
    results = dag.run(BuildConfig(root_dir=out_dir, params={}), force=True)
    failed = {n: r.message for n, r in results.items() if not r.success}
    assert not failed, f"header generation failed: {failed}"
    shutil.copyfile(CPP_DIR / BENCH_HEADER, out_dir / INCLUDE_DIR / BENCH_HEADER)
    spec = composite_top_spec(dut, width=int(unit["word_bits"]))
    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, part=part, period_ns=period_ns), encoding="utf-8"
    )
    xsi = out_dir / "xsi"
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(
        render_ports_h(spec), encoding="utf-8"
    )
    return spec


def csynth(out_dir: Path) -> str:
    """csynth of the generated top, then the RTL file list for XSI; returns the log."""
    out_dir = Path(out_dir)
    run = toolchain.run_vitis_hls(
        out_dir / f"{TOP}.tcl", work_dir=out_dir, capture_output=True
    )
    log = (run.stdout or "") + (run.stderr or "")
    (out_dir / "csynth.log").write_text(log, encoding="utf-8")
    report = out_dir / f"{TOP}_proj" / "solution1" / "syn" / "report" / "csynth.xml"
    assert run.returncode == 0 and report.is_file(), log[-3000:]
    # No source stamp: nothing here reads one, and tests/build/test_rtl_digest.py requires every
    # call under tests/ to pass stamp_sources=False.
    (out_dir / "xsi" / f"rtl_{TOP}.f").write_text(
        render_rtl_f(TOP, out_dir, stamp_sources=False), encoding="utf-8"
    )
    return log


def generate_tb(out_dir: Path, sim: UnitBenchSim) -> dict:
    """The XSI testbench of the bench's top, and the scenario's vector bundles."""
    xsi = Path(out_dir) / "xsi"
    tb = sim.tb
    vectors = render_vectors_h(
        f"{TOP}_vectors",
        scalars={
            "MEM_DW": sim.w,
            "MEM_NW": int(tb.mem.nwords_tot),
            "NUM_JOBS": len(sim.jobs),
        },
        note="Derived from the testbench graph in tests/linalg/_unit_bench.py.",
    )
    (xsi / f"{TOP}_vectors.h").write_text(vectors, encoding="utf-8")
    spec = tb_top_spec(tb)
    (xsi / f"{TOP}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{TOP}_bfm_tb.cpp").write_text(
        render_tb_main(spec, tb.n_cycles), encoding="utf-8"
    )
    sc = sim.write_scenario(xsi)
    for name in ("out", "s_done"):  # never let a previous run's dump pass for this one
        d = xsi / "vectors" / name
        if d.exists():
            for f in d.iterdir():
                f.unlink()
    return sc


def run_xsi(out_dir: Path) -> subprocess.CompletedProcess:
    xsi = Path(out_dir) / "xsi"
    return subprocess.run(
        xsi_runner_cmd(TOP, f"{TOP}_bfm_tb"),
        cwd=xsi,
        capture_output=True,
        text=True,
        check=False,
    )


def check_xsi(out_dir: Path, sc: dict, word_bits: int) -> list[int]:
    """Every reply's payload in the memory dump and every reply header on ``s_done`` equal the
    model; returns the cycle each reply header's last word arrived."""
    vdir = Path(out_dir) / "xsi" / "vectors"
    out = np.asarray(read_burst_bundle(vdir / "out")[0], np.uint64)
    for j, (off, exp) in enumerate(sc["expected"]):
        got = out[off : off + len(exp)]
        assert np.array_equal(got, exp), f"RTL: reply {j} differs at word {off}"
    done = np.asarray(read_burst_bundle(vdir / "s_done")[0], np.uint64)
    want = np.concatenate(sc["done"])
    assert len(done) == len(want), f"s_done has {len(done)} words, expected {len(want)}"
    assert np.array_equal(done, want), "RTL: reply headers differ"
    cycles = np.fromfile(vdir / "s_done" / "cycles.bin", dtype="<u8")
    hw = header_words(word_bits)
    return [int(cycles[(j + 1) * hw - 1]) for j in range(len(sc["done"]))]
