"""mem_r_stream.py — the calibration fixture for the in-band forwarding reader.

``MemRStream(inband=True)`` — the ``mem_r_stream_framed_task`` an accelerator composes as its ``m_axi``
read owner.  Like the writer (:mod:`waveflow.calib.fixtures.mem_w_stream`), its per-firing control cost is
a property of ``(component, platform)``, fit once and reused by every accelerator on that platform.  This fixture is
the reader's half, and the reason it exists: mem_copy is **writer-bound**, so nothing exercised the
reader as a bottleneck and its residual was never fit — the **interleaver** (a gather that issues two
reads per job) is the first reader-bound design, and it ran ~10% under the RTL until this landed.

The vehicle drives the reader **standalone** — a :class:`~waveflow.simulation.stream_tb.StreamDriver`
feeds ``s_cmd`` the framed ``[MemRCmd | forwarded burst]``, a memory sits behind ``m_mem``, a sink
drains ``m_out`` — so it depends only on the package, never on an example.  It sweeps ``nwords`` /
``num_trans`` — the read size (data words, and the AXI bursts they take).  Unlike the writer, the
reader's timing feature set is just ``(nwords, num_trans)``: it relays the in-band descriptor but does
not model a per-forwarded-burst cost (the 1-word relay is negligible and folds into the intercept), so
there is no ``n_fwd`` axis.

The RTL side is measured (:data:`RTL_SPAN`), read off the interleaver XSI traces — the reader is that
design's back-to-back bottleneck, so its ap_done cadence *is* its per-firing span: ``nw + (num_trans-1)``
bus cycles plus a constant ~16-cycle control residual (nw=64→83, 128→151, 256→287).
"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np

from waveflow.calib.fixture import ComponentFixture, SweepPoint, register

#: The reader task-body id — the component key its firings carry and the platform-library subdir.
COMPONENT = "mem_r_stream_framed_task"

#: Max AXI burst length (data beats per transaction) — ``num_trans = ceil(nwords / 16)``.
MEM_AXI_MAX_BURST = 16

#: Measured reader RTL firing span (cycles, ap_done-anchored) at ``n_fwd == 1``, per n_words — from the
#: interleaver XSI traces (its reader is the back-to-back bottleneck, so the ap_done cadence is the span).
RTL_SPAN = {64: 83.0, 128: 151.0, 256: 287.0}


def _num_trans(nwords: int) -> int:
    return math.ceil(nwords / MEM_AXI_MAX_BURST)


class MemRStreamFixture(ComponentFixture):
    """Calibration fixture for the in-band forwarding reader."""

    component = COMPONENT
    basis = ["nwords", "num_trans"]

    def __init__(self, mem_dwidth: int = 64,
                 nwords_grid: "tuple[int, ...]" = (64, 128, 256),
                 jobs: int = 8) -> None:
        self.mem_dwidth = int(mem_dwidth)
        self.nwords_grid = tuple(nwords_grid)
        self.jobs = int(jobs)

    def sweep(self) -> list[SweepPoint]:
        # One point per read size; the residual is a line in nwords (with num_trans along for the ride).
        return [
            SweepPoint(label=f"n{nw}",
                       features={"nwords": nw, "num_trans": _num_trans(nw)},
                       jobs=self.jobs)
            for nw in self.nwords_grid
        ]

    def rtl_firings(self, point: SweepPoint) -> dict | None:
        nw = point.features["nwords"]
        if nw not in RTL_SPAN:
            return None                     # needs a cosim measurement — reported, not invented
        return {
            "top": "mem_r_stream", "max_burst_len": MEM_AXI_MAX_BURST,
            "firings": [{
                "component": self.component, "index": 0,
                "nwords": nw, "num_trans": _num_trans(nw),
                "span": RTL_SPAN[nw], "blocked": 0,
            }],
        }

    def run_pysim(self, point: SweepPoint, *, comp_dir, platform_dir, clk) -> list[dict]:
        nw = int(point.features["nwords"])
        tm = self.timing_model(comp_dir, clk)
        # A pure read (no forwarded burst): the RTL span is fwd-independent, and the 1-word relay would
        # only add standalone-only overhead the composed reader's residual should not carry.
        return _run_reader(nwords=nw, n_fwd=0, jobs=point.jobs, timing_model=tm,
                           platform_dir=platform_dir, clk=clk, mem_dwidth=self.mem_dwidth)


def _run_reader(*, nwords: int, n_fwd: int, jobs: int, timing_model, platform_dir, clk,
                mem_dwidth: int = 64) -> list[dict]:
    """Drive ``MemRStream(inband)`` standalone for *jobs* firings and return its ``firing_records``.

    Each job frames ``[MemRCmd(addr, len=nwords, fwd_bursts=n_fwd) | n_fwd 1-word bursts]`` onto
    ``s_cmd``; the reader relays the forwarded bursts, then bursts ``nwords`` words off ``m_mem`` — both
    onto ``m_out``, which a sink drains.  We do not check the data (this is a *timing* vehicle); the
    reader records one firing per job carrying ``{nwords, num_trans, n_fwd, span, current_dly}``.
    """
    from waveflow.hw.interface import StreamIF
    from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
    from waveflow.hw.mem_stream import MemRCmd, MemRStream
    from waveflow.hw.memory import MemModel
    from waveflow.simulation.simulation import Simulation
    from waveflow.simulation.stream_tb import StreamDriver, StreamSink
    from waveflow.utils.burst_io import write_burst_bundle

    w = int(mem_dwidth)
    bpw = w // 8
    sim = Simulation()

    stride = nwords + 16                       # keep each job's source region disjoint
    arena = stride * jobs + 32
    mem = MemModel(name="mem", sim=sim, inline=False, clk=clk,
                       word_size=w, addr_size=32, nwords_tot=arena)
    mem.alloc(arena)                           # reads return zeros — content is irrelevant to timing
    # Platform bus model on the memory slave: pysim charges the calibrated m_axi transfer cost, so the
    # reader's residual is its own control cost, not the bus term.
    if platform_dir is not None:
        from waveflow.calib.bus_model import BusCalib
        mem.s_mm.bus_timing = BusCalib(platform_dir, clk_freq=clk.freq).bus_timing()

    rd = MemRStream(name="rd", sim=sim, mem_dwidth=w, inband=True, clk=clk)
    # Attach the fixture's model directly (turns on per-firing recording, predicts current_dly from the
    # platform's params); we do NOT pass calib_dir, so the reader builds no model of a different basis.
    rd.add_timing_model(timing_model)

    words: list[np.ndarray] = []
    for j in range(jobs):
        src = j * stride
        memr = np.asarray(MemRCmd(addr=src, len=nwords, fwd_bursts=n_fwd).serialize(word_bw=w),
                          dtype=np.uint64)
        resps = [np.asarray([0xD00 + i], dtype=np.uint64) for i in range(n_fwd)]
        words += [memr, *resps]                # no data on s_cmd — the reader gets data from m_mem

    with tempfile.TemporaryDirectory() as _root:
        root = Path(_root)
        write_burst_bundle(words, root / "s_cmd")
        drv = StreamDriver(sim=sim, bitwidth=w, in_bundle="s_cmd", root=root, has_tlast=True)
        sink = StreamSink(sim=sim, bitwidth=w, has_tlast=True)

        cmd_if = StreamIF(name="cmd_if", sim=sim, clk=clk, bitwidth=w)
        cmd_if.bind(ep_name="master", endpoint=drv.stream_ep)
        cmd_if.bind(ep_name="slave", endpoint=rd.s_cmd)

        out_if = StreamIF(name="out_if", sim=sim, clk=clk, bitwidth=w)
        out_if.bind(ep_name="master", endpoint=rd.m_out)
        out_if.bind(ep_name="slave", endpoint=sink.stream_ep)

        xbar = AXIMMCrossBarIF(sim=sim, clk=clk, nports_master=1, nports_slave=1, bitwidth=w)
        xbar.bind("master_0", rd.m_mem)
        xbar.bind("slave_0", mem.s_mm)
        assign_address_ranges([mem.s_mm], [(0, arena * bpw)])

        sim.run_sim()

    return list(rd.firing_records)


#: Registered at import (waveflow.calib.fixtures.__init__ imports this module).
FIXTURE = register(MemRStreamFixture())
