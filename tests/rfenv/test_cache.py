"""Step 9 of plans/sionna_rt/sionna_rt.md: the trace cache and its build step.

The tracer is replaced by a counting stand-in in the tests without the marker, so they check the
cache's decisions -- reuse, retrace, refuse -- with no Sionna; one ``sionna``-marked test does it for
real.
"""

from __future__ import annotations

import numpy as np
import pytest

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.rfenv import cache, sionna_backend
from waveflow.rfenv.cache import (
    TraceChannelStep,
    installed_tracer_versions,
    load_or_trace,
)
from waveflow.rfenv.paths import ABSOLUTE_DELAYS, SCHEMA_VERSION, PathSet, channel_map
from waveflow.rfenv.sionna_backend import DeviceSpec, SceneSpec, SolverSpec


def spec(z: float = 10.0) -> SceneSpec:
    return SceneSpec(
        scene="floor_wall",
        frequency_hz=3.5e9,
        transmitters=(DeviceSpec("tx", (-10.0, 0.0, z)),),
        receivers=(DeviceSpec("rx", (10.0, 0.0, z)),),
        solver=SolverSpec(max_depth=1),
    )


def fake_trace(s: SceneSpec, versions: dict | None = None, a: float = 1e-3) -> PathSet:
    """A channel file that claims to be the trace of ``s`` with the given tracer versions."""
    d = s.to_dict()
    return PathSet(
        a=[[[a]]],
        tau=[[[6.7e-8]]],
        doppler=[[[0.0]]],
        valid=[[[True]]],
        manifest={
            "schema_version": SCHEMA_VERSION,
            "carrier_hz": 3.5e9,
            "delays": ABSOLUTE_DELAYS,
            "channels": channel_map([("rx", 0)], [("tx", 0)]),
            "generator": {"name": "fake", "versions": versions or {}},
            "spec": d,
            "spec_hash": s.hash(),
            "created": "2026-10-12",
        },
    )


@pytest.fixture
def tracer(monkeypatch):
    """Replace the tracer: record each call, return a fresh fake trace marked with a = 2e-3."""
    calls = []

    def fake(s):
        calls.append(s.hash())
        return fake_trace(s, installed_tracer_versions(), a=2e-3)

    monkeypatch.setattr(sionna_backend, "trace", fake)
    return calls


def test_a_file_that_describes_the_scene_is_reused(tmp_path, tracer):
    fake_trace(spec(), installed_tracer_versions()).save(tmp_path / "chan")
    ps = load_or_trace(spec(), tmp_path / "chan")
    assert tracer == [] and ps.a[0, 0, 0] == 1e-3


def test_a_changed_specification_is_traced_and_saved(tmp_path, tracer):
    fake_trace(spec(), installed_tracer_versions()).save(tmp_path / "chan")
    ps = load_or_trace(spec(z=9.0), tmp_path / "chan")
    assert tracer == [spec(z=9.0).hash()] and ps.a[0, 0, 0] == 2e-3
    assert PathSet.load(tmp_path / "chan").manifest["spec_hash"] == spec(z=9.0).hash()


def test_a_missing_file_is_traced(tmp_path, tracer):
    load_or_trace(spec(), tmp_path / "sub" / "chan")
    assert tracer == [spec().hash()] and (tmp_path / "sub" / "chan.npz").is_file()


@pytest.mark.skipif(
    installed_tracer_versions() is None, reason="needs the tracer's versions"
)
def test_a_file_traced_by_other_versions_is_traced_again(tmp_path, tracer):
    old = dict(installed_tracer_versions(), **{"sionna-rt": "2.1.0"})
    fake_trace(spec(), old).save(tmp_path / "chan")
    load_or_trace(spec(), tmp_path / "chan")
    assert tracer == [spec().hash()]


def test_without_the_tracer_installed_a_matching_file_is_used(
    tmp_path, tracer, monkeypatch
):
    monkeypatch.setattr(cache, "installed_tracer_versions", lambda: None)
    fake_trace(spec(), {"sionna-rt": "1.0.0"}).save(tmp_path / "chan")
    assert load_or_trace(spec(), tmp_path / "chan").a[0, 0, 0] == 1e-3
    assert tracer == []


def test_never_uses_a_matching_file_and_refuses_a_different_one(tmp_path, tracer):
    fake_trace(spec(), {"sionna-rt": "0.0"}).save(tmp_path / "chan")
    assert load_or_trace(spec(), tmp_path / "chan", retrace="never").a[0, 0, 0] == 1e-3
    with pytest.raises(ValueError, match="retrace='never'"):
        load_or_trace(spec(z=9.0), tmp_path / "chan", retrace="never")
    assert tracer == []


def test_always_traces(tmp_path, tracer):
    fake_trace(spec(), installed_tracer_versions()).save(tmp_path / "chan")
    load_or_trace(spec(), tmp_path / "chan", retrace="always")
    assert tracer == [spec().hash()]


def test_a_bad_policy_is_refused(tmp_path):
    with pytest.raises(ValueError, match="retrace"):
        load_or_trace(spec(), tmp_path / "chan", retrace="sometimes")


def test_the_build_step_traces_once_then_skips(tmp_path, tracer):
    dag = BuildDag()
    dag.add(TraceChannelStep(name="trace", spec=spec(), stem="data/chan"))
    cfg = BuildConfig(root_dir=tmp_path)
    first = dag.run(cfg)
    assert first["trace"].success and not first["trace"].skipped
    assert tracer == [spec().hash()] and (tmp_path / "data" / "chan.json").is_file()
    second = dag.run(cfg)
    assert second["trace"].skipped and tracer == [spec().hash()]


@pytest.mark.sionna
def test_a_real_trace_is_cached_and_retraced_when_the_scene_changes(
    tmp_path, monkeypatch
):
    pytest.importorskip("sionna.rt")
    first = load_or_trace(spec(), tmp_path / "chan")
    calls = []
    real = sionna_backend.trace
    monkeypatch.setattr(sionna_backend, "trace", lambda s: calls.append(1) or real(s))
    again = load_or_trace(spec(), tmp_path / "chan")
    assert calls == [] and np.array_equal(again.a, first.a)
    moved = load_or_trace(spec(z=9.0), tmp_path / "chan")
    assert calls == [1] and not np.array_equal(moved.tau, first.tau)
