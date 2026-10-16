"""``waveflow/build/system_xsi.py`` -- the fast half (plans/host_runtime.md S5).

The RTL half is the example gates (test_mm_fir_xsi.py, test_markov_xsi.py), which run the
examples' system DAGs; ``run_system_xsi`` is a thin wrapper over the same steps.  Here: discovery from a bare system object, and the trace comparison.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.build.hwcodegen import LoweringError
from waveflow.build.system_xsi import compare_traces, discover
from waveflow.utils.burst_io import write_burst_bundle


def test_discover_finds_the_crossbar_the_host_and_the_cut():
    from examples.markov.markov_build import system
    from examples.mm_fir.mm_fir_build import system as fir_system

    s = system()
    xbar, host, inside = discover(s)
    assert xbar is s.xbar and host is s.host
    assert inside == [s.gen, s.chain, s.mem]            # device kernels, then the memory, in slot order
    f = fir_system("one_front")
    xbar, host, inside = discover(f)
    assert xbar is f.xbar and host is f.host and inside == [f.fir]


def test_a_system_needs_no_bus():
    """system_model S3: the direct-wired markov has no crossbar in its DUT -- so no bus and no host
    on one; its cut is still its own declaration."""
    from examples.markov.markov import MarkovSystem, default_jobs
    s = MarkovSystem(jobs=default_jobs(1, 16), link="direct")
    assert discover(s) == (None, None, [s.gen, s.chain, s.mem])


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


# ---------------------------------------------------------------------------------------------
# The HwSystem contract (plans/system_model.md S0)
# ---------------------------------------------------------------------------------------------

def test_a_system_declares_its_cut():
    """Both systems state their DUT through dut() -- a tree whose children are the devices and the
    memory (system_model S1) -- and cut() flattens it to the kernels and memories discover() hands
    on: the cut the old derivation from the crossbar's slaves gave, so the top is unchanged."""
    from examples.markov.markov_build import system
    from examples.mm_fir.mm_fir_build import system as fir_system
    from waveflow.hw.hw_system import DutTop, HwSystem

    s, f = system(), fir_system("per_view")
    assert isinstance(s, HwSystem) and isinstance(f, HwSystem)
    assert isinstance(s.dut(), DutTop)
    assert list(s.dut().sub_comps.values()) == [s.gen_dev, s.chain_dev, s.mem]
    assert s.xbar in s.dut().interfaces.values()
    assert s.cut() == [s.gen, s.chain, s.mem] and f.cut() == [f.fir]
    assert s.xbar in s.elements() and s.host in s.elements()


def test_a_class_that_is_not_a_system_is_refused_by_name():
    class Loose:
        sim = None

    with pytest.raises(LoweringError, match=r"not a waveflow HwSystem.*dut\(\).*run\(\)"):
        discover(Loose())


def test_a_system_without_its_cut_cannot_be_built():
    from waveflow.hw.hw_system import HwSystem

    class NoCut(HwSystem):
        def run(self):
            return None

    with pytest.raises(TypeError, match="dut"):
        NoCut()


def test_the_host_declares_its_generated_header_and_the_harness_names_no_participant_type():
    """system_model S4: a SwHost's endpoints header is part of what its bfm_model() declares, so
    system_tb_spec treats every participant alike -- it imports no participant type."""
    import inspect

    from examples.markov.markov_build import system
    from waveflow.build import system_top

    s = system()
    (bm,) = [s.host.bfm_model()]
    ((fname, render),) = bm.generated_files
    assert fname == "MarkovHost_endpoints.h"
    assert "MarkovHost_endpoints" in render()                  # rendered on demand, from the wired host
    src = inspect.getsource(system_top.system_tb_spec)
    assert "SwHost" not in src and "isinstance(p" not in src


def test_the_bus_and_the_host_are_found_by_role():
    """system_model D1: the bus is the DUT tree's link that realizes as bus IP; the host is the
    bus master outside the DUT.  discover() names neither type."""
    import inspect

    from examples.markov.markov_build import system

    s = system()
    xbar, host, _cut = discover(s)
    assert xbar is s.xbar and host is s.host
    src = inspect.getsource(discover)
    assert "AXIMMCrossBarIF" not in src and "SwHost" not in src
    # Take the crossbar out of the DUT: it is then testbench, and the DUT has no bus of its own.
    del s.dut().interfaces[s.xbar.name]
    assert discover(s)[:2] == (None, None)


# ---------------------------------------------------------------------------------------------
# The testbench contract (plans/system_model.md S3b): any participant, not only a host
# ---------------------------------------------------------------------------------------------

def test_the_harness_binds_every_participant_behind_a_top_port():
    """mem_copy: no host.  The driver and the sink answer the two stream ports with their declared
    models; the memory behind both m_axi bundles is ONE shared arena with a read and a write slave;
    with no host there is no done() -- the run is the scenario's bound."""
    from examples.mem_copy.mem_copy_sim import MemCopySystem
    from waveflow.build.system_top import (SystemTopSpec, render_system_ports_h, render_system_tb,
                                           system_tb_spec)

    s = MemCopySystem()
    spec = SystemTopSpec.from_system(s)
    tb = system_tb_spec(spec, None, [], graph=s)
    assert {m.name: m.cls for m in tb.models} == {
        "tb_cmd_if": "AxisMaster", "tb_done_if": "AxisSlave",
        "m0_axi": "AxiMmReadSlave", "m1_axi": "AxiMmWriteSlave"}
    assert [sh[0] for sh in tb.shared] == ["FlatMemory"] and tb.stop_on is None
    main, files = render_system_tb(spec, tb)
    assert 'h.run(wfbfm::run_param("n_cycles"' in main and "return 0;" in main
    ports_h = render_system_ports_h(spec)
    assert '"m0_axi_AWREADY"' in ports_h and '"m1_axi_ARREADY"' in ports_h   # the other direction pinned
    assert "tb_cmd_if" in ports_h and "m1_axi" in ports_h


def test_recordings_come_from_the_participants_and_pysim_writes_them(tmp_path):
    """A system states nothing: the sink's out_bundle and the memory's dump segments are its
    recordings, and pysim now writes them as the C++ models do."""
    from examples.mem_copy.mem_copy_sim import MemCopySystem

    s = MemCopySystem()
    assert s.recordings() == ("vectors/out", "vectors/s_done")
    s.write_scenario(tmp_path)
    s.run()
    for rec in s.recordings():
        assert {p.name for p in (tmp_path / rec).iterdir()} >= {"words.bin", "bounds.bin", "meta.json"}


def test_a_host_system_records_nothing_and_writes_its_scenario_through_the_host(tmp_path):
    """markov's testbench is its host: its traces go to the host's own directory, so the system has
    no recordings, and the default write_scenario is the host's."""
    from examples.markov.markov_build import system

    s = system()
    assert s.recordings() == ()
    s.write_scenario(tmp_path)
    assert s.host.scenario == tmp_path.as_posix() and any(tmp_path.iterdir())


# ---------------------------------------------------------------------------------------------
# Vendor IP in the testbench (plans/system_model.md S3d): behavioral edges, recording policy
# ---------------------------------------------------------------------------------------------

def test_the_system_harness_for_rf_loopback_is_the_kernel_flows_model_for_model():
    """The RFDC's two models each span the cut (a top stream port and an RF channel); the source and
    sink are peers on the channels.  Same classes, arguments, DynParams and channels as tb_top_spec
    gives the same design -- only the names it binds to differ."""
    from examples.rf_loopback.rf_loopback import RfLoopbackSystem
    from examples.rf_loopback.rf_loopback_xsi import make_xsi_tb
    from waveflow.build.composite_gen import tb_top_spec
    from waveflow.build.system_top import SystemTopSpec, system_tb_spec

    s = RfLoopbackSystem()
    spec = SystemTopSpec.from_system(s, top="rf_loopback_system")
    sys_tb, ker_tb = system_tb_spec(spec, None, [], graph=s), tb_top_spec(make_xsi_tb())
    # The one deliberate difference: the system harness points each counting model at the run's
    # counters directory (its own test below).  Everything else is the kernel flow's.
    def dyn(x):
        return tuple(d for d in x.dyn_params if d[0] != "counters_out")
    key = lambda m: (m.cls, m.args, dyn(m), m.channel)                              # noqa: E731
    assert sorted(map(key, sys_tb.models)) == sorted(map(key, ker_tb.models))
    chan = lambda c: (c.cls, c.name, c.args, dyn(c))                                # noqa: E731
    assert sorted(map(chan, sys_tb.channels)) == sorted(map(chan, ker_tb.channels))
    assert {m.cls for m in sys_tb.models if m.channel and m.binds[0] == "sim.dut()"} == {
        "RfdcAdcMaster", "RfdcDacSlave"}                          # each spans the cut


def test_a_recording_its_participant_marks_example_is_gathered_not_compared():
    from examples.mem_copy.mem_copy_sim import MemCopySystem
    from examples.rf_loopback.rf_loopback import RfLoopbackSystem

    rf = RfLoopbackSystem()
    assert rf.recordings() == ("vectors/rf_out", "vectors/counters") and rf.compared_recordings() == ()
    mc = MemCopySystem()
    assert mc.compared_recordings() == mc.recordings()          # exact by default


def test_counting_models_write_where_the_system_run_gathers_and_the_kernel_flow_is_unchanged():
    """A model declaring reports_counters is pointed at vectors/counters/<model>.json by the SYSTEM
    harness only; the kernel flow's harness sets nothing new.  The directory is a recording the
    example checks, never compared against pysim."""
    from examples.rf_loopback.rf_loopback import RfLoopbackSystem
    from examples.rf_loopback.rf_loopback_xsi import make_xsi_tb
    from waveflow.build.composite_gen import render_tb_harness, tb_top_spec
    from waveflow.build.system_top import SystemTopSpec, render_system_tb, system_tb_spec

    s = RfLoopbackSystem()
    spec = SystemTopSpec.from_system(s, top="rf_loopback_system")
    _main, files = render_system_tb(spec, system_tb_spec(spec, None, [], graph=s))
    wired = [ln.strip() for ln in files["rf_loopback_system_tb_harness.h"].splitlines()
             if "counters_out" in ln]
    assert len(wired) == 6 and all("vectors/counters/" in ln for ln in wired)
    assert "counters_out" not in render_tb_harness(tb_top_spec(make_xsi_tb()))
    assert "vectors/counters" in s.recordings()
    assert "vectors/counters" not in s.compared_recordings()
