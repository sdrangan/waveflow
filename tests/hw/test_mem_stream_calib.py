"""tests/hw/test_mem_stream_calib.py — the mem-streams' shared-library calibration wiring.

MemRStream/MemWStream are reusable *infra*, so their control residual is a property of ``(component,
platform)``, not of the accelerator that composes them.  This checks the wiring added for that:

* ``platform_dir`` resolves each stream's ``StreamTimingModel`` corpus to the shared platform library
  (``<platform_dir>/components/<task-body>/``), keyed by the component's task-body id;
* an explicit ``calib_dir`` overrides it (the project-local one-off);
* neither set leaves the component uncalibrated (no model, timing unchanged);
* the **reader** — which had no calibration hook before — now records a firing per command, so it is
  genuinely calibratable, mirroring the writer.

pysim only; the fitted params flow through the DAG steps tested under ``tests/build``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from waveflow.calib.platform import PlatformCalib
from waveflow.hw.clock import Clock
from waveflow.hw.mem_stream import (
    MemRStream,
    MemWStream,
    _resolve_calib_dir,
)
from waveflow.simulation.simulation import Simulation

MEM_DW = 64


def _sim():
    return Simulation()


class TestResolve:
    def test_platform_dir_resolves_to_the_shared_component_library(self, tmp_path):
        r = MemRStream(name="rd", sim=_sim(), mem_dwidth=MEM_DW, inband=True, platform_dir=tmp_path)
        w = MemWStream(name="wr", sim=_sim(), mem_dwidth=MEM_DW, inband=True, platform_dir=tmp_path)
        # keyed by the task-body id -> the two infra components land in DISTINCT shared libraries.
        assert Path(r.timing_model.calib_dir) == PlatformCalib(tmp_path).component_dir(
            "mem_r_stream_framed_task")
        assert Path(w.timing_model.calib_dir) == PlatformCalib(tmp_path).component_dir(
            "mem_w_stream_framed_done_task")
        assert r.timing_model.calib_dir != w.timing_model.calib_dir

    def test_calib_dir_overrides_platform_dir(self, tmp_path):
        local = tmp_path / "project_local"
        w = MemWStream(name="wr", sim=_sim(), mem_dwidth=MEM_DW, inband=True,
                       calib_dir=local, platform_dir=tmp_path / "platform")
        assert Path(w.timing_model.calib_dir) == local

    def test_neither_set_is_uncalibrated(self):
        assert MemRStream(name="rd", sim=_sim(), inband=True).timing_model is None
        assert MemWStream(name="wr", sim=_sim(), inband=True).timing_model is None

    def test_resolver_precedence_unit(self, tmp_path):
        assert _resolve_calib_dir("local", tmp_path, "comp") == Path("local")
        assert _resolve_calib_dir(None, tmp_path, "comp") == PlatformCalib(tmp_path).component_dir(
            "comp")
        assert _resolve_calib_dir(None, None, "comp") is None


class TestReaderIsCalibratable:
    """The reader gained a trailing ``timed_delay`` hook — attaching a model must now record one
    firing per command (with the features), the corpus the collect step reads."""

    def test_reader_records_a_firing_per_command(self, tmp_path):
        from waveflow.hw.interface import StreamIF
        from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
        from waveflow.hw.mem_stream import MemRCmd
        from waveflow.hw.memory import MemComponent
        from waveflow.simulation.stream_tb import StreamDriver, StreamSink
        from waveflow.utils.burst_io import write_burst_bundle

        sim = _sim()
        clk = Clock(freq=100e6)
        arena = 4096
        bpw = MEM_DW // 8
        mem = MemComponent(name="mem", sim=sim, inline=False, clk=clk,
                           word_size=MEM_DW, addr_size=32, nwords_tot=arena)
        mem.alloc(arena)
        n = 64
        known = np.arange(n, dtype=np.uint64)
        mem._mem.write(16 * bpw, known)

        rd = MemRStream(name="rd", sim=sim, mem_dwidth=MEM_DW, inband=True, clk=clk,
                        platform_dir=tmp_path)
        assert rd.timing_model is not None

        # a single framed read: MemRCmd relays 0 bursts, just fetches its data.
        memr = np.asarray(MemRCmd(addr=16, len=n, fwd_bursts=0).serialize(word_bw=MEM_DW),
                          dtype=np.uint64)
        write_burst_bundle([memr], tmp_path / "cmd")
        drv = StreamDriver(sim=sim, bitwidth=MEM_DW, in_bundle="cmd", root=tmp_path, has_tlast=True)
        sink = StreamSink(sim=sim, bitwidth=MEM_DW, has_tlast=True)

        cmd_if = StreamIF(name="cmd_if", sim=sim, clk=clk, bitwidth=MEM_DW)
        cmd_if.bind(ep_name="master", endpoint=drv.stream_ep)
        cmd_if.bind(ep_name="slave", endpoint=rd.s_cmd)
        out_if = StreamIF(name="out_if", sim=sim, clk=clk, bitwidth=MEM_DW)
        out_if.bind(ep_name="master", endpoint=rd.m_out)
        out_if.bind(ep_name="slave", endpoint=sink.stream_ep)
        xbar = AXIMMCrossBarIF(sim=sim, clk=clk, nports_master=1, nports_slave=1, bitwidth=MEM_DW)
        xbar.bind("master_0", rd.m_mem)
        xbar.bind("slave_0", mem.s_mm)
        assign_address_ranges([mem.s_mm], [(0, arena * bpw)])

        sim.run_sim()

        assert len(rd.firing_records) == 1
        rec = rd.firing_records[0]
        assert rec["nwords"] == n and rec["num_trans"] == 4          # ceil(64/16)
        assert rec["current_dly"] == 0.0                             # unfitted seed -> no delay
