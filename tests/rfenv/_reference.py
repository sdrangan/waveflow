"""The committed reference channel files: traced once with Sionna RT, used by tests that run without it.

Each file in ``tests/rfenv/data/`` is the trace of one :class:`SceneSpec` below, so it can be
regenerated, and a test can check that the committed bytes still belong to that specification
(``load_or_trace(..., retrace="never")`` refuses a file whose hash differs).  Regenerate with::

    python -m tests.rfenv._reference

which traces only what is missing or stale (sionna-rt, Mitsuba or Dr.Jit versions changed).
"""

from __future__ import annotations

from pathlib import Path

from waveflow.rfenv.cache import load_or_trace
from waveflow.rfenv.paths import PathSet
from waveflow.rfenv.sionna_backend import ArraySpec, DeviceSpec, SceneSpec, SolverSpec

DATA = Path(__file__).resolve().parent / "data"

#: The reference scenes, by file stem.
REFERENCES: dict[str, SceneSpec] = {
    # SISO over floor_wall's concrete floor, the receiver moving: two paths with distinct Doppler.
    "floor_wall_siso": SceneSpec(
        scene="floor_wall",
        frequency_hz=3.5e9,
        transmitters=(DeviceSpec("tx", (-10.0, 0.0, 10.0)),),
        receivers=(DeviceSpec("rx", (10.0, 0.0, 10.0), velocity=(-20.0, 3.0, 0.0)),),
        solver=SolverSpec(max_depth=1),
    ),
    # A 1x4 receive array off broadside in free space, moving: four channels with distinct phases.
    "empty_1x4": SceneSpec(
        scene="empty",
        frequency_hz=3.5e9,
        transmitters=(DeviceSpec("tx", (0.0, 0.0, 10.0)),),
        receivers=(DeviceSpec("rx", (60.0, 80.0, 10.0), velocity=(-3.0, -4.0, 0.0)),),
        rx_array=ArraySpec(num_rows=1, num_cols=4),
    ),
}


def reference(name: str) -> PathSet:
    """A committed reference channel, checked against its specification and never retraced."""
    return load_or_trace(REFERENCES[name], DATA / name, retrace="never")


def regenerate() -> None:
    for name, spec in REFERENCES.items():
        ps = load_or_trace(spec, DATA / name)
        print(
            f"{name}: {int(ps.valid.sum())} valid paths, {ps.n_rx_ch}x{ps.n_tx_ch} channels"
        )


if __name__ == "__main__":
    regenerate()
