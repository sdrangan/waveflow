"""The systolic unit in its own harness (``plans/systolic_characterization.md``, Stages 0-3).

* the unit's top's trace manifest names each stream-of-blocks channel's lock nets, after the task
  PARAMETER (Vitis's naming), so the core's block events can be read off a level-1 trace;
* the harness's scenarios: ``workload`` / ``golden`` agree in pysim, and the calibration workload is
  offered because the core (a task inside the unit) declares timed phases;
* at RTL (``-m xsi``): fed straight from streams, the unit runs back-to-back ``8x8x32`` jobs at the
  core's own pace -- 390 cycles a job (Vitis 2025.1), not the 821 it took while its 2-deep reply
  channel serialized load and store.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.linalg import _systolic_harness as SH
from tests.linalg._hls import require_vitis
from waveflow.build.composite_gen import composite_top_spec
from waveflow.hw.harness import harness
from waveflow.toolchain import toolchain


def test_manifest_names_the_block_locks():
    u = SH.unit()
    man = composite_top_spec(u, width=64).trace_manifest()
    by_id = {c["id"]: c for c in man["channels"]}
    core = next(t["inst"] for t in man["tasks"] if t["body"] == "systolic_core_task")
    load = next(t["inst"] for t in man["tasks"] if t["body"] == "systolic_load_task")
    a = by_id["a"]
    assert a["kind"] == "sob"
    assert a["write"] == {"blk_release": f"{load}_a_blk_write", "blk_ce0": f"{load}_a_blk_ce0",
                          "blk_full_n": "a_i_full_n"}
    assert a["read"] == {"blk_release": f"{core}_a_blk_read", "blk_ce0": f"{core}_a_blk_ce0",
                         "blk_empty_n": "a_t_empty_n"}
    assert by_id["c"]["write"]["blk_release"] == f"{core}_c_blk_write"


def test_reply_channel_holds_two_headers():
    """The fix: the store keeps a reply header for its whole firing, so the receiver -> store channel
    must take the next request's header (and a refused one's) meanwhile."""
    u = SH.unit()
    (reply,) = [i for n, i in u.interfaces.items() if n.endswith("_reply_if")]
    assert reply.depth == 2 * u.header_words == 6


def test_harness_scenarios_and_calibration_workload():
    sysm = harness(SH.unit(), workload="test", n_cycles=200_000)
    assert sysm.has_golden()
    wl = sysm.calibration_workload()
    assert wl is not None and wl.holdout == {"tiles": (3, 6)}
    u = SH.unit()
    # every calibration request is served, and the held-out tile counts occur
    shapes = u.jobs("calib")
    tiles = {(m // int(u.R)) * (n // int(u.C)) for _op, m, _k, n in shapes}
    assert {3, 6} <= tiles and len(shapes) == 32


@pytest.mark.xsi
def test_xsi_stream_fed_interval(tmp_path_factory):
    """Four identical 8x8x32 requests back to back, the unit fed straight from streams: every reply
    390 cycles after the one before (Vitis 2025.1) -- the core's own pace."""
    require_vitis()
    if not toolchain.find_vivado_path():
        pytest.skip("Vivado (xsim) not found; the XSI gate needs it")
    root = tmp_path_factory.mktemp("sh")
    u = SH.unit()
    dag = SH.build_dag(workload=SH.steady(u, 4), calibrate=False)
    res = dag.run(SH.build_config(root), through="compare")
    bad = {k: r.message[-2000:] for k, r in res.items() if not r.success}
    assert not bad, bad
    cyc = np.fromfile(root / SH.WORK_DIR / SH.TOP / "xsi_run" / "vectors" / "s_out" / "cycles.bin",
                      dtype="<u8")
    per = u.header_words + 128                       # a reply: its header, then C (128 words)
    assert len(cyc) == 4 * per
    ends = [int(cyc[(j + 1) * per - 1]) for j in range(4)]
    assert np.diff(ends).tolist() == [390, 390, 390]
