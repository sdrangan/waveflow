"""Synthetic channel files for the rfenv tests: a known set of paths, no ray tracer involved."""

from __future__ import annotations

import numpy as np

from waveflow.rfenv.paths import (
    ABSOLUTE_DELAYS,
    SCHEMA_VERSION,
    PathSet,
    channel_map,
    spec_hash,
)


def manifest(n_rx: int, n_tx: int, carrier_hz: float = 3.5e9, **spec) -> dict:
    """A complete manifest for a synthetic ``n_rx`` x ``n_tx`` channel."""
    spec = {"kind": "synthetic", **spec}
    return {
        "schema_version": SCHEMA_VERSION,
        "carrier_hz": carrier_hz,
        "delays": ABSOLUTE_DELAYS,
        "channels": channel_map(
            [("rx", i) for i in range(n_rx)], [("tx", i) for i in range(n_tx)]
        ),
        "generator": {"name": "tests.rfenv._pathsets"},
        "spec": spec,
        "spec_hash": spec_hash(spec),
        "created": "2026-10-12",
    }


def pathset(
    a, tau, doppler=None, valid=None, carrier_hz: float = 3.5e9, **spec
) -> PathSet:
    """A :class:`PathSet` from arrays shaped ``[n_rx, n_tx, n_paths]`` (lists are fine)."""
    a = np.asarray(a, dtype=np.complex128)
    tau = np.asarray(tau, dtype=np.float64)
    doppler = (
        np.zeros(a.shape) if doppler is None else np.asarray(doppler, dtype=np.float64)
    )
    valid = (
        np.ones(a.shape, dtype=bool) if valid is None else np.asarray(valid, dtype=bool)
    )
    return PathSet(
        a=a,
        tau=tau,
        doppler=doppler,
        valid=valid,
        manifest=manifest(a.shape[0], a.shape[1], carrier_hz, **spec),
    )


def random_pathset(
    n_rx: int, n_tx: int, n_paths: int, seed: int, max_tau: float = 1e-6
):
    """Random paths with some padding slots, as a ray tracer would return them."""
    rng = np.random.default_rng(seed)
    shape = (n_rx, n_tx, n_paths)
    a = (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) * 1e-4
    tau = rng.uniform(0.0, max_tau, shape)
    doppler = rng.uniform(-500.0, 500.0, shape)
    valid = rng.random(shape) > 0.2
    a = np.where(valid, a, 0.0)
    tau = np.where(valid, tau, -1.0)  # Sionna's padding
    return pathset(a, tau, doppler, valid, seed=seed)
