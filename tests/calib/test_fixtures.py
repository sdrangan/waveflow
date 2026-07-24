"""tests/calib/test_fixtures.py — the component-calibration fixture contract, registry, and the
in-band writer and reader fixtures.

Toolchain-free: the fixture's RTL side is stubbed with a synthetic span law (a real one needs cosim),
but the pysim vehicle, the registry, and the fit are exercised for real.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from waveflow.calib.fixture import ComponentFixture, SweepPoint, all_fixtures, get, register
from waveflow.calib.platform import Platform
from waveflow.hw.clock import Clock


# ---------------------------------------------------------------------------
# Contract + registry
# ---------------------------------------------------------------------------

def test_concrete_fixture_must_name_component_and_basis():
    with pytest.raises(TypeError, match="component"):
        class NoComponent(ComponentFixture):
            basis = ["nwords"]

            def sweep(self):
                return []

            def run_pysim(self, point, *, comp_dir, platform_dir, clk):
                return []

    with pytest.raises(TypeError, match="basis"):
        class NoBasis(ComponentFixture):
            component = "x"

            def sweep(self):
                return []

            def run_pysim(self, point, *, comp_dir, platform_dir, clk):
                return []


def _toy_fixture_cls():
    class Toy(ComponentFixture):
        component = "toy_task"
        basis = ["nwords"]

        def sweep(self):
            return [SweepPoint("n8", {"nwords": 8})]

        def run_pysim(self, point, *, comp_dir, platform_dir, clk):
            return []

    return Toy


def test_register_get_all_and_duplicate():
    Toy = _toy_fixture_cls()
    inst = Toy()
    register(inst)
    try:
        assert get("toy_task") is inst
        assert inst in all_fixtures()
        # Re-registering the SAME instance is idempotent; a different one for the same id is an error.
        assert register(inst) is inst
        with pytest.raises(ValueError, match="already registered"):
            register(Toy())
    finally:
        from waveflow.calib import fixture as fx_mod
        fx_mod._REGISTRY.pop("toy_task", None)


def test_get_unknown_raises():
    with pytest.raises(KeyError, match="no fixture registered"):
        get("does_not_exist_task")


# ---------------------------------------------------------------------------
# The shipped writer fixture
# ---------------------------------------------------------------------------

def test_writer_fixture_is_registered_on_import():
    import waveflow.calib.fixtures  # noqa: F401 — registers on import
    fx = get("mem_w_stream_framed_done_task")
    assert fx.basis == ["nwords", "num_trans", "n_fwd"]


def test_writer_component_id_matches_a_real_writer():
    """The hardcoded component id must equal the task-body id a real inband+done writer carries —
    else its firings never match the library slot."""
    from waveflow.hw.mem_stream import MemWStream
    from waveflow.simulation.simulation import Simulation
    wr = MemWStream(name="wr", sim=Simulation(), mem_dwidth=64, inband=True, emit_done=True)
    assert wr.kernel_task().task_fn == "mem_w_stream_framed_done_task"


def test_writer_sweep_is_a_grid():
    from waveflow.calib.fixtures.mem_w_stream import MemWStreamFixture
    fx = MemWStreamFixture(nwords_grid=(128, 512), n_fwd_grid=(1, 2, 4))
    pts = fx.sweep()
    assert len(pts) == 6
    # n_fwd varies at each nwords (a grid, not a diagonal) — the separability precondition.
    assert {p.features["n_fwd"] for p in pts if p.features["nwords"] == 128} == {1, 2, 4}
    assert all(p.features["num_trans"] == (p.features["nwords"] + 15) // 16 for p in pts)


def test_writer_rtl_is_measured_only_at_n_fwd_1():
    from waveflow.calib.fixtures.mem_w_stream import MemWStreamFixture
    fx = MemWStreamFixture()
    got = {p.label: fx.rtl_firings(p) for p in fx.sweep()}
    assert got["n128_f1"] is not None and got["n512_f1"] is not None
    # n_fwd > 1 has no measured RTL — it needs a cosim, and the fixture says so rather than inventing.
    assert got["n128_f2"] is None and got["n128_f4"] is None


def test_writer_vehicle_runs_and_records_n_fwd():
    from waveflow.calib.fixtures.mem_w_stream import MemWStreamFixture
    fx = MemWStreamFixture(nwords_grid=(64,), n_fwd_grid=(1, 3), jobs=3)
    clk = Clock(freq=100e6)
    with tempfile.TemporaryDirectory() as d:
        comp = Path(d) / "comp"
        for pt in fx.sweep():
            recs = fx.run_pysim(pt, comp_dir=comp, platform_dir=None, clk=clk)
            assert len(recs) == 3                     # one firing per job
            for r in recs:
                assert r["n_fwd"] == pt.features["n_fwd"]
                assert r["nwords"] == 64
                assert {"span", "current_dly"} <= set(r)


# ---------------------------------------------------------------------------
# The shipped reader fixture
# ---------------------------------------------------------------------------

def test_reader_fixture_is_registered_on_import():
    import waveflow.calib.fixtures  # noqa: F401 — registers on import
    fx = get("mem_r_stream_framed_task")
    # The reader's timing feature set is just (nwords, num_trans): it relays the in-band descriptor but
    # does not model a per-forwarded-burst cost, so — unlike the writer — there is no n_fwd basis axis.
    assert fx.basis == ["nwords", "num_trans"]


def test_reader_component_id_matches_a_real_reader():
    from waveflow.hw.mem_stream import MemRStream
    from waveflow.simulation.simulation import Simulation
    rd = MemRStream(name="rd", sim=Simulation(), mem_dwidth=64, inband=True)
    assert rd.kernel_task().task_fn == "mem_r_stream_framed_task"


def test_reader_sweep_is_one_point_per_size():
    from waveflow.calib.fixtures.mem_r_stream import MemRStreamFixture
    fx = MemRStreamFixture(nwords_grid=(64, 128, 256))
    pts = fx.sweep()
    assert len(pts) == 3
    assert {p.features["nwords"] for p in pts} == {64, 128, 256}
    assert all(p.features["num_trans"] == (p.features["nwords"] + 15) // 16 for p in pts)
    assert all("n_fwd" not in p.features for p in pts)


def test_reader_rtl_is_measured_for_known_sizes():
    from waveflow.calib.fixtures.mem_r_stream import MemRStreamFixture
    fx = MemRStreamFixture(nwords_grid=(128, 999))
    got = {p.features["nwords"]: fx.rtl_firings(p) for p in fx.sweep()}
    assert got[128] is not None
    assert got[999] is None                       # no measured span — reported, not invented


def test_reader_vehicle_runs_and_records():
    from waveflow.calib.fixtures.mem_r_stream import MemRStreamFixture
    fx = MemRStreamFixture(nwords_grid=(64,), jobs=3)
    clk = Clock(freq=100e6)
    with tempfile.TemporaryDirectory() as d:
        comp = Path(d) / "comp"
        for pt in fx.sweep():
            recs = fx.run_pysim(pt, comp_dir=comp, platform_dir=None, clk=clk)
            assert len(recs) == 3                 # one firing per job
            for r in recs:
                assert r["nwords"] == 64
                assert {"span", "current_dly"} <= set(r)


def test_reader_fixture_fits_from_measured_spans():
    from waveflow.calib.fixtures.mem_r_stream import MemRStreamFixture
    clk = Clock(freq=100e6)
    with tempfile.TemporaryDirectory() as d:
        plat = Platform.resolve(Path(d) / "p", "p", part="xc7z020clg484-1", clk_freq=100e6)
        report = MemRStreamFixture(jobs=4).calibrate(plat, clk=clk)
    assert report["fitted"] and report["missing_rtl"] == []
    assert report["component"] == "mem_r_stream_framed_task"


class _SynthWriter:
    """A writer fixture whose RTL span follows a known law, to test identifiability without cosim."""

    def __init__(self, k_fwd: float):
        from waveflow.calib.fixtures.mem_w_stream import MemWStreamFixture
        self._fx = MemWStreamFixture(nwords_grid=(64,), n_fwd_grid=(1, 2, 4), jobs=3)
        self._k = k_fwd

    def calibrate(self, platform, *, clk):
        k = self._k
        base = self._fx

        class Law(type(base)):
            def rtl_firings(self, point):
                nw, nt, nf = (point.features["nwords"], point.features["num_trans"],
                              point.features["n_fwd"])
                span = 40 + 1.0 * nw + 2.0 * (nt - 1) + k * nf
                return {"top": "mem_w_stream", "max_burst_len": 16,
                        "firings": [{"component": self.component, "index": 0, "nwords": nw,
                                     "num_trans": nt, "n_fwd": nf, "span": span, "blocked": 0}]}

        fx = Law(nwords_grid=(64,), n_fwd_grid=(1, 2, 4), jobs=3)
        return fx.calibrate(platform, clk=clk, reset=True)


def test_calibrate_identifies_the_n_fwd_coefficient():
    """The whole point: a fixture that *varies* n_fwd recovers its per-message cost.  The pysim already
    models part of the buffering, so the residual's absolute n_fwd coefficient is netted — but injecting
    an extra ``K`` cyc/message into the RTL law shifts the fitted coefficient by exactly ``K`` (same
    pysim both times), which is the separability memcpy's fixed n_fwd can never show."""
    clk = Clock(freq=100e6)
    K = 13.0
    with tempfile.TemporaryDirectory() as d:
        p0 = Platform.resolve(Path(d) / "a", "p", part="xc7z020clg484-1", clk_freq=100e6)
        p1 = Platform.resolve(Path(d) / "b", "p", part="xc7z020clg484-1", clk_freq=100e6)
        r0 = _SynthWriter(k_fwd=0.0).calibrate(p0, clk=clk)
        r1 = _SynthWriter(k_fwd=K).calibrate(p1, clk=clk)
    assert r0["fitted"] and r1["fitted"]
    assert r0["missing_rtl"] == [] and r1["missing_rtl"] == []
    shift = r1["params"]["n_fwd"] - r0["params"]["n_fwd"]
    assert shift == pytest.approx(K, abs=0.3), f"n_fwd coefficient shift {shift} != injected {K}"
