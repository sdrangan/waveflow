"""tests/calib/test_platform_resolver.py — the layered platform search path.

A ``pip``-installed user cannot write into ``site-packages``, so resolving a platform has to search a
*path*: the build's own root, then read-only fallbacks (an env override, the per-user library, the
packaged reference).  These pin the precedence and the read-vs-create routing, toolchain-free.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from waveflow.calib import platform as plat_mod
from waveflow.calib.platform import (
    PLATFORM_PATH_ENV,
    Platform,
    PlatformMismatchError,
    packaged_platforms_dir,
    platform_fallback_path,
    user_platforms_dir,
)


def _seed(root: Path, name: str, part="xc7z020clg484-1", freq=100e6) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "platform.json").write_text(
        json.dumps({"part": part, "clk_freq_hz": freq}), encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# The shipped reference is findable in-package
# ---------------------------------------------------------------------------

def test_packaged_dir_contains_the_reference_platform():
    pkg = packaged_platforms_dir()
    assert pkg is not None, "packaged platforms dir did not resolve"
    assert (pkg / "zynq7020_bfm_100mhz" / "platform.json").is_file()


def test_user_platforms_dir_is_under_waveflow():
    p = user_platforms_dir()
    assert p.name == "platforms"
    assert "waveflow" in str(p).lower()


# ---------------------------------------------------------------------------
# Fallback path assembly & ordering
# ---------------------------------------------------------------------------

def test_fallback_path_order_env_then_user_then_packaged(monkeypatch, tmp_path):
    import os
    envA, envB = tmp_path / "a", tmp_path / "b"
    user, pkg = tmp_path / "user", tmp_path / "pkg"
    monkeypatch.setenv(PLATFORM_PATH_ENV, os.pathsep.join([str(envA), str(envB)]))
    monkeypatch.setattr(plat_mod, "user_platforms_dir", lambda: user)
    monkeypatch.setattr(plat_mod, "packaged_platforms_dir", lambda: pkg)

    assert platform_fallback_path() == [envA, envB, user, pkg]


def test_fallback_path_dedups_preserving_order(monkeypatch, tmp_path):
    import os
    user = tmp_path / "user"
    monkeypatch.setenv(PLATFORM_PATH_ENV, os.pathsep.join([str(user), str(user)]))
    monkeypatch.setattr(plat_mod, "user_platforms_dir", lambda: user)
    monkeypatch.setattr(plat_mod, "packaged_platforms_dir", lambda: None)
    assert platform_fallback_path() == [user]


def test_fallback_path_drops_packaged_when_absent(monkeypatch, tmp_path):
    user = tmp_path / "user"
    monkeypatch.delenv(PLATFORM_PATH_ENV, raising=False)
    monkeypatch.setattr(plat_mod, "user_platforms_dir", lambda: user)
    monkeypatch.setattr(plat_mod, "packaged_platforms_dir", lambda: None)
    assert platform_fallback_path() == [user]


# ---------------------------------------------------------------------------
# Resolve: single-root behaviour is unchanged (backward compat)
# ---------------------------------------------------------------------------

def test_resolve_single_root_creates_when_absent(tmp_path):
    p = Platform.resolve(tmp_path, "plat", part="xc7z020clg484-1", clk_freq=100e6)
    assert p.dir == tmp_path / "plat"
    assert (p.dir / "platform.json").is_file()
    assert p.part == "xc7z020clg484-1"


def test_resolve_single_root_confirms_when_present(tmp_path):
    _seed(tmp_path, "plat")
    p = Platform.resolve(tmp_path, "plat", part="xc7z020clg484-1", clk_freq=100e6)
    assert p.dir == tmp_path / "plat"
    assert p.clk_freq == 100e6


# ---------------------------------------------------------------------------
# Resolve: fallbacks
# ---------------------------------------------------------------------------

def test_resolve_reads_from_fallback_when_absent_in_primary(tmp_path):
    primary = tmp_path / "primary"
    fb = tmp_path / "fallback"
    _seed(fb, "plat")
    p = Platform.resolve(primary, "plat", fallbacks=[fb])
    assert p.dir == fb / "plat"                      # read from the fallback
    assert not (primary / "plat").exists()           # nothing created in the primary


def test_resolve_primary_shadows_fallback(tmp_path):
    primary = tmp_path / "primary"
    fb = tmp_path / "fallback"
    _seed(primary, "plat", freq=100e6)
    _seed(fb, "plat", freq=200e6)
    p = Platform.resolve(primary, "plat", fallbacks=[fb])
    assert p.dir == primary / "plat"                 # primary wins
    assert p.clk_freq == 100e6


def test_resolve_creates_in_primary_never_a_fallback(tmp_path):
    primary = tmp_path / "primary"
    fb = tmp_path / "fallback"
    fb.mkdir()
    p = Platform.resolve(primary, "plat", part="xc7z020clg484-1", clk_freq=100e6, fallbacks=[fb])
    assert p.dir == primary / "plat"                 # created in the primary
    assert not (fb / "plat").exists()

def test_resolve_mismatch_from_fallback_raises(tmp_path):
    primary = tmp_path / "primary"
    fb = tmp_path / "fallback"
    _seed(fb, "plat", part="xc7z020clg484-1", freq=100e6)
    with pytest.raises(PlatformMismatchError):
        Platform.resolve(primary, "plat", part="xczu7ev-ffvc1156-2-e", clk_freq=100e6, fallbacks=[fb])
