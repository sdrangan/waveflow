"""Step 13 of plans/sionna_rt/sionna_rt.md: live mode (AC8).

A node in live mode traces its ``SceneSpec`` at ``pre_sim`` instead of loading a channel file.  On
the install that wrote the cached file (same sionna-rt, Mitsuba and Dr.Jit versions, same variant),
it gives the same samples -- the cached reference ``floor_wall_siso`` was traced here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from tests.rfenv._reference import DATA, REFERENCES
from tests.rfenv.test_nodes import BLK, play, rel_err, run, signal, source_graph
from waveflow.rfenv.cache import describes
from waveflow.rfenv.paths import PathSet
from waveflow.rfenv.sionna_backend import INSTALL_HINT
from waveflow.rfenv.waveforms import ZadoffChu

REPO = Path(__file__).resolve().parents[2]
SPEC = REFERENCES["floor_wall_siso"]


@pytest.fixture
def same_install():
    """The cached reference must come from this install, or AC8's 1e-6 does not apply."""
    pytest.importorskip("sionna.rt")
    from waveflow.rfenv.sionna_backend import import_sionna

    _, mi = import_sionna()
    ref = PathSet.load(DATA / "floor_wall_siso")
    assert (
        describes(ref, SPEC) and ref.manifest["generator"]["variant"] == mi.variant()
    ), (
        "the committed reference was traced by another install; regenerate it with "
        "python -m tests.rfenv._reference before comparing live and cached"
    )


@pytest.mark.sionna
def test_a_live_channel_gives_the_cached_samples(same_install):
    x = signal(1, 16, seed=31)
    live, _, y_live, _, b = play(x, 16, 1, spec=SPEC, mode="live")
    _, _, y_cached, _, _ = play(x, 16, 1, channel=DATA / "floor_wall_siso")
    assert live.paths.manifest["spec_hash"] == SPEC.hash()
    assert rel_err(y_live, y_cached) <= 1e-6
    b.assert_clean()


@pytest.mark.sionna
def test_a_live_source_gives_the_cached_samples(same_install):
    kw = {
        "waveform": ZadoffChu(length=139, root=5),
        "tx_power_dbm": 20.0,
        "gain_db": 60.0,
    }
    sim_l, src_l, snk_l, _ = source_graph(1, 8, spec=SPEC, mode="live", **kw)
    run(sim_l, 10)
    sim_c, _, snk_c, _ = source_graph(1, 8, channel=DATA / "floor_wall_siso", **kw)
    run(sim_c, 10)
    got, want = np.concatenate(snk_l.got, axis=1), np.concatenate(snk_c.got, axis=1)
    assert got.shape == want.shape == (1, 8 * BLK)
    assert rel_err(got, want) <= 1e-6 and src_l.paths is not None


def test_live_mode_without_sionna_names_the_extra():
    code = (
        "import tests.rfenv._block_sionna as b; b.install()\n"
        "from tests.rfenv.test_live import SPEC\n"
        "from tests.rfenv.test_nodes import channel_graph\n"
        "chan = channel_graph(1, 1, 2, spec=SPEC, mode='live')[2]\n"
        "try:\n"
        "    chan.pre_sim()\n"
        "except ImportError as e:\n"
        "    print('hint:', e)\n"
    )
    run_ = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run_.returncode == 0, run_.stderr[-2000:]
    assert f"hint: {INSTALL_HINT}" in run_.stdout


def test_a_cached_spec_without_sionna_uses_its_file(tmp_path):
    """Cached mode with a spec needs Sionna only when the file is missing or stale; a committed
    file is enough, here under the blocker."""
    code = (
        "import shutil, tests.rfenv._block_sionna as b; b.install()\n"
        "from tests.rfenv._reference import DATA\n"
        "from tests.rfenv.test_live import SPEC\n"
        "from tests.rfenv.test_nodes import play, signal\n"
        f"dst = {str(tmp_path)!r} + '/chan'\n"
        "for ext in ('.npz', '.json'):\n"
        "    shutil.copy(str(DATA / 'floor_wall_siso') + ext, dst + ext)\n"
        "chan = play(signal(1, 4, 1), 4, 1, spec=SPEC, channel=dst)[0]\n"
        "print('paths:', int(chan.paths.valid.sum()))\n"
    )
    run_ = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run_.returncode == 0, run_.stderr[-2000:]
    assert "paths: 2" in run_.stdout
