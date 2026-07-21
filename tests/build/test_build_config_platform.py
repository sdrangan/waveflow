"""tests/build/test_build_config_platform.py — BuildConfig's platform resolution.

A build selects a calibration platform by name; BuildConfig resolves it at construction into
`platform_info` — creating (and seeding part/clock) an absent platform, or confirming an existing one
against the build's part/clock.  This is the single place both synthesis (set_part/create_clock) and
the calibration steps read the platform from, so the two can't drift.
"""
from __future__ import annotations

import json

import pytest

from waveflow.build.build import BuildConfig
from waveflow.calib.platform import PLATFORM_MANIFEST, PlatformMismatchError


class TestPlatformResolution:
    def test_no_platform_is_none(self, tmp_path):
        cfg = BuildConfig(root_dir=tmp_path)
        assert cfg.platform_info is None

    def test_absent_platform_created_and_seeded(self, tmp_path):
        cfg = BuildConfig(root_dir=tmp_path, platform="zynq7020_bfm_100mhz",
                          part="xc7z020clg484-1", clk_freq=100e6,
                          platforms_root=tmp_path / "platforms")
        assert cfg.platform_info is not None
        assert cfg.platform_info.dir == tmp_path / "platforms" / "zynq7020_bfm_100mhz"
        data = json.loads((cfg.platform_info.dir / PLATFORM_MANIFEST).read_text())
        assert data == {"part": "xc7z020clg484-1", "clk_freq_hz": 100e6}

    def test_matching_platform_confirms(self, tmp_path):
        root = tmp_path / "platforms"
        BuildConfig(root_dir=tmp_path, platform="plat", part="xc7z020clg484-1", clk_freq=100e6,
                    platforms_root=root)
        # a second build with the same part/clock resolves cleanly.
        cfg = BuildConfig(root_dir=tmp_path, platform="plat", part="xc7z020clg484-1",
                          clk_freq=100e6, platforms_root=root)
        assert cfg.platform_info.part == "xc7z020clg484-1"

    def test_mismatch_raises_by_default(self, tmp_path):
        root = tmp_path / "platforms"
        BuildConfig(root_dir=tmp_path, platform="plat", part="xc7z020clg484-1", clk_freq=100e6,
                    platforms_root=root)
        with pytest.raises(PlatformMismatchError):
            BuildConfig(root_dir=tmp_path, platform="plat", part="xczu28dr-ffvg1517-2-e",
                        clk_freq=100e6, platforms_root=root)

    def test_allow_platform_mismatch_downgrades_to_warning(self, tmp_path):
        root = tmp_path / "platforms"
        BuildConfig(root_dir=tmp_path, platform="plat", part="xc7z020clg484-1", clk_freq=100e6,
                    platforms_root=root)
        with pytest.warns(match="different target"):
            cfg = BuildConfig(root_dir=tmp_path, platform="plat", part="other", clk_freq=100e6,
                              platforms_root=root, allow_platform_mismatch=True)
        assert cfg.platform_info.part == "xc7z020clg484-1"

    def test_platforms_root_defaults_to_calib_platforms(self, tmp_path):
        # no platform selected -> platforms_root still normalised to the tracked-library default.
        from pathlib import Path
        cfg = BuildConfig(root_dir=tmp_path)
        assert cfg.platforms_root == Path("calib/platforms")
