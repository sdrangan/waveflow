"""Software channels between host threads, at RTL (plans/host_runtime.md S6).

``QueuedFirHost`` is ``FirHost`` with its writer split into two threads joined by a ``SwQueue``: a packer
that queues the scenario items, a sender that takes them and writes the bus.  Software events take no
time -- a SimPy event in pysim, a same-tick wake in the C++ scheduler -- so the bus behaviour must be
FirHost's exactly: **618 cycles** (per_view), bit-exact, and every endpoint's trace identical to the
same host's pysim run.  A scheduler that woke a waiter one cycle late would move the count.

Run: ``pytest tests/examples/test_sw_channels_xsi.py -m xsi``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

from examples.mm_fir.mm_fir import FirHost
from examples.mm_fir.mm_fir_xsi import (
    NSAMP,
    PLAN,
    ROOT,
    RTL,
    XBAR_NAMES,
    output_words,
    parse_kv,
    scenario_x,
    system,
    trace_report,
)
from examples.mm_fir.mm_fir import fir_golden
from waveflow.build.system_xsi import run_system_xsi
from waveflow.build.trace_steps import rtl_staleness
from waveflow.sw import SwQueue
from waveflow.toolchain.toolchain import find_vivado_path

WORK = Path(__file__).resolve().parents[2] / "tests" / "build" / "_xsi_work"


@dataclass
class QueuedFirHost(FirHost):
    """FirHost, with the writer as two threads through a software queue (C++ twin beside this file)."""

    cpp_model: ClassVar[str | None] = "QueuedFirHostModel"
    cpp_header: ClassVar[str | None] = "xsi_local/queued_fir_host.h"

    def main(self):
        self.q = SwQueue(self, capacity=2, name="q")
        self.start(self._packer)
        self.start(self._sender)
        yield from self._reader()

    def _packer(self):
        for i in range(len(self.items)):
            yield from self.q.put(i)

    def _sender(self):
        for _ in range(len(self.items)):
            item = self.items[(yield from self.q.get())]
            if item[0] == "cfg":
                yield from self.cfg.write(item[1])
            else:
                _, _nsamp, _tx, _want, hdr, samples = item
                yield from self.qin.write(hdr)
                yield from self.qin.write(samples)


def test_queued_host_runs_like_fir_host_in_pysim():
    """The split costs no time in pysim either: same cycles, same outputs as FirHost."""
    ref = system("per_view")
    y_ref = ref.run()
    s = system("per_view")
    s.host.__class__ = QueuedFirHost
    y = s.run()
    assert np.array_equal(y, y_ref)
    assert s.sim.env.now == ref.sim.env.now


@pytest.mark.xsi
def test_queued_host_at_rtl_matches_fir_host():
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado")
    if not RTL.is_dir() or rtl_staleness(ROOT, "mm_fir") is not None:
        pytest.skip("XSI gate prerequisite missing: mm_fir's csynth RTL (python -m examples.mm_fir.mm_fir_build)")
    sysm = system("per_view")
    sysm.host.__class__ = QueuedFirHost
    run = run_system_xsi(sysm, WORK, top="mm_fir_top", xbar_name=XBAR_NAMES["per_view"],
                         workspace="mm_fir_queued")
    out = run.output + trace_report(run.traces)
    assert run.done and run.cycles == 618, (run.cycles, out[-2000:])
    assert parse_kv(out, "STATUS") == {"nsamp": NSAMP, "ncfg": 2}
    assert parse_kv(out, "RESP")["mismatches"] == 0
    assert np.array_equal(output_words(out), fir_golden(scenario_x(), PLAN))
    assert run.trace_mismatches == [], run.trace_mismatches
