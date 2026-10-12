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


def test_a_file_traced_by_other_versions_is_traced_again(tmp_path, tracer, monkeypatch):
    here = {"sionna-rt": "2.2.0", "mitsuba": "3.9.1", "drjit": "1.5.0"}
    monkeypatch.setattr(cache, "installed_tracer_versions", lambda: here)
    fake_trace(spec(), dict(here, **{"sionna-rt": "2.1.0"})).save(tmp_path / "chan")
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


def test_never_refuses_a_missing_part_without_tracing(tmp_path, tracer):
    fake_trace(spec(), {}).save(tmp_path / "chan")
    (tmp_path / "chan.json").unlink()
    with pytest.raises(FileNotFoundError, match=r"chan\.json"):
        load_or_trace(spec(), tmp_path / "chan", retrace="never")
    with pytest.raises(FileNotFoundError):
        load_or_trace(spec(), tmp_path / "nothing", retrace="never")
    assert tracer == [] and not (tmp_path / "chan.json").exists()


def test_a_file_from_another_schema_is_traced_again(tmp_path, tracer):
    import json

    _, js = fake_trace(spec(), installed_tracer_versions()).save(tmp_path / "chan")
    m = json.loads(js.read_text())
    m["schema_version"] = 99
    js.write_text(json.dumps(m))
    load_or_trace(spec(), tmp_path / "chan")
    assert tracer == [spec().hash()]


def test_the_never_step_is_fresh_whatever_the_versions(tmp_path, tracer):
    fake_trace(spec(), {"sionna-rt": "0.0"}).save(tmp_path / "chan")
    step = TraceChannelStep(
        name="t", spec=spec(), stem=tmp_path / "chan", retrace="never"
    )
    assert step.is_fresh(BuildConfig(root_dir=tmp_path), {}) is True


def test_the_blocker_hides_the_tracer_from_package_metadata():
    """Under the blocker the cache sees what a real install without the extra shows."""
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import tests.rfenv._block_sionna as b; b.install()\n"
        "from waveflow.rfenv.cache import installed_tracer_versions\n"
        "print('versions:', installed_tracer_versions())\n"
    )
    repo = Path(__file__).resolve().parents[2]
    run = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert "versions: None" in run.stdout


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
