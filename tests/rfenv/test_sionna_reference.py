"""Step 8 of plans/sionna_rt/sionna_rt.md: the traced channel against closed forms and Sionna itself.

* AC1: free space -- one path, the delay d/c, the power gain Friis.
* AC2: a reflecting plate -- the image-method delays, and the reflection coefficient of the plate's
  own material (read from the scene) by the ITU-R P.2040 slab formula, TM at 45 degrees.
* AC4: Doppler -- Sionna's per-path shift against v.k/lambda for a moving receiver, a moving
  transmitter, both, and a reflected path.
* AC5: ``rfenv.taps`` against Sionna's ``Paths.taps`` on the same trace, SISO and 1x4.

All ``sionna``-marked: they trace.  Numbers are Sionna RT 2.2.0 in single precision (the bounds in
the plan's §4 allow for it).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from waveflow.rfenv.apply import taps
from waveflow.rfenv.sionna_backend import (
    ArraySpec,
    DeviceSpec,
    SceneSpec,
    SolverSpec,
    build_scene,
    trace,
    trace_with_paths,
)

C = 299_792_458.0
F = 3.5e9
LAM = C / F
E0 = 8.8541878128e-12

pytestmark = pytest.mark.sionna


@pytest.fixture(autouse=True)
def _sionna():
    pytest.importorskip("sionna.rt")


def los_spec(
    tx_pos, rx_pos, tx_vel=(0.0, 0.0, 0.0), rx_vel=(0.0, 0.0, 0.0), **kw
) -> SceneSpec:
    args = {
        "scene": "empty",
        "frequency_hz": F,
        "transmitters": (DeviceSpec("tx", tx_pos, velocity=tx_vel),),
        "receivers": (DeviceSpec("rx", rx_pos, velocity=rx_vel),),
    }
    return SceneSpec(**(args | kw))


def plate_spec(
    scene: str, rx_pos=(10.0, 0.0, 10.0), rx_vel=(0.0, 0.0, 0.0), **kw
) -> SceneSpec:
    """Tx (-10, 0, 10) and an Rx 10 m above a plate at z = 0: 45 degree incidence."""
    args = {
        "scene": scene,
        "frequency_hz": F,
        "transmitters": (DeviceSpec("tx", (-10.0, 0.0, 10.0)),),
        "receivers": (DeviceSpec("rx", rx_pos, velocity=rx_vel),),
        "solver": SolverSpec(max_depth=1),
    }
    return SceneSpec(**(args | kw))


def friis_db(d: float) -> float:
    return 20 * math.log10(LAM / (4 * math.pi * d))


def valid_paths(ps):
    """``(tau, |a|^2 dB, doppler)`` of link (0, 0)'s valid paths, sorted by delay."""
    v = ps.valid[0, 0]
    order = np.argsort(ps.tau[0, 0][v])
    return (
        ps.tau[0, 0][v][order],
        10 * np.log10(np.abs(ps.a[0, 0][v][order]) ** 2),
        ps.doppler[0, 0][v][order],
    )


# --- AC1: free space --------------------------------------------------------------------------


def test_free_space_is_one_path_at_d_over_c_and_friis():
    d = 100.0
    ps = trace(los_spec((0.0, 0.0, 10.0), (d, 0.0, 10.0)))
    tau, gain_db, _ = valid_paths(ps)
    assert tau.size == 1
    assert abs(tau[0] - d / C) <= 1e-12
    assert abs(gain_db[0] - friis_db(d)) <= 0.01


# --- AC2: one reflecting plate ----------------------------------------------------------------


def slab_tm_db(eps_r: float, sigma: float, thickness: float, theta: float) -> float:
    """ITU-R P.2040 slab reflection |R|^2 in dB, TM (E in the plane of incidence)."""
    eta = eps_r - 1j * sigma / (2 * math.pi * F * E0)
    ci, si = math.cos(theta), math.sin(theta)
    root = np.sqrt(eta - si**2)
    r = (eta * ci - root) / (eta * ci + root)
    q = 2 * math.pi * thickness / LAM * root
    big = r * (1 - np.exp(-2j * q)) / (1 - r**2 * np.exp(-2j * q))
    return float(10 * np.log10(abs(big) ** 2))


def plate_material(scene_name: str, obj: str) -> tuple[float, float, float]:
    """``(eps_r, sigma, thickness)`` of a scene object's radio material at F, read from the scene."""
    scene = build_scene(plate_spec(scene_name))
    m = scene.objects[obj].radio_material

    def first(x):
        return float(np.asarray(x).ravel()[0])

    return first(m.relative_permittivity), first(m.conductivity), first(m.thickness)


@pytest.mark.parametrize(
    ("scene", "obj"), [("floor_wall", "floor"), ("simple_reflector", "merged-shapes")]
)
def test_a_single_reflector_reflects_as_its_material_says(scene, obj):
    ps = trace(plate_spec(scene))
    tau, gain_db, _ = valid_paths(ps)
    d_los, d_ref = 20.0, math.hypot(20.0, 20.0)
    assert tau.size == 2
    assert abs(tau[0] - d_los / C) <= 1e-12 and abs(tau[1] - d_ref / C) <= 1e-12
    # |R|^2: the reflected path against free space over the unfolded path.
    measured = gain_db[1] - friis_db(d_ref)
    expected = slab_tm_db(*plate_material(scene, obj), theta=math.radians(45.0))
    assert abs(measured - expected) <= 0.5, (measured, expected)


# --- AC4: Doppler ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tx_vel", "rx_vel"),
    [
        ((0.0, 0.0, 0.0), (-12.0, 5.0, 0.0)),  # receiver: 3.2 m/s closing
        ((3.0, 4.0, 0.0), (0.0, 0.0, 0.0)),  # transmitter: 5 m/s closing
        ((3.0, 4.0, 0.0), (-12.0, 5.0, 0.0)),  # both: 8.2 m/s closing
    ],
)
def test_doppler_is_the_closing_speed_over_lambda(tx_vel, rx_vel):
    tx, rx = np.array([0.0, 0.0, 10.0]), np.array([60.0, 80.0, 10.0])
    k = (rx - tx) / np.linalg.norm(rx - tx)  # the direction of travel, tx to rx
    expected = (np.dot(tx_vel, k) - np.dot(rx_vel, k)) / LAM
    ps = trace(los_spec(tuple(tx), tuple(rx), tx_vel, rx_vel))
    _, _, doppler = valid_paths(ps)
    assert abs(doppler[0] - expected) <= 0.1, (doppler[0], expected)


def test_a_reflected_path_has_its_own_doppler():
    """A receiver moving at 20 m/s toward -x: the line of sight closes at 20 m/s, the floor
    reflection (arriving along (1, 0, 1)/sqrt 2) at 20/sqrt 2 m/s."""
    v = 20.0
    ps = trace(plate_spec("floor_wall", rx_vel=(-v, 0.0, 0.0)))
    _, _, doppler = valid_paths(ps)
    assert abs(doppler[0] - v / LAM) <= 0.1
    assert abs(doppler[1] - v / math.sqrt(2) / LAM) <= 0.1


# --- AC5: taps against Sionna's own ---------------------------------------------------------


def rel_err(got, want) -> float:
    return float(np.linalg.norm(got - want) / np.linalg.norm(want))


@pytest.mark.parametrize(
    "spec",
    [
        plate_spec("floor_wall", rx_vel=(-20.0, 3.0, 0.0)),
        plate_spec(
            "floor_wall",
            rx_pos=(10.0, 2.0, 10.0),
            rx_vel=(-20.0, 3.0, 0.0),
            rx_array=ArraySpec(num_rows=1, num_cols=4),
        ),
    ],
    ids=["siso", "1x4"],
)
def test_taps_match_sionnas_on_the_same_trace(spec):
    """W = 122.88 MHz over every lag the paths reach, 4 time steps at 1 kHz (so the Doppler rotates
    by about 2 rad per step), absolute delays on both sides."""
    ps, paths = trace_with_paths(spec)
    w = 122.88e6
    l_max = math.ceil(ps.max_delay() * w) + 8
    mine = taps(ps, w, 0, l_max, sampling_frequency=1e3, num_time_steps=4)
    theirs = np.asarray(
        paths.taps(
            w,
            0,
            l_max,
            sampling_frequency=1e3,
            num_time_steps=4,
            normalize=False,
            normalize_delays=False,
            out_type="numpy",
        )
    )
    # [rx, rx_ant, tx, tx_ant, t, l] -> [rx_ch, tx_ch, t, l]
    theirs = theirs.reshape(mine.shape)
    assert rel_err(mine, theirs) <= 1e-3
