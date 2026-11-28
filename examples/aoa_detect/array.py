"""array.py -- the receiver's array: a uniform rectangular array (URA) and its sector element.

``docs/examples/aoa_detect/problem.md`` (the signal) and ``workload.md`` (the geometry).  Shared by the
workload, which places emitters, and the golden, which scans directions, so both use one steering
vector and one element ordering.

**Geometry.**  ``n_h x n_v`` elements at half-wavelength spacing in a vertical plane facing the
sector's boresight.  A direction is ``(phi, eps)``: azimuth about boresight and elevation (negative
below the horizon), in radians.  Element ``i = n * n_h + m`` is column ``m`` (horizontal) and row ``n``
(vertical); its phase is ``pi * (m cos(eps) sin(phi) + n sin(eps))``.

``steering`` returns the **unit-modulus** vector ``a``; the unit-norm signature of the docs is
``u = a / sqrt(n_rx)``.
"""
from __future__ import annotations

import numpy as np


def steering(phi, eps, n_h: int, n_v: int) -> np.ndarray:
    """Unit-modulus steering vectors, shape ``(..., n_h * n_v)`` for directions of shape ``(...)``."""
    phi = np.asarray(phi, dtype=float)
    eps = np.asarray(eps, dtype=float)
    m = np.tile(np.arange(n_h), n_v)          # column of element i
    n = np.repeat(np.arange(n_v), n_h)        # row of element i
    ph = np.pi * (m * (np.cos(eps) * np.sin(phi))[..., None] + n * np.sin(eps)[..., None])
    return np.exp(1j * ph)


def element_gain_db(phi, eps, tilt: float) -> np.ndarray:
    """The 3GPP TR 38.901 sector element's gain in dBi, 8 dBi at its peak.

    ``tilt`` is the downtilt in radians (positive points the peak below the horizon); ``eps`` is the
    elevation (negative below the horizon).
    """
    a_h = 12.0 * (np.degrees(np.asarray(phi)) / 65.0) ** 2
    a_v = 12.0 * (np.degrees(np.asarray(eps) + tilt) / 65.0) ** 2
    return 8.0 - np.minimum(a_h + a_v, 30.0)
