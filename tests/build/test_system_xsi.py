"""``waveflow/build/system_xsi.py`` -- the fast half (plans/host_runtime.md S5).

The RTL half is the example gates (test_mm_fir_xsi.py, test_markov_xsi.py), which run
``run_system_xsi``.  Here: discovery from a bare system object, and the trace comparison.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.build.hwcodegen import LoweringError
from waveflow.build.system_xsi import compare_traces, discover
from waveflow.utils.burst_io import write_burst_bundle


def test_discover_finds_the_crossbar_the_host_and_the_cut():
    from examples.markov.markov_xsi import system
    from examples.mm_fir.mm_fir_xsi import system as fir_system

    s = system()
    xbar, host, inside = discover(s)
    assert xbar is s.xbar and host is s.host
    assert inside == [s.gen, s.chain, s.mem]            # device kernels, then the memory, in slot order
    f = fir_system("one_front")
    xbar, host, inside = discover(f)
    assert xbar is f.xbar and host is f.host and inside == [f.fir]


def test_discover_needs_a_bus_system():
    from examples.markov.markov import MarkovSystem, default_jobs
    with pytest.raises(LoweringError, match="one crossbar"):
        discover(MarkovSystem(jobs=default_jobs(1, 16), link="direct"))


def test_compare_traces_names_what_differs(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        write_burst_bundle([np.arange(3, dtype=np.uint64)], root / "qin")
    write_burst_bundle([np.arange(2, dtype=np.uint64)], a / "qout")
    write_burst_bundle([np.arange(2, dtype=np.uint64) + 1], b / "qout")
    write_burst_bundle([np.arange(1, dtype=np.uint64)], b / "extra")
    assert compare_traces(a, a) == []
    assert compare_traces(a, b) == ["extra/words.bin", "extra/bounds.bin", "extra/meta.json",
                                    "qout/words.bin"]
