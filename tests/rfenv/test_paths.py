"""Step 4 of plans/sionna_rt/sionna_rt.md: the channel file (``PathSet``), numpy only."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from tests.rfenv._pathsets import manifest, pathset, random_pathset
from waveflow.rfenv.paths import PathSet, spec_hash

REPO = Path(__file__).resolve().parents[2]


def test_a_channel_file_round_trips_bit_for_bit(tmp_path):
    ps = random_pathset(4, 2, 7, seed=1)
    npz, js = ps.save(tmp_path / "chan")
    assert (npz.name, js.name) == ("chan.npz", "chan.json")
    for stem in (tmp_path / "chan", npz, js):
        back = PathSet.load(stem)
        for name in ("a", "tau", "doppler", "valid"):
            got, want = getattr(back, name), getattr(ps, name)
            assert got.dtype == want.dtype and np.array_equal(got, want), name
        assert back.manifest == ps.manifest


@pytest.mark.parametrize("name", ["sounder_3.5ghz", "chan.v2"])
def test_a_dotted_stem_keeps_every_character(tmp_path, name):
    a = pathset([[[1e-3]]], [[[1e-6]]], tag=name)
    b = pathset([[[2e-3]]], [[[1e-6]]], tag=name + "x")
    npz, js = a.save(tmp_path / name)
    b.save(tmp_path / (name + "x"))
    assert (npz.name, js.name) == (name + ".npz", name + ".json")
    assert PathSet.load(tmp_path / name).a[0, 0, 0] == 1e-3
    assert PathSet.load(npz).a[0, 0, 0] == 1e-3


def test_a_float32_carrier_is_accepted_as_a_plain_float():
    m = manifest(1, 1, carrier_hz=np.float32(3.5e9))
    ps = PathSet(
        a=[[[1e-3]]], tau=[[[1e-6]]], doppler=[[[0.0]]], valid=[[[True]]], manifest=m
    )
    assert type(ps.carrier_hz) is float and type(ps.manifest["carrier_hz"]) is float
    json.dumps(ps.manifest)


def test_arrays_are_canonical_and_read_only():
    ps = pathset([[[1e-3]]], [[[1e-6]]])
    assert ps.a.dtype == np.complex128 and ps.tau.dtype == np.float64
    assert ps.valid.dtype == bool and ps.doppler.dtype == np.float64
    with pytest.raises(ValueError):
        ps.tau[0, 0, 0] = 2e-6
    assert (ps.n_rx_ch, ps.n_tx_ch, ps.n_paths) == (1, 1, 1)
    assert ps.carrier_hz == 3.5e9


def test_statistics_ignore_padding():
    # Sionna pads unused path slots with tau = -1 and a = 0; they must not count.
    ps = pathset(
        [[[1e-3, 0.0, 2e-3]], [[0.5e-3, 0.0, 0.0]]],
        [[[2e-6, -1.0, 5e-6]], [[1e-6, -1.0, -1.0]]],
        valid=[[[1, 0, 1]], [[1, 0, 0]]],
    )
    assert ps.min_delay() == 1e-6 and ps.max_delay() == 5e-6
    assert np.allclose(ps.power_gain(), [[1e-6 + 4e-6], [0.25e-6]])


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"a": np.zeros((2, 2))}, r"\[n_rx_ch, n_tx_ch, n_paths\]"),
        ({"tau": np.zeros((1, 1, 2))}, "tau has shape"),
        ({"tau": [[[-1e-9]]]}, "non-negative absolute delay"),
        ({"tau": [[[np.nan]]]}, "non-negative absolute delay"),
        ({"doppler": [[[np.inf]]]}, "non-finite Doppler"),
        ({"a": [[[np.nan]]]}, "non-finite amplitude"),
    ],
)
def test_bad_arrays_are_refused(change, match):
    kw = {"a": [[[1e-3]]], "tau": [[[1e-6]]], "doppler": [[[0.0]]], "valid": [[[True]]]}
    kw.update(change)
    with pytest.raises(ValueError, match=match):
        PathSet(manifest=manifest(1, 1), **kw)


def test_padding_may_hold_anything():
    # An invalid slot is not a path; its contents are never read.
    ps = pathset([[[1e-3, np.nan]]], [[[1e-6, -1.0]]], valid=[[[True, False]]])
    assert ps.min_delay() == 1e-6


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda m: m.pop("generator"), r"lacks \['generator'\]"),
        (lambda m: m.update(schema_version=99), "schema_version 99"),
        (lambda m: m.update(delays="relative"), "must be 'absolute'"),
        (lambda m: m.update(carrier_hz=0), "carrier_hz must be a positive number"),
        (lambda m: m.update(carrier_hz=True), "carrier_hz must be a positive number"),
        (
            lambda m: m["channels"]["rx"].append({"device": "x", "antenna": 0}),
            "channel map",
        ),
        (lambda m: m["spec"].update(extra=1), "spec_hash does not match"),
    ],
)
def test_a_bad_manifest_is_refused(edit, match):
    m = manifest(1, 1)
    edit(m)
    with pytest.raises(ValueError, match=match):
        PathSet(
            a=[[[1e-3]]],
            tau=[[[1e-6]]],
            doppler=[[[0.0]]],
            valid=[[[True]]],
            manifest=m,
        )


def test_a_channel_map_that_is_not_a_dict_is_a_type_error():
    m = manifest(1, 1)
    m["channels"] = ["rx0"]
    with pytest.raises(TypeError, match="channel map must be a dict"):
        PathSet(
            a=[[[1e-3]]],
            tau=[[[1e-6]]],
            doppler=[[[0.0]]],
            valid=[[[True]]],
            manifest=m,
        )


def test_load_names_the_missing_part(tmp_path):
    pathset([[[1e-3]]], [[[1e-6]]]).save(tmp_path / "c")
    (tmp_path / "c.json").unlink()
    with pytest.raises(FileNotFoundError, match="c.json"):
        PathSet.load(tmp_path / "c")


def test_an_edited_file_is_refused(tmp_path):
    _, js = pathset([[[1e-3]]], [[[1e-6]]]).save(tmp_path / "c")
    m = json.loads(js.read_text())
    m["spec"]["kind"] = "edited"
    js.write_text(json.dumps(m))
    with pytest.raises(ValueError, match="spec_hash"):
        PathSet.load(tmp_path / "c")


def test_spec_hash_is_canonical():
    assert spec_hash({"b": 1, "a": [1, 2]}) == spec_hash({"a": [1, 2], "b": 1})
    assert spec_hash({"a": 1}) != spec_hash({"a": 1.5})


def test_the_package_imports_without_sionna():
    """``waveflow.rfenv`` and a channel file need no Sionna: run them under the import blocker."""
    code = (
        "import tests.rfenv._block_sionna as b; b.install()\n"
        "import waveflow.rfenv\n"
        "from tests.rfenv._pathsets import random_pathset\n"
        "random_pathset(2, 1, 3, seed=0)\n"
        "try:\n"
        "    import sionna.rt\n"
        "except ModuleNotFoundError as e:\n"
        "    print('blocked:', e.name, e)\n"
        "else:\n"
        "    raise SystemExit('sionna imported despite the blocker')\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert "blocked: sionna No module named 'sionna'" in run.stdout


def test_saving_twice_gives_the_same_bytes(tmp_path):
    """No clock in the zip: a regenerated channel file is not a spurious diff."""
    ps = random_pathset(2, 1, 3, seed=2)
    a, _ = ps.save(tmp_path / "a")
    b, _ = ps.save(tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()
