"""A test bench for systolic cores: a feeder and a drain around each, in one composite.

The boundary of a :class:`CoreBench` is plain 64-bit streams, three per core: ``s_cmd<i>`` (one
:class:`~waveflow.linalg.systolic.SystolicCmd` word per job), ``s_in<i>`` (``X``: ``A``, or ``Aᵀ``
for ``Aᴴ``, then the ``B`` matrices, as lane groups of ``ceil(2 W L / 64)`` words each, low word first; one burst per
matrix) and ``s_out<i>`` (each ``C`` the same way).  The C++ bodies are ``cpp/core_bench_tasks.h``.

The same jobs run three ways: the pysim composite (:func:`run_pysim`), a sequential Vitis
C-simulation of the generated top (:func:`render_seq_csim`: the top's channels declared in a
``main()``, every task fired once per job in dependency order, since Vitis 2024.1's threaded C-sim
of ``hls::stream_of_blocks`` hands a block over before it is written), and csynth of the top.
Expected outputs come from :func:`waveflow.linalg.matmul.matmul`.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.composite_gen import (
    GEN_DIR,
    INCLUDE_DIR,
    TopSpec,
    composite_top_spec,
    render_tcl,
    render_top,
)
from waveflow.build.streamutils import MemMgrStep
from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import (
    SobIFMaster,
    SobIFSlave,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
    StreamOfBlocksIF,
)
from waveflow.hw.mem_stream import KernelTask
from waveflow.linalg import matmul as mm
from waveflow.linalg.build import collect_parts, gen_linalg_headers
from waveflow.linalg.lanes import block_type, n_groups, pack_matrix, unpack_matrix
from waveflow.linalg.systolic import (
    MatmulOp,
    SystolicCmd,
    SystolicCore,
    command,
    stored_shape,
)
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle

CPP_DIR = Path(__file__).resolve().parent / "cpp"
BENCH_HEADER = "core_bench_tasks.h"
MASK64 = (1 << 64) - 1


def group_words(W: int, L: int) -> int:
    return -(-2 * int(W) * int(L) // 64)


def to_words64(groups, W: int, L: int) -> np.ndarray:
    nw = group_words(W, L)
    return np.array(
        [(int(g) >> (64 * w)) & MASK64 for g in groups for w in range(nw)], np.uint64
    )


def from_words64(words, W: int, L: int) -> list[int]:
    nw = group_words(W, L)
    words = [int(w) for w in words]
    return [
        sum(words[g * nw + w] << (64 * w) for w in range(nw))
        for g in range(len(words) // nw)
    ]


# --- the feeder and the drain ---------------------------------------------------------------------


@dataclass
class CoreFeed(FreeRunMod):
    """Test only: forwards each job's command, then lands ``X`` (``A``, or ``Aᵀ`` for ``Aᴴ``) and
    the ``B`` matrices in blocks."""

    cpp_kernel_name: ClassVar[str | None] = "core_feed"
    Mmax: HwParam[int] = 8
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    L: HwParam[int] = 4
    sob_depth: HwParam[int] = 2
    fid: HwParam[int] = 0
    wa: HwParam[int] = 12
    wb: HwParam[int] = 12
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        M, K, N, L = int(self.Mmax), int(self.Kmax), int(self.Nmax), int(self.L)
        mk = {"sim": self.sim, "bitwidth": 64, "has_tlast": False}
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", **mk)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", queue_size=1 << 16, **mk)
        self.core_cmd = StreamIFMaster(name=f"{self.name}_core_cmd", **mk)
        self.drain_cmd = StreamIFMaster(name=f"{self.name}_drain_cmd", **mk)
        self.a_blk = SobIFMaster(
            name=f"{self.name}_a_blk",
            sim=self.sim,
            element_type=block_type(int(self.wa), n_groups(M * K, L), L),
        )
        self.b_blk = SobIFMaster(
            name=f"{self.name}_b_blk",
            sim=self.sim,
            element_type=block_type(int(self.wb), K * N // L, L),
        )
        for ep in (
            self.s_cmd,
            self.s_in,
            self.core_cmd,
            self.drain_cmd,
            self.a_blk,
            self.b_blk,
        ):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "core_feed_task",
            BENCH_HEADER,
            ("s_cmd", "s_in", "core_cmd", "drain_cmd", "a_blk", "b_blk"),
            template_args=(
                int(self.Mmax),
                int(self.Kmax),
                int(self.Nmax),
                int(self.L),
                int(self.sob_depth),
                int(self.fid),
            ),
        )

    def _matrix(self, rows: int, cols: int, W: int):
        L = int(self.L)
        nw = group_words(W, L) * n_groups(rows * cols, L)
        words = yield from self.s_in.get(nwords_max=nw)
        re, im = unpack_matrix(from_words64(words, W, L), rows * cols, W, L)
        return re.reshape(rows, cols), im.reshape(rows, cols)

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.s_cmd.get_schema(SystolicCmd)
        words = np.asarray(cmd.serialize(word_bw=64), np.uint64)
        yield from self.core_cmd.write(words)
        yield from self.drain_cmd.write(words)
        m, k, n = int(cmd.m), int(cmd.k), int(cmd.n)
        a = yield from self._matrix(m, k, int(self.wa))  # X: A, or Aᵀ for Aᴴ
        blk = yield from self.a_blk.acquire_write()
        blk.payload = a
        yield from self.a_blk.commit_write(blk)
        for _ in range(int(cmd.nb)):
            b = yield from self._matrix(k, n, int(self.wb))
            blk = yield from self.b_blk.acquire_write()
            blk.payload = b
            yield from self.b_blk.commit_write(blk)


@dataclass
class CoreDrain(FreeRunMod):
    """Test only: writes each ``C`` of a job as lane-group words, one burst per matrix."""

    cpp_kernel_name: ClassVar[str | None] = "core_drain"
    Mmax: HwParam[int] = 8
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    L: HwParam[int] = 4
    sob_depth: HwParam[int] = 2
    fid: HwParam[int] = 0
    wc: HwParam[int] = 12
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        M, N, L = int(self.Mmax), int(self.Nmax), int(self.L)
        mk = {"sim": self.sim, "bitwidth": 64, "has_tlast": False}
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", **mk)
        self.c_blk = SobIFSlave(
            name=f"{self.name}_c_blk",
            sim=self.sim,
            element_type=block_type(int(self.wc), M * N // L, L),
        )
        self.s_out = StreamIFMaster(name=f"{self.name}_s_out", **mk)
        for ep in (self.s_cmd, self.c_blk, self.s_out):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "core_drain_task",
            BENCH_HEADER,
            ("s_cmd", "c_blk", "s_out"),
            template_args=(
                int(self.Mmax),
                int(self.Kmax),
                int(self.Nmax),
                int(self.L),
                int(self.sob_depth),
                int(self.fid),
            ),
        )

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.s_cmd.get_schema(SystolicCmd)
        W, L = int(self.wc), int(self.L)
        for _ in range(int(cmd.nb)):
            blk = yield from self.c_blk.acquire_read()
            cr, ci = blk.payload
            yield from self.s_out.write(to_words64(pack_matrix(cr, ci, W, L), W, L))
            yield from self.c_blk.release_read()


# --- the bench composite --------------------------------------------------------------------------


@dataclass
class CoreBench(FreeRunMod):
    """One feeder, core and drain per entry of ``cores`` (each a dict of ``SystolicCore`` fields)."""

    cpp_kernel_name: ClassVar[str | None] = "core_bench"
    cores: tuple = ()
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        boundary: list[str] = []
        self.core_list: list[SystolicCore] = []
        for i, spec in enumerate(self.cores):
            core = SystolicCore(name=f"core{i}", sim=self.sim, clk=self.clk, **spec)
            kw = {
                "sim": self.sim,
                "clk": self.clk,
                "Mmax": int(core.Mmax),
                "Kmax": int(core.Kmax),
                "Nmax": int(core.Nmax),
                "L": int(core.L),
                "sob_depth": int(core.sob_depth),
                "fid": core.traits.id,
            }
            feed = CoreFeed(name=f"feed{i}", wa=core.a.W, wb=core.b.W, **kw)
            drain = CoreDrain(name=f"drain{i}", wc=core.c.W, **kw)
            for c in (feed, core, drain):
                self.add_comp(c)
            self._stream(f"ccmd{i}", feed.core_cmd, core.cmd_in)
            self._stream(f"dcmd{i}", feed.drain_cmd, drain.s_cmd)
            self._blocks(f"a{i}", feed.a_blk, core.a_blk, int(core.sob_depth))
            self._blocks(f"b{i}", feed.b_blk, core.b_blk, int(core.sob_depth))
            self._blocks(f"c{i}", core.c_blk, drain.c_blk, int(core.sob_depth))
            setattr(self, f"s_cmd{i}", feed.s_cmd)
            setattr(self, f"s_in{i}", feed.s_in)
            setattr(self, f"s_out{i}", drain.s_out)
            boundary += [f"s_cmd{i}", f"s_in{i}", f"s_out{i}"]
            self.core_list.append(core)
        self.boundary = boundary
        self.extra_includes = ["hls_streamofblocks.h"]

    def _stream(self, name, master, slave) -> None:
        iface = StreamIF(name=f"{name}_if", sim=self.sim, clk=self.clk, bitwidth=64)
        iface.bind("master", master)
        iface.bind("slave", slave)
        self.add_if(iface)

    def _blocks(self, name, master, slave, depth: int) -> None:
        iface = StreamOfBlocksIF(
            name=f"{name}_if",
            sim=self.sim,
            clk=self.clk,
            element_type=master.element_type,
            depth=depth,
        )
        iface.bind("master", master)
        iface.bind("slave", slave)
        self.add_if(iface)


# --- jobs -----------------------------------------------------------------------------------------


@dataclass
class Job:
    """One job: the operation, the dimensions, ``A`` as stored and the ``B`` matrices."""

    op: int
    m: int
    k: int
    n: int
    a: tuple  # (re, im), shape stored_shape(op, m, k)
    bs: list  # [(re, im)], each k x n

    @property
    def nb(self) -> int:
        return len(self.bs)


def random_job(
    rng,
    core: SystolicCore,
    op: int,
    m: int,
    k: int,
    n: int,
    nb: int = 1,
    *,
    edge: bool = False,
) -> Job:
    """A job with random operands; ``edge`` puts every imaginary part of the first row (or column)
    of ``A`` and of ``B`` at ``-2^(W-1)``."""

    def draw(fmt, shape):
        lo, hi = -(1 << (fmt.W - 1)), (1 << (fmt.W - 1)) - 1
        return rng.integers(lo, hi + 1, size=shape), rng.integers(
            lo, hi + 1, size=shape
        )

    a = draw(core.a, stored_shape(op, m, k))
    bs = [draw(core.b, (k, n)) for _ in range(nb)]
    if edge:
        a[1][0, :] = -(1 << (core.a.W - 1))
        for _, bi in bs:
            bi[0, :] = -(1 << (core.b.W - 1))
    return Job(int(op), m, k, n, a, bs)


def expected(core: SystolicCore, job: Job) -> list[tuple[np.ndarray, np.ndarray]]:
    """The ``C`` matrices of a job, from the bit-exact model."""
    adjoint = job.op == MatmulOp.MUL_AH
    return [
        mm.matmul(
            *job.a, core.a, *b, core.b, core.c, adjoint=adjoint, form=int(core.form)
        )
        for b in job.bs
    ]


def job_streams(core: SystolicCore, job: Job) -> tuple[np.ndarray, list[np.ndarray]]:
    """The command word and the ``s_in`` bursts of a job."""
    L = int(core.L)
    cmd = np.asarray(
        command(job.op, job.m, job.k, job.n, job.nb).serialize(word_bw=64), np.uint64
    )
    x = (
        job.a if job.op == MatmulOp.MUL else (job.a[0].T, job.a[1].T)
    )  # the core takes Aᵀ
    bursts = [to_words64(pack_matrix(*x, core.a.W, L), core.a.W, L)]
    bursts += [to_words64(pack_matrix(*b, core.b.W, L), core.b.W, L) for b in job.bs]
    return cmd, bursts


def expected_words(core: SystolicCore, job: Job) -> list[np.ndarray]:
    L, W = int(core.L), core.c.W
    return [
        to_words64(pack_matrix(cr, ci, W, L), W, L) for cr, ci in expected(core, job)
    ]


def check_jobs(core: SystolicCore, jobs, got_bursts) -> None:
    """Every ``C`` the bench wrote equals the model's, in order."""
    want = [w for job in jobs for w in expected_words(core, job)]
    assert len(got_bursts) == len(
        want
    ), f"{len(got_bursts)} outputs, expected {len(want)}"
    for i, (g, w) in enumerate(zip(got_bursts, want, strict=True)):
        assert np.array_equal(np.asarray(g, np.uint64), w), f"output {i} differs"


# --- the three runs -------------------------------------------------------------------------------


def run_pysim(
    cores: tuple, jobs: list[list[Job]], root: Path
) -> list[list[np.ndarray]]:
    """Run the bench in pysim; returns each core's ``C`` bursts."""
    sim = Simulation()
    bench = CoreBench(name="bench", sim=sim, cores=cores)
    clk = bench.clk
    sinks = []
    for i, core in enumerate(bench.core_list):
        cmds, ins = [], []
        for job in jobs[i]:
            cmd, bursts = job_streams(core, job)
            cmds.append(cmd)
            ins += bursts
        write_burst_bundle(cmds, root / "vectors" / f"s_cmd{i}")
        write_burst_bundle(ins, root / "vectors" / f"s_in{i}")
        for port in ("s_cmd", "s_in"):
            drv = StreamDriver(sim=sim, bitwidth=64, in_bundle=f"vectors/{port}{i}")
            drv.root = root
            iface = StreamIF(name=f"tb_{port}{i}_if", sim=sim, clk=clk, bitwidth=64)
            iface.bind("master", drv.stream_ep)
            iface.bind("slave", getattr(bench, f"{port}{i}"))
        sink = StreamSink(sim=sim, bitwidth=64, queue_size=1 << 16)
        iface = StreamIF(name=f"tb_s_out{i}_if", sim=sim, clk=clk, bitwidth=64)
        iface.bind("master", getattr(bench, f"s_out{i}"))
        iface.bind("slave", sink.stream_ep)
        sinks.append(sink)
    sim.run_sim()
    return [list(s.words) for s in sinks]


def generate(cores: tuple, out_dir: Path, *, part: str, period_ns: float) -> TopSpec:
    """Headers, the top and its csynth TCL of a bench, in ``out_dir``."""
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    bench = CoreBench(name="core_bench", sim=Simulation(), cores=cores)
    parts = collect_parts(bench)
    inc = gen_linalg_headers(
        out_dir, parts.traits, parts.bodies, INCLUDE_DIR, schemas=parts.schemas
    )
    shutil.copyfile(CPP_DIR / BENCH_HEADER, inc / BENCH_HEADER)
    dag = BuildDag()
    dag.add(
        MemMgrStep(output_dir=INCLUDE_DIR)
    )  # every generated top includes memmgr.hpp
    dag.run(BuildConfig(root_dir=out_dir, params={}), force=True)
    spec = composite_top_spec(bench, width=64)
    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(
        render_tcl(spec.top_name, part=part, period_ns=period_ns), encoding="utf-8"
    )
    return spec


def render_seq_csim(spec: TopSpec, cores: tuple, jobs: list[list[Job]]) -> str:
    """A ``main()`` that declares the top's channels, fires each core's feeder, core and drain once
    per job, and prints every word of every ``s_out<i>``."""
    bench = CoreBench(name="core_bench", sim=Simulation(), cores=cores)
    top_cpp = render_top(spec)
    includes = [ln for ln in top_cpp.splitlines() if ln.startswith("#include")]
    decls = [p.decl.replace("&", "") + ";" for p in spec.ports]
    decls += [
        ln.strip().replace("hls_thread_local ", "")
        for ch in spec.channels
        for ln in ch.decl.splitlines()
        if "#pragma" not in ln
    ]
    data, feeds = [], []
    for i, core in enumerate(bench.core_list):
        cmds, ins = [], []
        for job in jobs[i]:
            cmd, bursts = job_streams(core, job)
            cmds.append(cmd)
            ins += bursts
        for port, words in (
            (f"s_cmd{i}", np.concatenate(cmds)),
            (f"s_in{i}", np.concatenate(ins)),
        ):
            vals = ", ".join(f"{int(w)}ULL" for w in words)
            data.append(f"static const unsigned long long D_{port}[] = {{{vals}}};")
            feeds.append(
                f"    for (unsigned i = 0; i < {len(words)}; ++i) "
                f"{port}.write(ap_uint<64>(D_{port}[i]));"
            )
    calls = []
    for i in range(len(cores)):
        tasks = spec.tasks[3 * i : 3 * i + 3]
        body = " ".join(
            f"{t.task_fn}<{', '.join(str(a) for a in t.template_args)}>({', '.join(t.args)});"
            for t in tasks
        )
        calls.append(f"    for (int j = 0; j < {len(jobs[i])}; ++j) {{ {body} }}")
    dumps = [
        f'    fprintf(f, "P {i}\\n"); while (!s_out{i}.empty()) '
        f'fprintf(f, "%s\\n", s_out{i}.read().to_string(16).c_str());'
        for i in range(len(cores))
    ]
    nl = "\n"
    return f"""// GENERATED by tests/linalg/_core_bench.py -- sequential C-simulation of {spec.top_name}.
{nl.join(includes)}
#include <cstdio>
{nl.join(data)}
int main(int argc, char** argv) {{
    FILE* f = fopen(argv[1], "w");
{nl.join("    " + d for d in decls)}
{nl.join(feeds)}
{nl.join(calls)}
{nl.join(dumps)}
    fprintf(f, "CSIM_DONE\\n");
    fclose(f);
    return 0;
}}
"""


def parse_seq_csim(
    text: str, cores: tuple, jobs: list[list[Job]]
) -> list[list[np.ndarray]]:
    """The ``C`` bursts of each core from the C-simulation's output."""
    assert "CSIM_DONE" in text, text[-500:]
    words: list[list[int]] = [[] for _ in cores]
    cur = -1
    for line in text.splitlines():
        if line.startswith("P "):
            cur = int(line[2:])
        elif line and line != "CSIM_DONE":
            words[cur].append(int(line, 16))
    bench = CoreBench(name="core_bench", sim=Simulation(), cores=cores)
    out = []
    for i, core in enumerate(bench.core_list):
        sizes = [len(w) for job in jobs[i] for w in expected_words(core, job)]
        assert len(words[i]) == sum(
            sizes
        ), f"core {i}: {len(words[i])} words, expected {sum(sizes)}"
        bursts, pos = [], 0
        for s in sizes:
            bursts.append(np.array(words[i][pos : pos + s], np.uint64))
            pos += s
        out.append(bursts)
    return out
