"""mem_w_stream.py — the calibration fixture for the in-band forwarding writer.

``MemWStream(inband=True, emit_done=True)`` — the ``mem_w_stream_framed_done_task`` mem_copy composes.
Its per-firing control cost is a property of ``(component, platform)``, fit once and reused by every
accelerator on that platform (:mod:`waveflow.calib.platform`).

The vehicle here drives the writer **standalone** — a :class:`~waveflow.simulation.stream_tb.StreamDriver`
feeds ``s_in`` the framed ``[MemWCmd | fwd bursts | data words]`` directly, a memory sits behind
``m_mem``, a sink drains ``s_done`` — so the fixture depends only on the package, never on the
mem_copy example.  It sweeps two independent axes:

* ``nwords`` / ``num_trans`` — the write size (data words, and the AXI bursts they take), and
* ``n_fwd`` — the number of forwarded bursts the writer buffers across the store and echoes on
  ``s_done``.  mem_copy holds this at 1, so its residual absorbs the per-message cost into the
  intercept; only a sweep that varies ``n_fwd`` **independently** of ``nwords`` can identify the
  per-message coefficient (a constant regressor is collinear with the intercept).

The RTL side is measured (:data:`RTL_SPAN`, from the closed mem_copy loop) only at ``n_fwd == 1``; the
``n_fwd > 1`` points return ``None`` from :meth:`rtl_firings` — they *need* a cosim measurement, which
is exactly the gap this fixture exists to close once a toolchain run provides those spans.
"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np

from waveflow.calib.fixture import ComponentFixture, SweepPoint, register

#: The writer task-body id — the component key its firings carry and the platform-library subdir.
COMPONENT = "mem_w_stream_framed_done_task"

#: Max AXI burst length (data beats per transaction) — ``num_trans = ceil(nwords / 16)``.
MEM_AXI_MAX_BURST = 16

#: Measured writer RTL firing span (cycles, ap_done-anchored) at ``n_fwd == 1``, per n_words — from
#: the closed mem_copy calibration loop (see examples/mem_copy/calibrate_platform.py).
RTL_SPAN = {128: 183.0, 512: 615.0}


def _num_trans(nwords: int) -> int:
    return math.ceil(nwords / MEM_AXI_MAX_BURST)


class MemWStreamFixture(ComponentFixture):
    """Calibration fixture for the in-band forwarding writer."""

    component = COMPONENT
    basis = ["nwords", "num_trans", "n_fwd"]

    def __init__(self, mem_dwidth: int = 64,
                 nwords_grid: "tuple[int, ...]" = (128, 512),
                 n_fwd_grid: "tuple[int, ...]" = (1, 2, 4),
                 jobs: int = 8) -> None:
        self.mem_dwidth = int(mem_dwidth)
        self.nwords_grid = tuple(nwords_grid)
        self.n_fwd_grid = tuple(n_fwd_grid)
        self.jobs = int(jobs)

    def sweep(self) -> list[SweepPoint]:
        # A grid, not a diagonal: n_fwd varies at every nwords and vice versa, so the per-message and
        # per-word coefficients are separable.
        return [
            SweepPoint(label=f"n{nw}_f{nf}",
                       features={"nwords": nw, "num_trans": _num_trans(nw), "n_fwd": nf},
                       jobs=self.jobs)
            for nw in self.nwords_grid
            for nf in self.n_fwd_grid
        ]

    def rtl_firings(self, point: SweepPoint) -> dict | None:
        nw = point.features["nwords"]
        nf = point.features["n_fwd"]
        if nf != 1 or nw not in RTL_SPAN:
            return None                     # needs a cosim measurement — reported, not invented
        return {
            "top": "mem_w_stream", "max_burst_len": MEM_AXI_MAX_BURST,
            "firings": [{
                "component": self.component, "index": 0,
                "nwords": nw, "num_trans": _num_trans(nw), "n_fwd": nf,
                "span": RTL_SPAN[nw], "blocked": 0,
            }],
        }

    def run_pysim(self, point: SweepPoint, *, comp_dir, platform_dir, clk) -> list[dict]:
        nw = int(point.features["nwords"])
        nf = int(point.features["n_fwd"])
        tm = self.timing_model(comp_dir, clk)
        return _run_writer(nwords=nw, n_fwd=nf, jobs=point.jobs, timing_model=tm,
                           platform_dir=platform_dir, clk=clk, mem_dwidth=self.mem_dwidth)


def _run_writer(*, nwords: int, n_fwd: int, jobs: int, timing_model, platform_dir, clk,
                mem_dwidth: int = 64) -> list[dict]:
    """Drive ``MemWStream(inband, emit_done)`` standalone for *jobs* firings and return its
    ``firing_records``.

    Each job frames ``[MemWCmd(addr, len=nwords, fwd_bursts=n_fwd) | n_fwd 1-word bursts | nwords data
    words]`` onto ``s_in``; the writer stores the data and echoes the forwarded bursts on ``s_done``.
    We do not check the copy — this is a *timing* vehicle — but the writer records one firing per job,
    each carrying ``{nwords, num_trans, n_fwd, span, current_dly}``.
    """
    from waveflow.hw.interface import StreamIF
    from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
    from waveflow.hw.mem_stream import MemWCmd, MemWStream
    from waveflow.hw.memory import MemModel
    from waveflow.simulation.simulation import Simulation
    from waveflow.simulation.stream_tb import StreamDriver, StreamSink
    from waveflow.utils.burst_io import write_burst_bundle

    w = int(mem_dwidth)
    bpw = w // 8
    sim = Simulation()

    stride = nwords + 16                       # keep each job's destination region disjoint
    arena = stride * jobs + 32
    mem = MemModel(name="mem", sim=sim, inline=False, clk=clk,
                       word_size=w, addr_size=32, nwords_tot=arena)
    mem.alloc(arena)
    # Platform bus model on the memory slave: pysim charges the calibrated m_axi transfer cost, so the
    # writer's residual is its own control cost (incl. per-message), not the bus term.
    if platform_dir is not None:
        from waveflow.calib.bus_model import BusCalib
        mem.s_mm.bus_timing = BusCalib(platform_dir, clk_freq=clk.freq).bus_timing()

    wr = MemWStream(name="wr", sim=sim, mem_dwidth=w, inband=True, emit_done=True, clk=clk)
    # Attach the fixture's model directly (turns on per-firing recording, and predicts current_dly
    # from the platform's params); we do NOT pass calib_dir to the writer, so it builds no model of a
    # different basis.
    wr.add_timing_model(timing_model)

    # Frame every job's s_in bursts.  A 1-word forwarded burst keeps n_buf == n_fwd within the
    # default max_fwd_words bound.
    words: list[np.ndarray] = []
    for j in range(jobs):
        dst = j * stride
        memw = np.asarray(MemWCmd(addr=dst, len=nwords, fwd_bursts=n_fwd).serialize(word_bw=w),
                          dtype=np.uint64)
        resps = [np.asarray([0xD00 + i], dtype=np.uint64) for i in range(n_fwd)]
        data = np.zeros(nwords, dtype=np.uint64)
        words += [memw, *resps, data]

    with tempfile.TemporaryDirectory() as _root:
        root = Path(_root)
        write_burst_bundle(words, root / "s_in")
        drv = StreamDriver(sim=sim, bitwidth=w, in_bundle="s_in", root=root, has_tlast=True)
        sink = StreamSink(sim=sim, bitwidth=w, has_tlast=True)

        in_if = StreamIF(name="in_if", sim=sim, clk=clk, bitwidth=w)
        in_if.bind(ep_name="master", endpoint=drv.stream_ep)
        in_if.bind(ep_name="slave", endpoint=wr.s_in)

        done_if = StreamIF(name="done_if", sim=sim, clk=clk, bitwidth=w)
        done_if.bind(ep_name="master", endpoint=wr.s_done)
        done_if.bind(ep_name="slave", endpoint=sink.stream_ep)

        xbar = AXIMMCrossBarIF(sim=sim, clk=clk, nports_master=1, nports_slave=1, bitwidth=w)
        xbar.bind("master_0", wr.m_mem)
        xbar.bind("slave_0", mem.s_mm)
        assign_address_ranges([mem.s_mm], [(0, arena * bpw)])

        sim.run_sim()

    return list(wr.firing_records)


#: Registered at import (waveflow.calib.fixtures.__init__ imports this module).
FIXTURE = register(MemWStreamFixture())
