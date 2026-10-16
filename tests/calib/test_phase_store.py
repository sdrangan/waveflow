"""Timed-phase calibrations filed under identity (``plans/unit_harness.md`` D7, Stage 3), without a
toolchain: one key for a module wherever it sits, a record found from that key alone and verified
against the module, and a repeated calibration that adds no simulation to the DAG."""
from __future__ import annotations

import pytest

from examples.stream_kernel.stream_kernel import PolyKernel
from examples.stream_kernel.stream_kernel_mm import mm_system
from waveflow.build.build import BuildDag
from waveflow.build.phase_calib import PhaseCalibCachedStep, add_calib_steps, timed_modules
from waveflow.calib.phases import module_identity, phase_record, scenario_digest, stored_phases
from waveflow.calib.record_store import ModuleStore, Provenance, Record, StaleRecordError
from waveflow.hw.harness import harness

#: A fit as fit_phase reports it: the parameters, and the rows (features, rtl, predicted).
FITS = {
    "samples": {"latency": {"params": {"intercept": 30.0, "nwords": 0.0},
                            "train": [({"nwords": 8}, 30.0, 30.0)], "holdout": []},
                "fill_time": {"params": {"intercept": -1.0, "nwords": 1.0},
                              "train": [({"nwords": 8}, 7.0, 7.0)], "holdout": []}},
    "resp": {"resp_time": {"params": {"intercept": 4.0}, "train": [({}, 4.0, 4.0)], "holdout": []}},
}


def _file(store, module, workload="w0"):
    rec, ident = phase_record(module, FITS, workload=workload, part="xc7z020-clg484-1",
                              period_ns=10.0)
    store.append(rec, identity=ident)
    return rec


def test_one_key_wherever_the_kernel_sits():
    """The stream harness, behind a crossbar, alone on a direct link: one key -- what its ports are
    bound to (a queue's depth) is context, not structure."""
    keys = {module_identity(k).key for k in (harness(PolyKernel).dut(), mm_system().device.kernel,
                                               mm_system(direct=True).device.kernel)}
    assert len(keys) == 1


def test_a_record_is_found_from_the_identity_alone(tmp_path):
    """Filed from the harness's kernel, loaded by a kernel in another system that has never seen it."""
    store = ModuleStore(tmp_path)
    _file(store, harness(PolyKernel).dut())
    k = mm_system().device.kernel
    for ph in (k.samp_phase, k.resp_phase):
        ph.calib_dir, ph._models = tmp_path, None
    assert k.samp_phase.predict(nwords=100) == {"latency": 30.0, "fill_time": 99.0}
    assert k.resp_phase.predict() == {"resp_time": 4.0}
    assert stored_phases(store, k, workload="w0") is not None
    assert stored_phases(store, k, workload="another") is None


def test_a_record_of_different_hardware_is_refused(tmp_path):
    """Same key, another structure signature: the read raises rather than loading a number that
    describes something else."""
    store = ModuleStore(tmp_path)
    k = harness(PolyKernel).dut()
    rec = _file(store, k)
    bad = Record(key=rec.key, target="timing", source="xsi", payload=rec.payload,
                 provenance=Provenance(signature="0" * 64))
    store.records_path(rec.key, "timing").open("a").write(__import__("json").dumps(bad.to_json()) + "\n")
    with pytest.raises(StaleRecordError):
        stored_phases(store, k)


def test_a_repeated_calibration_adds_no_simulation(tmp_path):
    """With the record in the store for this workload, the DAG gets the cached step and nothing else;
    with an empty store, or another workload, the traced XSI run is back."""
    sysm = harness(PolyKernel)
    wl = sysm.calibration_workload()
    digest = scenario_digest(wl.system)

    store = ModuleStore(tmp_path / "s")
    dag = BuildDag()
    add_calib_steps(dag, sysm, work_dir=tmp_path / "w", top="t", store=store)
    assert "calib_system_xsi" in dag.step_names()

    for m in timed_modules(wl.system):
        _file(store, m, workload=digest)
    dag = BuildDag()
    step = add_calib_steps(dag, sysm, work_dir=tmp_path / "w", top="t", store=store)
    assert isinstance(step, PhaseCalibCachedStep) and dag.step_names() == ["calib_phases"]

    store2 = ModuleStore(tmp_path / "s2")
    for m in timed_modules(wl.system):
        _file(store2, m, workload="a different workload")
    dag = BuildDag()
    add_calib_steps(dag, sysm, work_dir=tmp_path / "w", top="t", store=store2)
    assert "calib_system_xsi" in dag.step_names()


def test_the_digest_is_of_the_workload_and_leaves_the_system_untouched():
    sysm = harness(PolyKernel)
    wl = sysm.calibration_workload()
    a = scenario_digest(wl.system)
    assert a == scenario_digest(wl.system)
    assert a != scenario_digest(sysm)                  # the test workload is another scenario
    assert wl.system._root is None                     # digested on a fresh copy
