"""Step 7 of plans/sionna_rt/sionna_rt.md: the Sionna RT backend.

The ``sionna``-marked tests trace scenes; the rest check the specification and the install hint and
run without Sionna.  Sionna is imported only inside the backend, so this module collects anywhere.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from waveflow.rfenv.paths import PathSet
from waveflow.rfenv.sionna_backend import (
    INSTALL_HINT,
    ArraySpec,
    DeviceSpec,
    SceneSpec,
    SolverSpec,
    trace,
)

REPO = Path(__file__).resolve().parents[2]


def floor_wall(**kw) -> SceneSpec:
    """The AC2 geometry: Tx and Rx 20 m apart, 10 m above floor_wall's concrete floor."""
    args = {
        "scene": "floor_wall",
        "frequency_hz": 3.5e9,
        "transmitters": (DeviceSpec("tx", (-10.0, 0.0, 10.0)),),
        "receivers": (DeviceSpec("rx", (10.0, 0.0, 10.0)),),
        "solver": SolverSpec(max_depth=1),
    }
    return SceneSpec(**(args | kw))


@pytest.fixture
def sionna():
    return pytest.importorskip("sionna.rt")


# --- the specification (no Sionna) ------------------------------------------------------------


def test_a_specification_round_trips_through_json():
    spec = floor_wall(rx_array=ArraySpec(num_rows=2, num_cols=2, polarization="cross"))
    d = spec.to_dict()
    back = SceneSpec.from_dict(json.loads(json.dumps(d)))
    assert back == spec and back.hash() == spec.hash()


def test_the_hash_follows_every_setting():
    base = floor_wall()
    changed = [
        floor_wall(solver=SolverSpec(max_depth=2)),
        floor_wall(rx_array=ArraySpec(num_cols=2)),
        SceneSpec(**{**base.__dict__, "frequency_hz": 28e9}),
        SceneSpec(
            **{**base.__dict__, "receivers": (DeviceSpec("rx", (10.0, 0.0, 9.0)),)}
        ),
    ]
    assert len({base.hash(), *(s.hash() for s in changed)}) == 5


def xml_spec(path) -> SceneSpec:
    return SceneSpec(
        scene=str(path),
        frequency_hz=3.5e9,
        transmitters=(DeviceSpec("tx", (0.0, 0.0, 1.0)),),
        receivers=(DeviceSpec("rx", (1.0, 0.0, 1.0)),),
    )


ROOM = """<scene version="2.1.0">
  <shape type="ply"><string name="filename" value="meshes/floor.ply"/></shape>
</scene>"""


def test_an_xml_scene_is_hashed_by_its_bytes_and_its_meshes(tmp_path, monkeypatch):
    (tmp_path / "meshes").mkdir()
    (tmp_path / "meshes" / "floor.ply").write_bytes(b"ply v1")
    xml = tmp_path / "room.xml"
    xml.write_text(ROOM)
    h = xml_spec(xml).hash()
    monkeypatch.chdir(tmp_path)
    assert xml_spec("room.xml").hash() == h  # relative or absolute: one identity
    (tmp_path / "meshes" / "floor.ply").write_bytes(b"ply v2")
    assert xml_spec(xml).hash() != h  # editing a mesh changes it
    (tmp_path / "meshes" / "floor.ply").unlink()
    with pytest.raises(FileNotFoundError):
        xml_spec(xml).hash()


def test_an_integer_frequency_is_the_same_scene():
    a = floor_wall(frequency_hz=3_500_000_000)
    assert a.frequency_hz == 3.5e9 and type(a.frequency_hz) is float
    assert a.hash() == floor_wall().hash()
    assert SceneSpec.from_dict(a.to_dict()).hash() == a.hash()


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"transmitters": ()}, "at least one transmitter"),
        ({"frequency_hz": 0.0}, "positive"),
        ({"receivers": (DeviceSpec("tx", (1.0, 0.0, 0.0)),)}, "unique"),
    ],
)
def test_a_bad_specification_is_refused(kw, match):
    args = {
        "scene": "empty",
        "frequency_hz": 3.5e9,
        "transmitters": (DeviceSpec("tx", (0.0, 0.0, 0.0)),),
        "receivers": (DeviceSpec("rx", (1.0, 0.0, 0.0)),),
    } | kw
    with pytest.raises(ValueError, match=match):
        SceneSpec(**args)


def test_coordinates_become_plain_float_triples():
    d = DeviceSpec("rx", np.array([1, 2, 3]), velocity=(np.float32(-1.5), 0, 0))
    assert d.position == (1.0, 2.0, 3.0) and type(d.position[0]) is float
    assert type(d.velocity[0]) is float
    with pytest.raises(ValueError, match="3 values"):
        DeviceSpec("rx", (1.0, 2.0))


def test_orientation_and_look_at_are_exclusive():
    with pytest.raises(ValueError, match="not both"):
        DeviceSpec(
            "tx", (0.0, 0.0, 0.0), orientation=(0.0, 0.0, 0.0), look_at=(1.0, 0.0, 0.0)
        )


def test_without_sionna_a_trace_names_the_extra():
    code = (
        "import tests.rfenv._block_sionna as b; b.install()\n"
        "from tests.rfenv.test_sionna_backend import floor_wall\n"
        "from waveflow.rfenv.sionna_backend import trace\n"
        "try:\n"
        "    trace(floor_wall())\n"
        "except ImportError as e:\n"
        "    print('hint:', e)\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert f"hint: {INSTALL_HINT}" in run.stdout


# --- tracing (Sionna RT) ----------------------------------------------------------------------


@pytest.mark.sionna
def test_a_built_in_scene_traces_into_a_valid_channel_file(sionna, tmp_path):
    ps = trace(floor_wall())
    assert (ps.n_rx_ch, ps.n_tx_ch) == (1, 1) and int(ps.valid.sum()) == 2
    assert ps.carrier_hz == 3.5e9
    gen = ps.manifest["generator"]
    assert gen["versions"]["sionna-rt"] == sionna.__version__ and gen["variant"]
    assert ps.manifest["spec_hash"] == floor_wall().hash()
    back = PathSet.load(ps.save(tmp_path / "floor_wall")[0])
    assert np.array_equal(back.a, ps.a) and back.manifest == ps.manifest


@pytest.mark.sionna
def test_two_traces_of_one_specification_are_identical(sionna):
    a, b = trace(floor_wall()), trace(floor_wall())
    for name in ("a", "tau", "doppler", "valid"):
        assert np.array_equal(getattr(a, name), getattr(b, name)), name


@pytest.mark.sionna
@pytest.mark.parametrize("synthetic", [True, False])
def test_an_array_flattens_to_one_channel_per_antenna(sionna, synthetic):
    """A 1x4 receive array off the transmitter's broadside: four channels, the same paths, and the
    per-antenna phase in a (with the synthetic array, tau and doppler are broadcast)."""
    spec = SceneSpec(
        scene="empty",
        frequency_hz=3.5e9,
        transmitters=(DeviceSpec("tx", (0.0, 0.0, 10.0)),),
        receivers=(DeviceSpec("rx", (60.0, 80.0, 10.0), velocity=(-3.0, -4.0, 0.0)),),
        rx_array=ArraySpec(num_rows=1, num_cols=4),
        solver=SolverSpec(synthetic_array=synthetic),
    )
    ps = trace(spec)
    assert ps.a.shape[:2] == (4, 1)
    names = [(c["device"], c["antenna"]) for c in ps.manifest["channels"]["rx"]]
    assert names == [("rx", i) for i in range(4)]
    v = ps.valid[:, 0, :]
    assert v.sum(axis=1).tolist() == [1, 1, 1, 1]
    phases = np.angle(ps.a[:, 0, :][v])
    assert np.ptp(np.unwrap(phases)) > 0.1  # off broadside, the antennas differ
    assert np.allclose(ps.doppler[:, 0, :][v], 5.0 / (299_792_458.0 / 3.5e9), rtol=1e-5)


@pytest.mark.sionna
def test_a_variant_requested_after_import_is_a_clear_error(sionna, monkeypatch):
    from waveflow.rfenv.sionna_backend import VARIANT_ENV, import_sionna

    import_sionna()  # sionna.rt is now imported
    monkeypatch.setenv(VARIANT_ENV, "scalar_rgb")
    with pytest.raises(RuntimeError, match="already imported"):
        import_sionna()


@pytest.mark.sionna
def test_a_variant_requested_before_import_is_used(sionna):
    code = (
        "from tests.rfenv.test_sionna_backend import floor_wall\n"
        "from waveflow.rfenv.sionna_backend import trace\n"
        "print('variant:', trace(floor_wall()).manifest['generator']['variant'])\n"
    )
    env = {
        **__import__("os").environ,
        "WAVEFLOW_SIONNA_VARIANT": "llvm_ad_mono_polarized",
    }
    run = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    assert "variant: llvm_ad_mono_polarized" in run.stdout


@pytest.mark.sionna
def test_a_dual_polarized_array_is_ordered_pattern_major(sionna):
    """A 1x2 "VH" receive array is 4 channels: positions 0-1 with V, then 0-1 with H.  A V-polarized
    transmitter in free space reaches the V elements and (almost) not the H ones."""
    spec = SceneSpec(
        scene="empty",
        frequency_hz=3.5e9,
        transmitters=(DeviceSpec("tx", (0.0, 0.0, 10.0)),),
        receivers=(DeviceSpec("rx", (60.0, 80.0, 10.0)),),
        rx_array=ArraySpec(num_rows=1, num_cols=2, polarization="VH"),
    )
    ps = trace(spec)
    assert ps.n_rx_ch == 4
    gain = np.abs(ps.a[:, 0, :]).max(axis=-1)
    assert np.all(gain[:2] > 1e-6) and np.all(gain[2:] < 1e-12)
