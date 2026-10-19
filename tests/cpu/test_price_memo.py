"""``SwFunction.price`` memoizes a call's price (``plans/sim_speed_cpu_linalg.md``, step 10.2).

A memo is only an optimization if it returns what pricing from scratch returns, at every point, for
every configuration -- and never lets one call's features alias another's.
"""

from __future__ import annotations

from waveflow.cpu import task as T
from waveflow.cpu.config import CpuConfig
from waveflow.cpu.platform import CpuPlatform
from waveflow.cpu.task import SwFunction, eval_cost, eval_cycles


def _sched_add() -> SwFunction:
    plat = CpuPlatform.load()
    return SwFunction(
        name="sched_ops.add",
        fn=lambda: (None, {}),
        cycles=plat.model("sched_ops.add", "cycles"),
        energy_pj=plat.model("sched_ops.add", "energy_pj"),
        working_set=lambda c: 8.0 * (c["n_tasks"] + 1),
    )


def test_the_memo_returns_what_pricing_from_scratch_returns():
    f = _sched_add()
    cfg = CpuConfig()
    for n in (0, 3, 17, 3, 0, 5000, 17):  # repeats hit the memo
        cnt = {"n_tasks": float(n), "n_scanned": float(n), "n_moved": float(n // 2)}
        feats, cycles, energy = f.price(cnt, cfg)
        want = f.features(cnt, cfg)
        assert feats == want
        assert cycles == eval_cycles(f.cycles, want)
        assert energy == eval_cost(f.energy_pj, want)
    assert len(f._prices) == 4


def test_each_call_gets_its_own_features():
    f = _sched_add()
    cnt = {"n_tasks": 2.0, "n_scanned": 1.0, "n_moved": 0.0}
    a, _, _ = f.price(cnt, CpuConfig())
    b, _, _ = f.price(cnt, CpuConfig())
    assert a == b and a is not b


def test_the_cache_sizes_are_part_of_the_key():
    """The regime features depend on the configuration's caches: a smaller L1 is a different price."""
    f = _sched_add()
    cnt = {"n_tasks": 8000.0, "n_scanned": 10.0, "n_moved": 5.0}  # a 64 KB working set
    big, _, _ = f.price(cnt, CpuConfig())
    small, _, _ = f.price(cnt, CpuConfig(l1d_bytes=16 * 1024, l2_bytes=32 * 1024))
    assert big["ws_over_l1"] != small["ws_over_l1"]
    assert small == f.features(cnt, CpuConfig(l1d_bytes=16 * 1024, l2_bytes=32 * 1024))


def test_an_unhashable_counter_is_priced_every_time():
    f = SwFunction(name="odd", fn=lambda: (None, {}), cycles=7.0)
    feats, cycles, energy = f.price({"x": [1, 2]}, CpuConfig())  # type: ignore[dict-item]
    assert (feats, cycles, energy) == ({"x": [1, 2]}, 7.0, None)
    assert f._prices == {}


def test_the_memo_is_bounded(monkeypatch):
    monkeypatch.setattr(T, "PRICE_MEMO_MAX", 3)
    f = SwFunction(name="fixed", fn=lambda: (None, {}), cycles=5.0)
    for i in range(10):
        assert f.price({"i": float(i)}, CpuConfig())[1] == 5.0
        assert len(f._prices) <= 3
