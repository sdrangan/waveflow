"""The lock channel's **lowering**: what a graph holding one becomes, in C++ and in Verilog.

``plans/t2p_lock_chan.md`` S1, checkpoint 2.  Checkpoint 1 proved the protocol in pysim; this proves
the other half — that an interface holding four channels, two of which are not channels, reaches a
generated top with the right ports on the right side of the wrapper seam, and that Vitis accepts the
result at the II the design claims.

**The consumer is a fixture, not an example** (:mod:`tests.hw.lock_toy`): two tasks, one memory, one
lock, and nothing a user would want to read for its own sake.  Building a teaching example ahead of
the first real consumer is precisely the un-consumed-abstraction mistake the plan opens by refusing.

The Vitis half is one test and it is marked ``vitis``.  A failed csynth is a real failure; the skip
is only for Vitis not being installed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from waveflow.build.codegen_check import check
from waveflow.build.composite_gen import composite_top_spec, render_tcl, render_top
from waveflow.build.elaborate import elaborate
from waveflow.build.wrapper_gen import render_wrapper, wrapper_spec
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.locked_mem import LOCK_GRANTED, LOCK_SCHEMA_CLASSES
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain

from tests.hw.lock_toy.lock_toy import BASE, CHECK_PERIOD, DEPTH, NWORD, WORD_BW, LockToy

FIXTURE_DIR = Path(__file__).resolve().parent / "lock_toy"
TOP = "lock_toy"

#: The two hand-written ``hls::task`` bodies.  Beside the fixture rather than in ``waveflow/build/``
#: because they are the *toy's*, not framework — the header they both include, ``mem_lock.h``, is
#: the framework part and ships through :class:`~waveflow.build.streamutils.MemLockStep`.
TASK_BODIES = ("lock_toy_write_task.h", "lock_toy_read_task.h")


def _toy():
    return elaborate(LockToy, {}, name=TOP)


# ---------------------------------------------------------------------------
# What the graph lowers to
# ---------------------------------------------------------------------------

def test_the_toy_lowers_as_a_composite_kernel():
    """``check()`` runs the real generator, so this is the rules and not a restatement of them."""
    ok, msg = check(_toy(), COMPOSITE_KERNEL)
    assert ok, msg


def test_the_memory_ports_stay_on_the_boundary_and_the_lock_streams_do_not():
    """**The whole structural claim**, in one assertion.

    Four boundary ports and two internal channels.  Get the split wrong in either direction and
    nothing says so until much later: a ``BramIF`` counted as an internal edge makes the kernel's
    memory ports vanish into a FIFO that does not exist, and a lock stream counted as a boundary port
    makes the kernel demand two AXI-Stream pins a testbench has to drive.
    """
    spec = composite_top_spec(_toy(), width=WORD_BW)
    assert [(p.name, p.kind) for p in spec.ports] == [
        ("s_in", "axis_in"), ("buf_w", "bram"), ("buf_r", "bram"), ("s_out", "axis_out")]
    assert [c.name for c in spec.channels] == ["lock_cmd", "lock_resp"]
    # The wrapper hides the memory ports, so from outside the design is two AXI-Stream pins.
    assert [p.name for p in spec.pin_ports] == ["s_in", "s_out"]


def test_the_lock_channels_are_built_at_the_SCHEMA_width():
    """64 bits, and it would still be 64 in a 32-bit design.

    Derived from what travels on the channel rather than from the design's word, for the reason
    ``status_bitwidth`` gives next door: a channel whose width can disagree with its payload is a
    disagreement waiting to be found at the wrap.
    """
    spec = composite_top_spec(_toy(), width=WORD_BW)
    text = render_top(spec)
    assert "hls_thread_local hls::stream<ap_uint<64> > lock_cmd;" in text
    assert "#pragma HLS STREAM variable=lock_cmd depth=1" in text
    assert "#pragma HLS STREAM variable=lock_resp depth=1" in text
    for cls in LOCK_SCHEMA_CLASSES:
        assert int(cls.nwords_per_inst(64)) == 1


def test_one_endpoint_becomes_three_adjacent_task_arguments():
    """``lock`` occupies one name in the signature and three arguments in the C++.

    Adjacent and in ``physical_endpoints()`` order, which is why a hand-written body reads
    ``(buf, cmd, resp)`` together instead of at positions 2 and 4.  A body whose channels were not
    adjacent would need a second naming scheme that only the resolver understood.
    """
    text = render_top(composite_top_spec(_toy(), width=WORD_BW))
    assert (f"hls::task t0(lock_toy_write_task<{WORD_BW}, {DEPTH}, {NWORD}, {BASE}>, "
            f"s_in, buf_w, lock_cmd, lock_resp);") in text
    assert (f"hls::task t1(lock_toy_read_task<{WORD_BW}, {DEPTH}, {CHECK_PERIOD}>, "
            f"buf_r, lock_cmd, lock_resp, s_out);") in text


def test_the_bram_pragmas_carry_the_memorys_own_latency():
    """``latency=`` comes from the memory's Verilog, through the bound ``BramIF``.

    Not from a constant here: a pragma latency that disagrees with the memory shifts every read by a
    cycle and passes a "did it run" check.  The lock does not change that chain — it only decides who
    may be on the bus.
    """
    comp = _toy()
    text = render_top(composite_top_spec(comp, width=WORD_BW))
    lat = int(comp.mem.read_latency)
    for port in ("buf_w", "buf_r"):
        assert (f"#pragma HLS INTERFACE mode=bram port={port} storage_type=ram_1wnr "
                f"latency={lat}") in text


def test_the_wrapper_joins_both_memory_ports_with_no_hand_wiring():
    """``add_if(lock)`` alone is enough for the wrapper to find both wires.

    That is what ``rtl_interfaces()`` buys.  A composite that had to remember two ``add_rtl_if``
    calls could forget one, and a dangling ``bram`` port is refused only after codegen — by which
    point the message names a port rather than the missing call.
    """
    comp = _toy()
    spec = composite_top_spec(comp, width=WORD_BW)
    assert sorted(comp.rtl_ifs) == sorted([comp.lock.wr_if.name, comp.lock.rd_if.name])
    v = render_wrapper(wrapper_spec(comp, spec))
    assert "module lock_toy_top (" in v
    for port in ("buf_w", "buf_r"):
        assert f".{port}_Addr_A(" in v, f"the wrapper leaves {port} dangling"
    assert "bram_t2p" in v, "the memory is not instantiated beside the kernel"
    # From outside, the design is two AXI-Stream ports: the memory is internal to the wrapper.
    assert "s_in_TDATA" in v and "s_out_TDATA" in v
    assert "buf_w_Addr_A," not in v.split("module lock_toy_top (")[1].split(");")[0], (
        "a memory port leaked into the wrapper's own port list")


def test_the_read_during_write_scan_finds_the_two_roles():
    """The RTL-side hazard scan needs one writer and one reader on the *same* memory, by name.

    ``bram_hazard_manifest`` refuses a memory whose two accessors both act as the same role, so this is
    also the check that the lock wired the writer to port A and the reader to port B.  It is what
    checkpoint 4's VCD scan is pointed at, and a spec with no memories in it would make that scan
    look clean while examining nothing.
    """
    from waveflow.build.wrapper_gen import bram_hazard_manifest

    comp = _toy()
    spec = bram_hazard_manifest(comp, composite_top_spec(comp, width=WORD_BW))
    assert len(spec["memories"]) == 1, spec
    mem = spec["memories"][0]
    assert mem["write"]["addr"] == "buf_w_addr_a"
    assert mem["read"]["addr"] == "buf_r_addr_a"
    assert mem["addr_bits"] == int(comp.mem.addr_bits)


# ---------------------------------------------------------------------------
# The pysim twin of the two C++ bodies
# ---------------------------------------------------------------------------

def _run_toy(payload: np.ndarray, n_out_bursts: int = 30):
    """Drive one transaction through the toy and return ``(toy, everything it emitted)``.

    **The payload is ONE burst**, and that is not a convenience: a pysim slave dequeues a whole
    burst per ``get`` and ``get_pipelined`` under-reads a payload split across several, silently.
    One C++ firing consumes the whole transaction inside ``store_shot``, so one pysim ``get`` must
    too — ``examples/bram_access`` spells out what happens when the two granularities differ.
    """
    sim = Simulation()
    clk = Clock(name="clk", freq=250e6)
    toy = LockToy(sim=sim, name=TOP, clk=clk)
    src = StreamIFMaster(sim=sim, name="src", bitwidth=WORD_BW, has_tlast=True)
    snk = StreamIFSlave(sim=sim, name="snk", bitwidth=WORD_BW, has_tlast=True)
    for nm, m, s in (("in", src, toy.s_in), ("out", toy.s_out, snk)):
        ifc = StreamIF(name=f"toy_{nm}", sim=sim, clk=clk, bitwidth=WORD_BW, depth=2)
        ifc.bind("master", m)
        ifc.bind("slave", s)
    got: list = []

    def drive():
        yield from src.write(np.array([1], dtype=np.uint64))       # the trigger, its own burst
        yield from src.write(payload)                              # the payload, ONE burst

    def drain():
        for _ in range(n_out_bursts):
            got.append(np.asarray((yield from snk.get())).ravel())

    for obj in sim._sim_objs:
        obj.pre_sim()
    for obj in sim._sim_objs:
        p = obj.run_proc()
        if p is not None:
            sim.env.process(p)
    sim.env.process(drive())
    sim.env.process(drain())
    sim.env.run()
    return toy, np.concatenate(got)


def test_the_toy_takes_the_lock_stores_the_region_and_plays_it_back():
    """The pysim twin of both bodies, end to end.

    Values, not plumbing: the region lands at ``BASE`` and its neighbours do not move, which is the
    ``base + offset`` claim.  A design writing the right words at the wrong base passes every "did it
    run" check.
    """
    pay = np.arange(5000, 5000 + NWORD, dtype=np.uint64)
    toy, out = _run_toy(pay)
    assert toy.wr.last_status == LOCK_GRANTED
    assert toy.wr.n_stored == 1
    assert np.array_equal(toy.mem.storage[BASE:BASE + NWORD], pay)
    assert int(toy.mem.storage[BASE - 1]) == 0 and int(toy.mem.storage[BASE + NWORD]) == 0, (
        "the words either side of the region moved; the base is off")
    toy.lock.assert_handover_happened(1)
    assert np.isin(pay, out).all(), "the owner never played the loaded region back"


def test_the_owner_plays_filler_while_it_is_yielded_and_never_stalls():
    """The output has a filler gap in it, and the run has no gap in it.

    Two claims, and the second is the one a byte comparison misses: the owner emits a chunk **every**
    firing whether or not it owns the memory, because the side that cannot stop is what makes it the
    owner.  A body that blocked while yielded would back-pressure whatever it feeds.
    """
    n_drained = 30
    toy, out = _run_toy(np.arange(5000, 5000 + NWORD, dtype=np.uint64), n_out_bursts=n_drained)
    assert toy.rd.n_filler >= 1, (
        "the owner never played filler, so the handover was never actually visible on its output — "
        "which means this run did not exercise the yield at all")
    assert out.size == n_drained * CHECK_PERIOD, (
        f"{n_drained} drained bursts carried {out.size} words, not {n_drained * CHECK_PERIOD}: the "
        f"owner emitted a short chunk, which is a beat it skipped rather than a beat it filled")


def test_the_grant_arrives_within_the_declared_check_period():
    """The bound the owner's ``check_period`` exists to make assertable.

    ``check_period`` elements of its own work, plus the memory's read latency for the chunk already
    in flight, plus the beat the answer takes — at the fabric's rate, because this owner is clocked
    by the fabric and nothing else.
    """
    toy, _ = _run_toy(np.arange(NWORD, dtype=np.uint64))
    budget = 2 * (CHECK_PERIOD + int(toy.mem.read_latency) + 2) / float(toy.clk.freq)
    toy.lock.assert_grant_bounded(budget)


# ---------------------------------------------------------------------------
# Vitis
# ---------------------------------------------------------------------------

def _stage(tmp_path: Path) -> Path:
    """Write the whole compilable tree: headers, both task bodies, the top, and its tcl."""
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.streamutils import MemLockStep, MemMgrStep, StreamUtilsStep
    from waveflow.hw.dataschema import DataSchemaStep

    inc = "include"
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=inc))
    # `render_top` includes memmgr.hpp unconditionally, so it must be beside the sources even for a
    # design with no m_axi port at all.
    dag.add(MemMgrStep(output_dir=inc))
    for cls in LOCK_SCHEMA_CLASSES:
        # Plain read_stream / write_stream, no `framed=True`: the lock channels are INTERNAL edges,
        # where ap_axis is refused outright (HLS 214-208).
        dag.add(DataSchemaStep(cls, word_bw_supported=[WORD_BW], include_dir=inc))
    dag.add(MemLockStep(output_dir=inc))
    results = dag.run(BuildConfig(root_dir=tmp_path), force=True)
    failed = [n for n, r in results.items() if not r.success]
    assert not failed, f"header generation failed: {failed}"

    for name in TASK_BODIES:
        (tmp_path / inc / name).write_text((FIXTURE_DIR / name).read_text(encoding="utf-8"),
                                           encoding="utf-8")
    spec = composite_top_spec(_toy(), width=WORD_BW)
    gen = tmp_path / "gen"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{TOP}.cpp").write_text(render_top(spec), encoding="utf-8")
    # `config_rtl -reset state` is not decoration here: the OWNER writes before it reads, which is
    # the reset trap (reference-hls-task-reset-trap), and the pragma alone did not close it under
    # Vitis 2025.1 in rf_repeat_play.  An owner cannot avoid the shape -- writing without being
    # asked is what "the side that cannot stop" means.
    (tmp_path / f"{TOP}.tcl").write_text(
        render_tcl(TOP, part="xczu48dr-ffvg1517-2-e", period_ns=4,
                   solution_config=("config_rtl -reset state",)),
        encoding="utf-8")
    return tmp_path


@pytest.mark.vitis
def test_the_lock_toy_csynthesizes_at_ii_1(tmp_path):
    """Vitis accepts a task holding a ``mode=bram`` port and two lock channels, at II=1.

    The II is **achieved**, read out of the report, and the loop names are **discovered** rather than
    spelled: Vitis names an unlabelled loop ``VITIS_LOOP_<line>_1`` and nests that into its children,
    so a spelled name stops matching on a comment edit — and a gate that skipped on a miss would read
    as a pass.  Both loops here are labelled for that reason, and this asserts they are found.
    """
    from waveflow.utils.csynthparse import loop_pipeline_ii, module_loops

    if not toolchain.find_vitis_path():
        pytest.skip("Vitis not found.")
    root = _stage(tmp_path)
    try:
        toolchain.run_vitis_hls(root / f"{TOP}.tcl", work_dir=root)
    except RuntimeError as exc:
        pytest.skip(f"Vitis execution unavailable: {exc}")
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"csynth of {TOP} failed:\n{exc.stdout}\n{exc.stderr}")

    report = root / f"{TOP}_proj" / "solution1" / "syn" / "report"
    assert report.is_dir(), f"csynth reported success but wrote no report dir at {report}"

    # One module per pipelined body, found by the LABEL each loop carries.  Naming them by hand is
    # what the comment above refuses; searching for the label is stable under an edit that moves it.
    found = {label: [p.stem[: -len("_csynth")] for p in report.glob("*_csynth.xml")
                     if p.stem.endswith(f"Pipeline_{label}_csynth")]
             for label in ("store_shot", "play_chunk")}
    for label, mods in found.items():
        assert len(mods) == 1, (
            f"expected exactly one synthesized module for the {label!r} loop, found {mods}. "
            f"An empty list means the label moved or the loop stopped being pipelined — and a gate "
            f"that skipped here would read as a pass.")
        module = mods[0]
        loops = module_loops(report, module)
        assert loops, f"{module} reports no pipelined loop at all"
        assert loop_pipeline_ii(report, module, loops[0]) == 1, (
            f"{module}.{loops[0]} does not achieve II=1. One cycle per element is the throughput "
            f"claim of both sides of this lock; do not re-record it without diagnosing why.")
