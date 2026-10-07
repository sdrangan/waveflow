"""A test bench for CG vector cores: a feeder and a drain around each, in one composite.

The boundary of a :class:`CgCoreBench` is plain 64-bit streams, three per core: ``s_cmd<i>`` (one
:class:`~waveflow.linalg.cg_vector.CgVectorCmd` word per job), ``s_in<i>`` (``B``, then the job's
``nit`` matrices ``S``, as lane groups of ``ceil(2 W L / 64)`` words each, low word first; one
burst per matrix) and ``s_out<i>`` (the ``nit`` matrices ``P`` and then ``X``, the same way).  The
C++ bodies are ``cpp/cg_core_bench_tasks.h``.

``S`` comes from the model: a job carries its ``A``, and :func:`expected` runs
:func:`waveflow.linalg.cg.cg_init`, then per iteration :func:`~waveflow.linalg.cg.mm_step` and
:func:`~waveflow.linalg.cg.vec_step`, recording every ``S``, ``P`` and the final ``X``.  The same
jobs run three ways: the pysim composite (:func:`run_pysim`), a sequential Vitis C-simulation of the
generated top (:func:`render_seq_csim`, every task fired once per job in dependency order, as
``_core_bench`` does), and csynth of the top.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from tests.linalg._core_bench import from_words64, group_words, to_words64
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
from waveflow.linalg import cg
from waveflow.linalg.build import collect_parts, gen_linalg_headers
from waveflow.linalg.cg_vector import CgVectorCmd, CgVectorCore, command
from waveflow.linalg.lanes import block_type, pack_matrix, unpack_matrix
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils import fixputils as fx
from waveflow.utils.burst_io import write_burst_bundle
from waveflow.utils.fixputils import Format, OMode, QMode

CPP_DIR = Path(__file__).resolve().parent / "cpp"
BENCH_HEADER = "cg_core_bench_tasks.h"


# --- formats --------------------------------------------------------------------------------------

#: Integer bits per register, sign included: the parameterization the CG example studied (its
#: ``INT_BITS``), written out because the tests cannot import the example.
INT_BITS = {
    "A": 3, "B": 4, "P": 4, "R": 4, "S": 5, "X": 3,
    "ps": 10, "rz": 9, "alpha": 5, "beta": 3,
}  # fmt: skip


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


def cg_formats(W: int, g: int = 0, g_div: int = 6) -> cg.CgFormats:
    """Vectors and ``alpha``, ``beta`` ``W`` bits wide, ``ps`` and ``rz`` ``W + g``: the example's
    sweep formats (``W12g8`` is ``cg_formats(12, 8)``)."""
    regs = {
        v: reg(W, INT_BITS[v]) for v in ("A", "B", "P", "R", "S", "X", "alpha", "beta")
    }
    regs |= {s: reg(W + g, INT_BITS[s]) for s in ("ps", "rz")}
    return cg.CgFormats(**regs, g_div=g_div)


def stress_formats() -> cg.CgFormats:
    """The example's saturation stress set: 12-bit vectors, 14-bit scalars, and ``alpha`` and
    ``beta`` with 2 and 1 integer bits, so they saturate."""
    regs = {v: reg(12, INT_BITS[v]) for v in ("A", "B", "P", "R", "S", "X")}
    regs |= {
        "ps": reg(14, 10),
        "rz": reg(14, 9),
        "alpha": reg(14, 2),
        "beta": reg(14, 1),
    }
    return cg.CgFormats(**regs, g_div=2)


# --- the feeder and the drain ---------------------------------------------------------------------


@dataclass
class CgFeed(FreeRunMod):
    """Test only: forwards each job's command, then lands ``B`` and the ``nit`` matrices ``S``."""

    cpp_kernel_name: ClassVar[str | None] = "cg_feed"
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    nitmax: HwParam[int] = 8
    L: HwParam[int] = 4
    sob_depth: HwParam[int] = 2
    fid: HwParam[int] = 0
    wb: HwParam[int] = 12
    ws: HwParam[int] = 12
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        G = int(self.Kmax) * int(self.Nmax) // int(self.L)
        mk = {"sim": self.sim, "bitwidth": 64, "has_tlast": False}
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", **mk)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", queue_size=1 << 16, **mk)
        self.core_cmd = StreamIFMaster(name=f"{self.name}_core_cmd", **mk)
        self.drain_cmd = StreamIFMaster(name=f"{self.name}_drain_cmd", **mk)
        self.b_blk = SobIFMaster(
            name=f"{self.name}_b_blk",
            sim=self.sim,
            element_type=block_type(int(self.wb), G, int(self.L)),
        )
        self.s_blk = SobIFMaster(
            name=f"{self.name}_s_blk",
            sim=self.sim,
            element_type=block_type(int(self.ws), G, int(self.L)),
        )
        for ep in (self.s_cmd, self.s_in, self.core_cmd, self.drain_cmd):
            self.add_endpoint(ep)
        self.add_endpoint(self.b_blk)
        self.add_endpoint(self.s_blk)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_feed_task",
            BENCH_HEADER,
            ("s_cmd", "s_in", "core_cmd", "drain_cmd", "b_blk", "s_blk"),
            template_args=(
                int(self.Kmax),
                int(self.Nmax),
                int(self.nitmax),
                int(self.L),
                int(self.sob_depth),
                int(self.fid),
            ),
        )

    def _matrix(self, rows: int, cols: int, W: int):
        L = int(self.L)
        words = yield from self.s_in.get(
            nwords_max=group_words(W, L) * rows * cols // L
        )
        re, im = unpack_matrix(from_words64(words, W, L), rows * cols, W, L)
        return re.reshape(rows, cols), im.reshape(rows, cols)

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.s_cmd.get_schema(CgVectorCmd)
        words = np.asarray(cmd.serialize(word_bw=64), np.uint64)
        yield from self.core_cmd.write(words)
        yield from self.drain_cmd.write(words)
        nit, k, n = int(cmd.nit), int(cmd.k), int(cmd.n)
        for ep, W in [(self.b_blk, int(self.wb))] + [(self.s_blk, int(self.ws))] * nit:
            m = yield from self._matrix(k, n, W)
            blk = yield from ep.acquire_write()
            blk.payload = m
            yield from ep.commit_write(blk)


@dataclass
class CgDrain(FreeRunMod):
    """Test only: writes the ``nit`` matrices ``P`` and then ``X`` of each job as lane-group words."""

    cpp_kernel_name: ClassVar[str | None] = "cg_drain"
    Kmax: HwParam[int] = 8
    Nmax: HwParam[int] = 32
    nitmax: HwParam[int] = 8
    L: HwParam[int] = 4
    sob_depth: HwParam[int] = 2
    fid: HwParam[int] = 0
    wp: HwParam[int] = 12
    wx: HwParam[int] = 12
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        G = int(self.Kmax) * int(self.Nmax) // int(self.L)
        mk = {"sim": self.sim, "bitwidth": 64, "has_tlast": False}
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", **mk)
        self.p_blk = SobIFSlave(
            name=f"{self.name}_p_blk",
            sim=self.sim,
            element_type=block_type(int(self.wp), G, int(self.L)),
        )
        self.x_blk = SobIFSlave(
            name=f"{self.name}_x_blk",
            sim=self.sim,
            element_type=block_type(int(self.wx), G, int(self.L)),
        )
        self.s_out = StreamIFMaster(name=f"{self.name}_s_out", **mk)
        for ep in (self.s_cmd, self.p_blk, self.x_blk, self.s_out):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask(
            "cg_drain_task",
            BENCH_HEADER,
            ("s_cmd", "p_blk", "x_blk", "s_out"),
            template_args=(
                int(self.Kmax),
                int(self.Nmax),
                int(self.nitmax),
                int(self.L),
                int(self.sob_depth),
                int(self.fid),
            ),
        )

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.s_cmd.get_schema(CgVectorCmd)
        L = int(self.L)
        for ep, W in [(self.p_blk, int(self.wp))] * int(cmd.nit) + [
            (self.x_blk, int(self.wx))
        ]:
            blk = yield from ep.acquire_read()
            re, im = blk.payload
            yield from self.s_out.write(to_words64(pack_matrix(re, im, W, L), W, L))
            yield from ep.release_read()


# --- the bench composite --------------------------------------------------------------------------


@dataclass
class CgCoreBench(FreeRunMod):
    """One feeder, core and drain per entry of ``cores`` (each a dict of ``CgVectorCore`` fields)."""

    cpp_kernel_name: ClassVar[str | None] = "cg_core_bench"
    cores: tuple = ()
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        boundary: list[str] = []
        self.core_list: list[CgVectorCore] = []
        for i, spec in enumerate(self.cores):
            core = CgVectorCore(name=f"core{i}", sim=self.sim, clk=self.clk, **spec)
            f = core.formats
            kw = {
                "sim": self.sim,
                "clk": self.clk,
                "Kmax": int(core.Kmax),
                "Nmax": int(core.Nmax),
                "nitmax": int(core.nitmax),
                "L": int(core.L),
                "sob_depth": int(core.sob_depth),
                "fid": core.traits.id,
            }
            feed = CgFeed(name=f"feed{i}", wb=f.B.W, ws=f.S.W, **kw)
            drain = CgDrain(name=f"drain{i}", wp=f.P.W, wx=f.X.W, **kw)
            for c in (feed, core, drain):
                self.add_comp(c)
            self._stream(f"ccmd{i}", feed.core_cmd, core.cmd_in)
            self._stream(f"dcmd{i}", feed.drain_cmd, drain.s_cmd)
            depth = int(core.sob_depth)
            self._blocks(f"b{i}", feed.b_blk, core.b_blk, depth)
            self._blocks(f"s{i}", feed.s_blk, core.s_blk, depth)
            self._blocks(f"p{i}", core.p_blk, drain.p_blk, depth)
            self._blocks(f"x{i}", core.x_blk, drain.x_blk, depth)
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
    """One job: ``nit`` iterations on ``A`` (``k × k``) and ``B`` (``k × n``), stored integers."""

    nit: int
    k: int
    n: int
    a: tuple
    b: tuple


def random_job(
    rng, core: CgVectorCore, nit: int, k: int, n: int, *, zero_column: bool = False
) -> Job:
    """A CG system ``(HᴴH + σ²I)/M · X = HᴴY/M`` over Rayleigh ``H`` (M = 8k), quantized to the
    core's formats; ``zero_column`` zeroes the first column of ``B`` (its residual is exactly zero,
    so both divisions take the zero guard)."""
    f = core.formats
    M = 8 * k

    def cn(shape):
        return (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) / np.sqrt(
            2
        )

    H, Y = cn((M, k)), cn((M, n))
    A = (H.conj().T @ H + 10 ** rng.uniform(-2, 0) * np.eye(k)) / M
    B = H.conj().T @ Y / M
    if zero_column:
        B[:, 0] = 0
    a = fx.quantize_real(A.real, f.A), fx.quantize_real(A.imag, f.A)
    b = fx.quantize_real(B.real, f.B), fx.quantize_real(B.imag, f.B)
    return Job(int(nit), int(k), int(n), a, b)


def expected(core: CgVectorCore, job: Job) -> tuple[list, list, tuple]:
    """The job's ``S`` matrices (the core's input), its ``P`` matrices and ``X``, by the model."""
    f = core.formats
    state = cg.cg_init(*job.b, f)
    ss, ps = [], [(state.pr, state.pi)]
    for it in range(1, job.nit + 1):
        sr, si = cg.mm_step(*job.a, state.pr, state.pi, f)
        ss.append((sr, si))
        state, _ = cg.vec_step(state, sr, si, f)
        if it < job.nit:
            ps.append((state.pr, state.pi))
    return ss, ps, (state.xr, state.xi)


def job_streams(core: CgVectorCore, job: Job) -> tuple[np.ndarray, list[np.ndarray]]:
    """The command word and the ``s_in`` bursts of a job: ``B``, then each ``S``."""
    f, L = core.formats, int(core.L)
    cmd = np.asarray(command(job.nit, job.k, job.n).serialize(word_bw=64), np.uint64)
    ss, _, _ = expected(core, job)
    bursts = [to_words64(pack_matrix(*job.b, f.B.W, L), f.B.W, L)]
    bursts += [to_words64(pack_matrix(*s, f.S.W, L), f.S.W, L) for s in ss]
    return cmd, bursts


def expected_words(core: CgVectorCore, job: Job) -> list[np.ndarray]:
    f, L = core.formats, int(core.L)
    _, ps, x = expected(core, job)
    out = [to_words64(pack_matrix(*p, f.P.W, L), f.P.W, L) for p in ps]
    return out + [to_words64(pack_matrix(*x, f.X.W, L), f.X.W, L)]


def check_jobs(core: CgVectorCore, jobs, got_bursts) -> None:
    """Every ``P`` and ``X`` the bench wrote equals the model's, in order."""
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
    """Run the bench in pysim; returns each core's ``P`` and ``X`` bursts."""
    sim = Simulation()
    bench = CgCoreBench(name="bench", sim=sim, cores=cores)
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
    bench = CgCoreBench(name="cg_core_bench", sim=Simulation(), cores=cores)
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
    bench = CgCoreBench(name="cg_core_bench", sim=Simulation(), cores=cores)
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
    return f"""// GENERATED by tests/linalg/_cg_core_bench.py -- sequential C-simulation of {spec.top_name}.
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
    """The ``P`` and ``X`` bursts of each core from the C-simulation's output."""
    assert "CSIM_DONE" in text, text[-500:]
    words: list[list[int]] = [[] for _ in cores]
    cur = -1
    for line in text.splitlines():
        if line.startswith("P "):
            cur = int(line[2:])
        elif line and line != "CSIM_DONE":
            words[cur].append(int(line, 16))
    bench = CgCoreBench(name="cg_core_bench", sim=Simulation(), cores=cores)
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
