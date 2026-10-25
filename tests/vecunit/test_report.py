"""``waveflow.vecunit.report`` (Stage 9 of ``plans/vecunit_new.md``): the trace report's reading of which
task binds, on stand-in traces, and the bounds of a job."""

from __future__ import annotations

import numpy as np

from waveflow.utils.trace import Firing
from waveflow.vecunit.report import job_bounds, trace_report


class _Trace:
    """What :func:`trace_report` reads from a bound trace: the manifest, firings and block events."""

    def __init__(self, write_held, read_slack, store_slack):
        self.manifest = {
            "tasks": [{"inst": f"vecunit_{r}_1_axis_axis_U0"} for r in ("load", "exec", "store")],
            "channels": [
                {"id": "qi0", "kind": "sob", "producer": "vecunit_load_1_axis_axis_U0",
                 "consumer": "vecunit_exec_1_axis_axis_U0"},
                {"id": "qo0", "kind": "sob", "producer": "vecunit_exec_1_axis_axis_U0",
                 "consumer": "vecunit_store_1_axis_axis_U0"}]}
        commits = [100 * (k + 1) for k in range(4)]
        self.events = {
            ("qi0", "write"): [(c - 50, c, h) for c, h in zip(commits, write_held)],
            ("qi0", "read"): [(c + s, c + 90, False) for c, s in zip(commits, read_slack)],
            ("qo0", "write"): [(c + 10, c + 60, False) for c in commits],
            ("qo0", "read"): [(c + 60 + s, c + 99, False) for c, s in zip(commits, store_slack)]}

    def component_firings(self, inst):
        return tuple(Firing(index=j, start=100 * j, end=100 * j + 99) for j in range(4))

    def block_events(self, name, side):
        return self.events[(name, side)]


def test_the_execute_task_binds_when_it_waits_on_neither_side():
    """The loader's commits held (it waited on the execute task), the execute task taking its
    blocks with slack, the store waiting on every block: the execute task binds."""
    t = _Trace(write_held=[False, True, True, True], read_slack=[2, 40, 45, 40],
               store_slack=[3, 3, 3, 3])
    r = trace_report(t)
    assert r["binds"] == "exec"
    assert r["queues"]["qi0"]["held"] == 3 and r["queues"]["qo0"]["waited"] == 3
    assert r["tasks"]["exec"]["firings"] == 4


def test_a_negative_slack_is_unknown_not_a_wait():
    """The fused loop enables a queue's memory before its block is committed: no evidence."""
    t = _Trace(write_held=[False, True, True, True], read_slack=[2, -80, -80, -80],
               store_slack=[3, 3, 3, 3])
    assert trace_report(t)["queues"]["qi0"]["waited"] == 0


def test_the_loader_binds_when_its_port_is_busy_and_the_execute_task_waits(tmp_path):
    from waveflow.vecunit.build import TOP, WORK_DIR

    acc = tmp_path / WORK_DIR / TOP / "xsi_run" / "vectors" / "s_in0_acc"
    acc.mkdir(parents=True)
    np.arange(10, 400, dtype="<u8").tofile(acc / "cycles.bin")  # a word every cycle
    t = _Trace(write_held=[False] * 4, read_slack=[2, 3, 2, 3], store_slack=[3, 3, 3, 3])
    r = trace_report(t, tmp_path)
    assert r["ports"]["s_in0"]["busy"] == 1.0
    assert r["binds"] == "load"


def test_job_bounds():
    """CG's steady job: four 8 x 32 matrices in (B and three S, 128 words each at 64 bits), four out
    with a header each; the execute task binds.  The matched filter at one antenna: a k x 1 block
    lands a value a cycle (L does not divide n), so the loader takes twice its words."""
    from examples.vecunit_cg.harness import unit as cg_unit
    from examples.vecunit_mf.harness import unit as mf_unit

    u = cg_unit()
    b = job_bounds(u.cfg, u.jobs("steady")[0][1])
    assert b["input"] == 512 and b["output"] == 4 * (1 + 128)
    assert b["binds"] == "execute" and b["elements"] < b["execute"]
    m = mf_unit()
    b = job_bounds(m.cfg, m.jobs("steady_nrx1")[0][1])
    assert b["input"] == 4 * 2 * 32 and b["load"] == 4 * 2 * 64 + 8 * m.timing.load
