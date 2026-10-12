"""examples/rt_sounder (plans/sionna_rt/sionna_rt.md, M5): the ray-traced channel sounder.

Runs without Sionna: the channel is the committed ``examples/rt_sounder/data/street_canyon`` file.
``tests/rfenv/test_no_sionna.py`` runs this module under the Sionna import blocker too.
"""

from __future__ import annotations

import math

import numpy as np

from examples.rt_sounder.rt_sounder import SAMP_RATE, SPEC, channel
from waveflow.rfenv.apply import taps


def test_the_committed_channel_belongs_to_the_scene():
    # load_or_trace(..., retrace="never") refuses a file traced for another spec.
    ps = channel()
    assert ps.manifest["spec_hash"] == SPEC.hash()
    assert (ps.n_rx_ch, ps.n_tx_ch) == (1, 1) and int(ps.valid.sum()) == 5


def test_the_channel_has_a_cluster_and_a_distant_echo():
    """Taps above −30 dB: the cluster across lags 23-37 and the echo at lags 43-44."""
    ps = channel()
    h = taps(ps, SAMP_RATE, 0, math.ceil(ps.max_delay() * SAMP_RATE) + 4)[0, 0, 0]
    db = 20 * np.log10(np.abs(h) / np.abs(h).max())
    strong = np.flatnonzero(db > -30).tolist()
    assert strong == list(range(23, 38)) + [43, 44]
    assert int(np.argmax(np.abs(h))) in (30, 31)
