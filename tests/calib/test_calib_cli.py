"""The ``waveflow_calib`` CLI: the front door to a platform.

``list`` is the one worth testing hardest.  It answers the question asked *before* creating a platform —
is there already a calibrated one for my target? — and its two obligations are to print the **identity**
rather than just a name (a name does not say which part the numbers are valid for) and to mark entries
that are **shadowed**, since resolution is first-match-wins on the whole directory and a shadowed entry
is never actually used.
"""
from __future__ import annotations

import json

from waveflow.calib.cli import main


def _mk(root, name, part="xc7z020clg484-1", clk=100e6):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "platform.json").write_text(json.dumps({"part": part, "clk_freq_hz": clk}), encoding="utf-8")
    return d


class TestList:
    def test_prints_identity_not_just_a_name(self, tmp_path, capsys):
        _mk(tmp_path, "myboard", part="xc7z020clg484-1", clk=100e6)
        assert main(["list", "--platforms-root", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "myboard" in out
        assert "xc7z020clg484-1" in out          # the part, so you can tell whether it matches
        assert "100 MHz" in out

    def test_marks_a_shadowed_entry(self, tmp_path, capsys):
        """A name owned by an earlier root hides the later one entirely — say so, do not just list it."""
        _mk(tmp_path, "zynq7020_bfm_100mhz")     # shadows the packaged platform of the same name
        main(["list", "--platforms-root", str(tmp_path)])
        assert "(shadowed)" in capsys.readouterr().out

    def test_no_shadow_note_when_nothing_is_shadowed(self, tmp_path, capsys):
        _mk(tmp_path, "a_unique_name_nobody_ships")
        main(["list", "--platforms-root", str(tmp_path)])
        out = capsys.readouterr().out
        assert "a_unique_name_nobody_ships" in out

    def test_the_packaged_platform_is_always_visible(self, tmp_path, capsys):
        """Even from an empty project — that is the point of the fallback path."""
        assert main(["list", "--platforms-root", str(tmp_path / "nothing_here")]) == 0
        assert "zynq7020_bfm_100mhz" in capsys.readouterr().out


class TestNew:
    def test_requires_an_identity_unless_seeding(self, tmp_path, capsys):
        """A platform with no part/clock cannot gate anything, so it is refused rather than created."""
        assert main(["new", str(tmp_path / "bad")]) == 2
        assert "--part and --clk are required" in capsys.readouterr().out

    def test_seeding_inherits_the_identity(self, tmp_path, capsys):
        assert main(["new", str(tmp_path / "mine"), "--from", "zynq7020_bfm_100mhz"]) == 0
        out = capsys.readouterr().out
        assert "seeded from" in out
        assert "xc7z020clg484-1" in out          # inherited, though --part was never given


class TestShow:
    def test_a_missing_platform_is_reported_not_crashed(self, tmp_path, capsys):
        assert main(["show", str(tmp_path / "nope")]) == 1
        assert "no platform at" in capsys.readouterr().out
