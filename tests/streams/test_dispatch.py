"""StreamDispatch (``waveflow/streams/dispatch.py``, ``plans/stream_dispatch.md``).

pysim: both policies exact on a backlog, credits never exceeded, a bad credit ignored and counted, jobs
never split; the pool ``dispatch -> links -> arbiter`` (``m_sel`` back as the credits) returning every job
once, whole; a pool of workers whose time depends on the job, where first-free beats a fixed split.  The
body's signature, step and the pool's top.  The body itself under g++ with stand-in headers
(``cpp_mock/``), against the model's decision rules.  At RTL (``-m xsi``): the pool, its frames as a
multiset (the order is timing's), RTL and pysim both equal to the inputs'.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

from waveflow.build.build import BuildConfig
from waveflow.hw.clock import Clock
from waveflow.hw.harness import harness
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    FramedStreamIFSlave,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.simulation.simulation import Simulation
from waveflow.streams import StreamArbiter, StreamDispatch, StreamDispatchStep, rtl
from waveflow.streams.rtl import DispatchPool, sorted_frames
from waveflow.toolchain.toolchain import find_vitis_path, find_vivado_path

ROOT = Path(__file__).resolve().parents[2] / "work" / "streams"
SRC = Path(__file__).resolve().parents[2] / "waveflow" / "streams" / "src"
MOCK = Path(__file__).resolve().parent / "cpp_mock"
PER = 1e-8


# --- the block alone: workers that never give a credit back, or give them back on a script --------------


def _dispatch_alone(policy, n=3, credits=2, jobs=10, frame_bursts=1, script=()):
    """The dispatcher with *jobs* jobs waiting, each output read at once, and ``s_free`` fed *script*
    (``(cycle, word)`` pairs).  Returns ``(the worker each job went to, in order; the block)``."""
    sim = Simulation()
    clk = Clock(freq=1 / PER)
    d = StreamDispatch(name="d", sim=sim, n_out=n, bitwidth=32, frame_bursts=frame_bursts, credits=credits,
                       policy=policy, clk=clk)
    tx = {}
    for port, ep in (("s_in", d.s_in), ("s_free", d.s_free)):
        tx[port] = StreamIFMaster(name=f"t_{port}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"l_{port}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=4096)
        link.bind("master", tx[port])
        link.bind("slave", ep)
    order: list[tuple[float, int, int]] = []
    rx = []
    for k in range(n):
        r = StreamIFSlave(name=f"r{k}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"o{k}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=4096)
        link.bind("master", d.m_out[k])
        link.bind("slave", r)
        rx.append(r)

    def drive():
        for f in range(jobs):
            for b in range(frame_bursts):
                yield from tx["s_in"].write(np.asarray([f << 8 | b] * 3, dtype=np.uint32))

    def credit():
        t = 0
        for cyc, word in script:
            yield sim.env.timeout((cyc - t) * PER)
            t = cyc
            yield from tx["s_free"].write(np.asarray([word], dtype=np.uint32))

    def read(k):
        while True:
            words = yield from rx[k].get()
            order.append((sim.env.now, k, int(words[0])))

    sim.env.process(drive())
    sim.env.process(credit())
    for k in range(n):
        sim.env.process(read(k))
    sim.run_sim()
    bursts = sorted(order, key=lambda x: (x[0], x[2]))
    by_job = {}
    for _, k, w in bursts:
        by_job.setdefault(w >> 8, set()).add((k, w & 0xFF))
    assert all(len({k for k, _ in v}) == 1 for v in by_job.values())       # a job's bursts on one worker
    assert all(sorted(b for _, b in v) == list(range(frame_bursts)) for v in by_job.values())
    return [next(iter(by_job[f]))[0] for f in sorted(by_job)], d


def test_first_free_fills_the_lowest_worker_first():
    sent, d = _dispatch_alone("first_free")
    assert sent == [0, 0, 1, 1, 2, 2]                    # 3 workers x 2 credits, then nothing comes back
    assert d.outstanding == [2, 2, 2] and d.peak == [2, 2, 2]


def test_round_robin_takes_each_worker_in_turn():
    sent, _ = _dispatch_alone("round_robin")
    assert sent == [0, 1, 2, 0, 1, 2]


@pytest.mark.parametrize("policy", ["first_free", "round_robin"])
def test_a_job_is_never_split_across_workers(policy):
    sent, _ = _dispatch_alone(policy, frame_bursts=3, jobs=4, credits=1)
    assert len(sent) == 3                                # three workers, one credit each


def test_a_returned_credit_frees_its_own_worker_only():
    # Worker 1's credit back at cycle 100 takes the last job; at 200, with no job left, two credits for
    # worker 1 (one in excess) and one naming no worker: the excess and the stray are counted and
    # ignored, and never let a worker past its credits.
    sent, d = _dispatch_alone("first_free", credits=1, jobs=4,
                              script=((100, 1), (200, 1), (200, 1), (200, 9)))
    assert sent == [0, 1, 2, 1]
    assert d.bad_credits == 2
    assert d.outstanding == [1, 0, 1] and max(d.peak) == 1


@pytest.mark.parametrize("policy", ["first_free", "round_robin"])
def test_a_job_announced_before_it_is_all_there_stalls_the_block(policy):
    """Rule 2, as at RTL (``plans/stream_dispatch.md`` Stage 8): the block commits to a job on its first
    word and copies it as the words arrive.  A job begun at cycle 10 and held open until 200 goes to worker
    1 at its first word; the credit worker 0 returns at cycle 20 then waits on ``s_free`` -- not taken
    back -- until the job is all there, and is taken back at once after."""
    sim = Simulation()
    clk = Clock(freq=1 / PER)
    d = StreamDispatch(name="d", sim=sim, n_out=2, bitwidth=32, credits=1, policy=policy, clk=clk)
    tx = {}
    for port, ep in (("s_in", d.s_in), ("s_free", d.s_free)):
        tx[port] = StreamIFMaster(name=f"t_{port}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"l_{port}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=64)
        link.bind("master", tx[port])
        link.bind("slave", ep)
    out = {}
    for k in range(2):
        r = StreamIFSlave(name=f"r{k}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"o{k}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=64)
        link.bind("master", d.m_out[k])
        link.bind("slave", r)
        out[k] = r
    seen = {}

    def drive():
        yield from tx["s_in"].write(np.asarray([0xA0, 0xA1], dtype=np.uint32))     # job A: worker 0
        yield sim.env.timeout((10 - sim.env.now / PER) * PER)
        x = yield from tx["s_in"].begin(np.asarray([0xB0, 0xB1, 0xB2], dtype=np.uint32))
        yield sim.env.timeout(190 * PER)                                          # B held open to 200
        yield from tx["s_in"].end(x)

    def credit():
        yield sim.env.timeout(20 * PER)
        yield from tx["s_free"].write(np.asarray([0], dtype=np.uint32))

    def probe():
        yield sim.env.timeout(100 * PER)
        seen[100] = (list(d.outstanding), len(d.s_free.data_buffer.items), out[1].pending_burst is not None)
        yield sim.env.timeout(110 * PER)
        seen[210] = (list(d.outstanding), len(d.s_free.data_buffer.items))

    for p in (drive(), credit(), probe()):
        sim.env.process(p)
    sim.run_sim()
    assert d.sent == [1, 1]
    assert seen[100] == ([1, 1], 1, True)        # B committed and on its way; the credit not taken back
    assert seen[210] == ([0, 1], 0)               # B all there at 200, then the credit
    assert d.bad_credits == 0


# --- the pool: dispatch -> links -> arbiter, m_sel back as the credits -----------------------------------


@pytest.mark.parametrize("policy", ["first_free", "round_robin"])
@pytest.mark.parametrize("n,frame_bursts,credits", [(2, 1, 1), (3, 2, 2), (4, 1, 1), (4, 2, 3)])
def test_the_pool_returns_every_job_once_and_whole(policy, n, frame_bursts, credits):
    mod = DispatchPool(name="pool", sim=Simulation(), n=n, frame_bursts=frame_bursts, credits=credits,
                       policy=policy)
    sysm = harness(mod, workload="test")
    assert sysm.dut().policy == policy and sysm.dut().dsp.policy == policy
    sysm.run()
    got = [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins["m_out"].words]
    assert sorted_frames(got, frame_bursts) == mod.expected_frames()
    d = sysm.dut().dsp
    assert sum(d.sent) == len(mod.expected_frames()) == 14
    assert d.outstanding == [0] * n and max(d.peak) <= credits and d.bad_credits == 0


def test_the_pool_has_no_golden():
    """The order on ``m_out`` is timing's, so the pool states no golden for the in-order ``golden`` step;
    its check is :meth:`DispatchPool.expected_frames`, a multiset."""
    assert not hasattr(DispatchPool, "golden")


@dataclass
class TimedWorker(FreeRunMod):
    """A test worker: takes a job, holds it ``CYCLES_PER_WORD`` cycles a word of its job, then passes it
    on -- a unit whose time depends on its data."""

    cpp_kernel_name: ClassVar[str | None] = None
    CYCLES_PER_WORD: ClassVar[int] = 20
    clk: Clock = field(default_factory=lambda: Clock(freq=1 / PER))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=32)
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=32)
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.m_out)

    def run_iter(self):
        words = yield from self.s_in.get()
        yield self.env.timeout(len(words) * self.CYCLES_PER_WORD * self.clk.period)
        yield from self.m_out.write(words)


def _makespan(policy, credits, lengths, n=3):
    """Jobs of *lengths* words through ``dispatch -> n TimedWorkers -> arbiter``; the cycle the last
    result arrives, and how many jobs each worker took."""
    sim = Simulation()
    clk = Clock(freq=1 / PER)
    d = StreamDispatch(name="d", sim=sim, n_out=n, bitwidth=32, credits=credits, policy=policy, clk=clk)
    a = StreamArbiter(name="a", sim=sim, n_in=n, bitwidth=32, policy="round_robin", clk=clk)
    ws = [TimedWorker(name=f"w{k}", sim=sim, clk=clk) for k in range(n)]
    links = [(d.m_out[k], w.s_in) for k, w in enumerate(ws)]
    links += [(w.m_out, a.s_in[k]) for k, w in enumerate(ws)]
    links.append((a.m_sel, d.s_free))
    tx = StreamIFMaster(name="tx", sim=sim, bitwidth=32)
    rx = StreamIFSlave(name="rx", sim=sim, bitwidth=32)
    links += [(tx, d.s_in), (a.m_out, rx)]
    for i, (m, s) in enumerate(links):
        link = StreamIF(name=f"l{i}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=64)
        link.bind("master", m)
        link.bind("slave", s)
    done = []

    def drive():
        for f, n_w in enumerate(lengths):
            yield from tx.write(np.asarray([f << 12 | j for j in range(n_w)], dtype=np.uint32))

    def read():
        while True:
            words = yield from rx.get()
            done.append((sim.env.now / PER, int(words[0]) >> 12))

    sim.env.process(drive())
    sim.env.process(read())
    sim.run_sim()
    assert sorted(f for _, f in done) == list(range(len(lengths)))
    return max(t for t, _ in done), list(d.sent)


def test_first_free_keeps_every_worker_busy_where_a_fixed_split_does_not():
    """Jobs of data-dependent length (every third one long): first-free with one credit a worker sends
    each job to whichever worker is done; round-robin with deep credits is a fixed split (job f to
    worker f mod 3), and the worker that drew the long jobs finishes last."""
    lengths = [16 if f % 3 == 0 else 2 for f in range(18)]
    t_free, sent_free = _makespan("first_free", 1, lengths)
    t_fixed, sent_fixed = _makespan("round_robin", 16, lengths)
    assert sent_fixed == [6, 6, 6]
    assert t_free < 0.75 * t_fixed, (t_free, t_fixed)
    assert sent_free != sent_fixed                    # the short jobs went to the idle workers


# --- the body, its step and the pool's top --------------------------------------------------------------


def test_the_dispatch_body_and_ports():
    d = StreamDispatch(name="d", sim=Simulation(), n_out=3, bitwidth=64, frame_bursts=2, credits=4,
                       policy="round_robin")
    assert d.ports == ("s_in", "s_free", "m_out_0", "m_out_1", "m_out_2")
    kt = d.kernel_task()
    assert kt.task_fn == "stream_dispatch_task" and kt.signature == d.ports and kt.framed == d.ports
    assert kt.template_args == (64, 2, 4, 1)


@pytest.mark.parametrize("bad", [dict(n_out=1), dict(n_out=17), dict(frame_bursts=0), dict(credits=0),
                                 dict(credits=256), dict(policy="least_loaded")])
def test_the_dispatcher_refuses_a_bad_build(bad):
    with pytest.raises(ValueError):
        StreamDispatch(name="d", sim=Simulation(), **bad)


def test_the_step_copies_the_body(tmp_path):
    s = StreamDispatchStep(output_dir="include")
    text = s.generate(s.KEY, BuildConfig(root_dir=tmp_path, params={}))
    assert "template <int W, int NB, int C, int RR, typename TI, typename TF, typename... TO>" in text


def test_the_pool_top_has_both_tasks_the_loop_and_a_credit_link_for_every_credit(tmp_path):
    mod, top = rtl.make("pool", 4, 32, 2, "round_robin", credits=3)
    assert top == "poolrr4_w32_f2_c3"
    rtl.generate_for(tmp_path, mod)
    (gen,) = (tmp_path / "gen").glob("*.cpp")
    text = gen.read_text()
    assert "stream_dispatch_task<32, 2, 3, 1," in text and "stream_arbiter_task<32, 2, 1," in text
    assert "#pragma HLS STREAM variable=credit depth=12" in text              # n * credits
    assert all(f"#pragma HLS STREAM variable=w{k} depth=16" in text for k in range(4))


def test_the_dispatcher_alone_runs_in_its_harness():
    """``rtl.make("dispatch", ...)``: the block alone, for its resources and timing.  Its own scenario
    puts nothing on ``s_free`` (an empty bundle, so the driver has one to read), so each worker takes
    its credits' worth of jobs and the rest wait."""
    mod, top = rtl.make("dispatch", 3, 32, 1, "priority", credits=2)
    assert top == "sdspff3_w32_f1_c2" and mod.policy == "first_free"
    assert mod.workload("test")["s_free"] == []
    sysm = harness(mod, workload="test")
    sysm.run()
    d = sysm.dut()
    assert d.sent == [2, 2, 2] and d.outstanding == [2, 2, 2] and d.bad_credits == 0
    assert [len(sysm.stand_ins[f"m_out_{k}"].words) for k in range(3)] == [2, 2, 2]


def test_the_dispatcher_alone_top_has_its_task_only(tmp_path):
    mod, top = rtl.make("dispatch", 4, 64, 2, "round_robin", credits=4)
    assert top == "sdsprr4_w64_f2_c4"
    rtl.generate_for(tmp_path, mod)
    text = (tmp_path / "gen" / "stream_dispatch.cpp").read_text()
    assert "stream_dispatch_task<64, 2, 4, 1," in text and "stream_arbiter_task<" not in text


def test_pysim_dispatcher_spends_no_cycle_between_jobs():
    """pysim's ``StreamDispatch`` has no decision cost: backlogged jobs leave back to back, whatever
    ``n_out`` and the policy (the RTL's cost is measured by ``waveflow.streams.dispatch_timing``,
    ``plans/stream_dispatch.md`` Stage 4).  First-free with ample credits keeps to worker 0."""
    from waveflow.streams.dispatch_timing import JOBS, pysim_gaps

    for n, policy in ((2, "first_free"), (4, "round_robin")):
        g = pysim_gaps(n, policy)
        assert g["jobs"] == JOBS and set(g["gaps"]) == {0}
        assert g["workers"] == ([0] * JOBS if policy == "first_free" else [f % n for f in range(JOBS)])


def test_the_round_trip_pairs_each_credit_with_its_workers_next_job():
    """``dispatch_timing._pair``: a credit for worker ``k`` with the first job to ``k`` after it; a credit
    no job followed is left out."""
    from waveflow.streams.dispatch_timing import _pair

    credits = [(10, 0), (12, 1), (30, 0), (40, 1)]
    jobs = {0: [0, 14, 33], 1: [5, 20]}
    assert _pair(credits, jobs) == [(10, 14, 0), (12, 20, 1), (30, 33, 0)]


# --- the body under g++, against the model's decision rules ------------------------------------------------


def _model_script(lines, n, credits, policy, frame_bursts=1):
    """What the model's rules (:meth:`StreamDispatch.free_workers`, :meth:`StreamDispatch.take_back`) do
    with a ``dispatch_check.cpp`` script, firing by firing: the ``sent <worker> <job>`` lines it prints."""
    d = StreamDispatch(name="d", sim=Simulation(), n_out=n, credits=credits, policy=policy,
                       frame_bursts=frame_bursts)
    jobs, free_q, out, job = [], [], [], 0
    for line in lines:
        op, *args = line.split()
        if op == "job":
            jobs.append(job)
            job += 1
        elif op == "credit":
            free_q.append(int(args[0]))
        elif op == "fire":
            for _ in range(int(args[0])):
                if free_q:
                    d.take_back(free_q.pop(0))
                elif jobs and (free := d.free_workers()):
                    k = free[0]
                    d.outstanding[k] += 1
                    if policy == "round_robin":
                        d.next_k = (k + 1) % n
                    out.append(f"sent {k} {jobs.pop(0)}")
    return out


SCRIPT = ["job 3", "job 1 2", "job 2", "job 4", "job 5", "job 6", "job 7", "job 2", "job 1", "fire 12",
          "credit 0", "credit 9", "credit 2", "credit 2", "credit 2", "fire 9", "credit 1", "credit 0",
          "fire 4", "job 3", "job 3", "fire 3"]


@pytest.mark.skipif(shutil.which("g++") is None, reason="no g++: the body's logic check needs a C++ compiler")
@pytest.mark.parametrize("policy", ["first_free", "round_robin"])
@pytest.mark.parametrize("credits", [1, 2])
def test_the_body_decides_as_the_model(tmp_path, policy, credits):
    """``stream_dispatch_task`` compiled with stand-in ``hls_stream.h`` / ``ap_int.h`` (logic only, not
    Vitis csim), three workers, a scripted run: every job to the worker the model's rules pick."""
    exe = tmp_path / "dc"
    rr = 1 if policy == "round_robin" else 0
    script = [ln for ln in SCRIPT if ln != "job 1 2"]          # one burst a job here
    cc = subprocess.run(["g++", "-std=c++17", "-Wno-unknown-pragmas", f"-I{MOCK}", f"-I{SRC}",
                         f"-DC_={credits}", f"-DRR_={rr}", str(MOCK / "dispatch_check.cpp"), "-o", str(exe)],
                        capture_output=True, text=True, check=False)
    assert cc.returncode == 0, cc.stderr
    run = subprocess.run([str(exe)], input="\n".join(script) + "\n", capture_output=True, text=True,
                         check=False)
    assert run.returncode == 0, run.stderr
    got = [ln for ln in run.stdout.splitlines() if ln.startswith("sent")]
    assert got == _model_script(script, 3, credits, policy)
    assert len(got) >= 6


# --- at RTL -----------------------------------------------------------------------------------------------


def _frames(trace_dir: Path, frame_bursts: int) -> list[tuple]:
    """A recording's frames as a multiset: its words split at its bursts' ends."""
    words = np.fromfile(trace_dir / "words.bin", dtype="<u8")
    bounds = np.fromfile(trace_dir / "bounds.bin", dtype="<u8")
    starts = [0, *bounds[:-1]]
    return sorted_frames([words[int(s):int(e)] for s, e in zip(starts, bounds)], frame_bursts)


@pytest.mark.xsi
@pytest.mark.parametrize("n,frame_bursts,credits,policy,workload", [
    (2, 1, 1, "first_free", "test"),
    (3, 2, 2, "round_robin", "test"),
    # Four workers (the arbiter's hang needed four inputs: plans/shared_kernel_rfsoc_timing.md), a backlog.
    (4, 1, 1, "first_free", "long"),
    (4, 2, 3, "round_robin", "long"),
], ids=["pool2_ff", "pool3_rr_c2", "pool4_ff_long", "pool4_rr_c3_long"])
def test_rtl_pool_returns_every_job_once(n, frame_bursts, credits, policy, workload):
    """THE gates: the pool at RTL, its ``m_out`` the inputs' jobs as a multiset, each whole, none lost or
    doubled -- and pysim's the same.  Not ``compare``: which worker takes a job, and so the order out, is
    timing's (``plans/stream_dispatch.md`` D6)."""
    if not (find_vivado_path() and find_vitis_path()):
        pytest.skip("XSI gate prerequisite missing: Vitis HLS + Vivado")
    pol = "round_robin" if policy == "round_robin" else "priority"
    root = ROOT / f"pool{n}{frame_bursts}{credits}{policy[0]}{workload[0]}"
    root.mkdir(parents=True, exist_ok=True)
    dag, top = rtl.build_block_dag(root / "xw", "pool", n, 32, frame_bursts, pol, workload=workload,
                                   credits=credits)
    cfg = BuildConfig(root_dir=root, params={"synth": "build"})
    res = dag.run(cfg, through="system_xsi")
    # ``through`` runs a step and its ancestors only, and ``pysim`` is not one of ``system_xsi``'s; the
    # gate checks pysim's recording too, so run it (``compare``, which would pull it in, is no gate here).
    res.update(dag.run(cfg, through="pysim"))
    bad = {k: r.message for k, r in res.items() if not r.success}
    assert not bad, bad
    mod, _ = rtl.make("pool", n, 32, frame_bursts, pol, credits)
    want = mod.expected_frames(workload)
    work = root / "xw" / top
    assert _frames(work / "traces" / "m_out", frame_bursts) == want
    assert _frames(work / "pysim_traces" / "m_out", frame_bursts) == want
