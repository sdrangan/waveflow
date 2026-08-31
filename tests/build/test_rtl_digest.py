"""tests/build/test_rtl_digest.py -- the source stamp, and the staleness question it answers.

The guard's job is to decide "was this RTL built from *this* source".  It used to answer with
mtimes, which is a proxy, and the proxy said "stale" for a ``--force`` regeneration that emitted
**identical bytes** -- so proving the artifacts were byte-identical was what silently skipped every
XSI gate that would have proved the design still behaves.

These tests pin the three things that must all hold at once:

* identical content is **not** stale, whatever the mtimes say;
* changed content **is** stale, and says so in the sentence a human can act on;
* a build tree with **no stamp** falls back to the mtime check, never to "clean".
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from waveflow.build.rtl_digest import (
    STAMP_NAME,
    first_mismatch,
    read_stamp,
    source_digests,
    source_files,
    stamp_path,
    write_stamp,
)
from waveflow.build.trace_steps import rtl_staleness

REPO = Path(__file__).resolve().parents[2]
TOP = "widget"

#: The sentence that does the work.  It is asserted in several tests on purpose: rewording it is a
#: change to what the guard *tells a human to do*, and should not slip through as a typo fix.
REFUSAL_TAIL = "Do NOT re-record a cycle count against RTL you did not produce."


def _tree(root: Path, *, cpp: str = "int main(){}\n", hdr: str = "#pragma once\n") -> Path:
    """A minimal example directory: one generated .cpp, one include header, one synthesized .v."""
    (root / "gen").mkdir(parents=True, exist_ok=True)
    (root / "include").mkdir(parents=True, exist_ok=True)
    verilog = root / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
    verilog.mkdir(parents=True, exist_ok=True)
    (root / "gen" / f"{TOP}.cpp").write_text(cpp, encoding="utf-8")
    (root / "include" / "helper.h").write_text(hdr, encoding="utf-8")
    (verilog / f"{TOP}.v").write_text("module widget; endmodule\n", encoding="utf-8")
    return root


def _touch_newer(path: Path, ref: Path, *, seconds: float = 600.0) -> None:
    """Give *path* an mtime well after *ref*'s -- what a ``--force`` regeneration does."""
    t = ref.stat().st_mtime + seconds
    os.utime(path, (t, t))


class TestSourceSet:
    def test_is_the_generated_cpp_plus_the_include_sources(self, tmp_path):
        rels = {p.relative_to(tmp_path).as_posix() for p in source_files(_tree(tmp_path), TOP)}
        assert rels == {f"gen/{TOP}.cpp", "include/helper.h"}

    def test_ignores_non_source_files_under_include(self, tmp_path):
        root = _tree(tmp_path)
        (root / "include" / "notes.md").write_text("not a source\n", encoding="utf-8")
        rels = {p.relative_to(root).as_posix() for p in source_files(root, TOP)}
        assert "include/notes.md" not in rels

    def test_is_sorted_so_a_mismatch_report_is_deterministic(self, tmp_path):
        root = _tree(tmp_path)
        for name in ("z.h", "a.h", "m.hpp"):
            (root / "include" / name).write_text("x\n", encoding="utf-8")
        files = source_files(root, TOP)
        assert files == sorted(files)


class TestStampRoundTrip:
    def test_written_stamp_reads_back_as_the_current_digests(self, tmp_path):
        root = _tree(tmp_path)
        assert write_stamp(root, TOP) == stamp_path(root, TOP)
        assert read_stamp(root, TOP) == source_digests(root, TOP)

    def test_no_project_directory_means_nothing_to_vouch_for(self, tmp_path):
        (tmp_path / "gen").mkdir()
        assert write_stamp(tmp_path, TOP) is None

    def test_lives_inside_the_project_so_deleting_it_takes_the_stamp(self, tmp_path):
        root = _tree(tmp_path)
        write_stamp(root, TOP)
        assert stamp_path(root, TOP) == root / f"{TOP}_proj" / STAMP_NAME

    @pytest.mark.parametrize("payload", [
        "not json at all",
        json.dumps({"version": 99, "files": {}}),
        json.dumps({"version": 1}),
        json.dumps({"version": 1, "files": ["a", "b"]}),
        json.dumps(["version", 1]),
    ])
    def test_anything_untrustworthy_reads_as_no_stamp(self, tmp_path, payload):
        """Every unhappy shape collapses to ``None`` -- the caller then falls back to mtimes.

        Returning ``{}`` instead would read as "no sources, therefore nothing changed", which is a
        corrupt file quietly certifying the tree as clean.
        """
        root = _tree(tmp_path)
        stamp_path(root, TOP).write_text(payload, encoding="utf-8")
        assert read_stamp(root, TOP) is None


class TestFirstMismatch:
    def test_equal_maps_agree(self):
        assert first_mismatch({"a": "1"}, {"a": "1"}) is None

    def test_names_the_changed_file_and_what_happened(self):
        assert first_mismatch({"a": "1"}, {"a": "2"}) == ("a", "has changed")
        assert first_mismatch({}, {"a": "1"}) == ("a", "is new")
        assert first_mismatch({"a": "1"}, {}) == ("a", "is gone")

    def test_reports_the_first_in_sorted_order(self):
        assert first_mismatch({"a": "1", "b": "1"}, {"a": "9", "b": "9"})[0] == "a"


class TestStalenessWithAStamp:
    def test_a_force_regeneration_of_identical_bytes_is_not_stale(self, tmp_path):
        """**The bug this whole change exists for.**

        ``--force`` rewrites ``gen/<top>.cpp`` with the same bytes and a new mtime.  git says clean,
        the mtime check said stale, and every XSI gate skipped while pytest printed a green line.
        """
        root = _tree(tmp_path)
        write_stamp(root, TOP)
        gen = root / "gen" / f"{TOP}.cpp"
        rtl = root / f"{TOP}_proj" / "solution1" / "syn" / "verilog" / f"{TOP}.v"
        gen.write_text(gen.read_text(encoding="utf-8"), encoding="utf-8")   # identical bytes
        _touch_newer(gen, rtl)
        assert rtl_staleness(root, TOP) is None

    def test_an_edited_source_is_still_stale(self, tmp_path):
        """The gate that matters most: sharpening the guard must not blunt it."""
        root = _tree(tmp_path)
        write_stamp(root, TOP)
        (root / "gen" / f"{TOP}.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        why = rtl_staleness(root, TOP)
        assert why is not None
        assert f"gen/{TOP}.cpp has changed" in why
        assert REFUSAL_TAIL in why

    def test_an_edited_header_is_stale_even_with_an_older_mtime(self, tmp_path):
        """Content, not time: a header restored from another branch can be *older* and still wrong."""
        root = _tree(tmp_path)
        write_stamp(root, TOP)
        hdr = root / "include" / "helper.h"
        hdr.write_text("#pragma once\n// from another branch\n", encoding="utf-8")
        os.utime(hdr, (1, 1))
        why = rtl_staleness(root, TOP)
        assert why is not None and "include/helper.h has changed" in why

    def test_a_new_source_file_is_stale(self, tmp_path):
        root = _tree(tmp_path)
        write_stamp(root, TOP)
        (root / "include" / "extra.h").write_text("#pragma once\n", encoding="utf-8")
        why = rtl_staleness(root, TOP)
        assert why is not None and "include/extra.h is new" in why

    def test_a_deleted_source_file_is_stale(self, tmp_path):
        root = _tree(tmp_path)
        write_stamp(root, TOP)
        (root / "include" / "helper.h").unlink()
        why = rtl_staleness(root, TOP)
        assert why is not None and "include/helper.h is gone" in why


class TestStalenessWithoutAStamp:
    def test_falls_back_to_mtimes_not_to_clean(self, tmp_path):
        """A build tree made before the stamp existed must not silently become unguarded.

        This is the direction that would turn a fix for silent skips into a new silent pass.
        """
        root = _tree(tmp_path)
        gen = root / "gen" / f"{TOP}.cpp"
        rtl = root / f"{TOP}_proj" / "solution1" / "syn" / "verilog" / f"{TOP}.v"
        _touch_newer(gen, rtl)
        why = rtl_staleness(root, TOP)
        assert why is not None
        assert "no source stamp" in why
        assert REFUSAL_TAIL in why

    def test_older_sources_still_pass_under_the_fallback(self, tmp_path):
        root = _tree(tmp_path)
        rtl = root / f"{TOP}_proj" / "solution1" / "syn" / "verilog" / f"{TOP}.v"
        _touch_newer(rtl, root / "gen" / f"{TOP}.cpp")
        assert rtl_staleness(root, TOP) is None


class TestAbsentRtlIsNotStaleness:
    def test_no_project_directory(self, tmp_path):
        assert rtl_staleness(tmp_path, TOP) is None

    def test_project_with_no_verilog(self, tmp_path):
        (tmp_path / f"{TOP}_proj" / "solution1" / "syn" / "verilog").mkdir(parents=True)
        assert rtl_staleness(tmp_path, TOP) is None


def _render_rtl_f_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == "render_rtl_f":
                out.append(node)
    return out


def test_no_gate_re_stamps_the_sources_it_is_about_to_check():
    """``render_rtl_f`` stamps by default, because eleven builds call it right after csynth.

    Every XSI gate ALSO calls it -- to re-render the ``.f`` from the RTL on disk, having run no
    csynth at all.  A stamp written there would record today's sources against yesterday's RTL: the
    guard signing its own dismissal, silently, which is the exact failure class this file exists to
    close.  So the default is the one the builds want, and this asserts no test can drift off it.
    """
    offenders = []
    for path in sorted((REPO / "tests").rglob("*.py")):
        for call in _render_rtl_f_calls(path):
            ok = any(kw.arg == "stamp_sources" and isinstance(kw.value, ast.Constant)
                     and kw.value.value is False for kw in call.keywords)
            if not ok:
                offenders.append(f"{path.relative_to(REPO).as_posix()}:{call.lineno}")
    assert not offenders, (
        "these render_rtl_f() calls under tests/ would stamp the source digest, disabling the "
        "staleness guard for whatever they touch; pass stamp_sources=False:\n  "
        + "\n  ".join(offenders))
