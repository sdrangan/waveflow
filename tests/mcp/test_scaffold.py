"""``waveflow new-accel``: the project it writes, and that it runs unmodified.

The gate that matters is :func:`test_scaffolded_project_runs_pysim`, and it is
slow on purpose. Everything else here is structure -- the right files, the
right names, the compute removed -- and structure can all be right while the
project does not run. A scaffold that does not run is worse than none: the
first thing it teaches an agent is that the reference flow is broken.

The rename is the risky part. One token, ``poly``, is spread across schema
class names, a C++ namespace, the kernel name, four filenames and the build
DAG's artifact ids, and a miss anywhere leaves a project that imports but
fails somewhere further in. ``test_no_trace_of_the_source_example_name``
checks the whole tree at once.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from waveflow.mcp.frames import load_frame
from waveflow.mcp.scaffold import (
    ScaffoldError,
    new_accel,
    waveflow_new_accel_project,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("scaffold") / "gain_clip"
    new_accel("gain_clip", dest=out)
    return out


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_writes_the_files_the_frame_promises(project: Path) -> None:
    for name in (
        "gain_clip.py",
        "gain_clip_build.py",
        "gain_clip_evaluate_impl.tpp",
        "AGENTS.md",
        "frame.md",
        "CLAUDE.md",
        "GEMINI.md",
        "spec/oracle.py",
        "spec/scenarios.py",
        "spec/check.py",
        "spec/layout.md",
    ):
        assert (project / name).is_file(), f"missing {name}"


def test_agents_md_is_the_process_verbatim(project: Path) -> None:
    """One text, two consumers.  If these drift, the project and the tool disagree."""
    frame = load_frame("stream_inband")
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == frame.process
    assert (project / "frame.md").read_text(encoding="utf-8") == frame.specification


def test_claude_and_gemini_point_at_agents_md(project: Path) -> None:
    for name in ("CLAUDE.md", "GEMINI.md"):
        text = (project / name).read_text(encoding="utf-8")
        assert "AGENTS.md" in text and "frame.md" in text
        assert len(text.splitlines()) < 15, f"{name} should be a pointer, not a copy"


def test_the_example_results_do_not_come_along(project: Path) -> None:
    """A fresh project must not ship the reference design's worked numbers."""
    for unwanted in ("gen", "include", "results", "logs", "vcd", "data"):
        assert not (project / unwanted).exists(), (
            f"{unwanted}/ was copied from the example; a fresh project would "
            f"then be comparing itself against the example's run"
        )


# ---------------------------------------------------------------------------
# The rename
# ---------------------------------------------------------------------------


def test_the_schemas_and_module_are_renamed(project: Path) -> None:
    source = (project / "gain_clip.py").read_text(encoding="utf-8")
    for expected in (
        "class GainClipAccel(HostActivated)",
        "class GainClipCmdHdr(DataList)",
        "class GainClipRespHdr(DataList)",
        "class GainClipError(IntEnum)",
    ):
        assert expected in source, f"not renamed: {expected}"
    assert 'cpp_kernel_name: ClassVar[str | None] = "gain_clip"' in source


#: The frame's own documents are **not** renamed, and must not be: they tell
#: the agent to go and read `examples/stream_inband`, naming `poly.py`,
#: `PolyAccel` and the rest.  Renaming those references would point it at
#: files that do not exist.
_FRAME_DOCS = {"AGENTS.md", "frame.md"}


def test_no_trace_of_the_source_example_name(project: Path) -> None:
    """``poly`` must survive nowhere in the project's own code.

    Two exclusions, both deliberate: the frame documents, which cite the
    reference example on purpose (see `_FRAME_DOCS`), and the English word
    ``polynomial``, which the token-boundary rule spares so that prose about
    the reference design does not turn into ``gain_clipnomial``.
    """
    token = re.compile(r"(?<![A-Za-z0-9])[Pp]oly(?![a-z0-9])")
    offenders: list[str] = []
    for path in project.rglob("*"):
        if not path.is_file() or path.suffix in (".pyc",):
            continue
        rel = path.relative_to(project).as_posix()
        if rel in _FRAME_DOCS:
            continue
        if token.search(rel):
            offenders.append(f"{rel} (filename)")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), start=1):
            if token.search(line):
                offenders.append(f"{rel}:{n}: {line.strip()[:90]}")
    assert not offenders, "the rename missed:\n  " + "\n  ".join(offenders[:20])


def test_the_frame_documents_still_cite_the_real_example(project: Path) -> None:
    """The other side of the exclusion above.

    `AGENTS.md` tells the agent to read `poly.py` out of `stream_inband`. If
    the rename ever reached these files it would send it after `gain_clip.py`
    inside the example directory, which does not exist -- a wrong pointer is
    worse than none, because the agent will invent what it cannot find.
    """
    agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert 'waveflow_get_example("stream_inband", file="poly.py")' in agents

    frame_md = (project / "frame.md").read_text(encoding="utf-8")
    assert "PolyAccel(HostActivated)" in frame_md
    assert "examples/stream_inband" in frame_md


def test_prose_about_the_reference_design_is_left_alone(project: Path) -> None:
    """The counterpart to the test above: ``polynomial`` is not a token.

    Renaming it would produce ``gain_clipnomial``, which is how a naive
    substring replace announces itself.
    """
    blob = "\n".join(
        p.read_text(encoding="utf-8")
        for p in project.rglob("*.py")
        if p.is_file()
    )
    assert "nomial" not in blob.replace("polynomial", "").replace("Polynomial", "")


# ---------------------------------------------------------------------------
# The stub
# ---------------------------------------------------------------------------


def test_the_compute_is_removed_from_both_sides(project: Path) -> None:
    python = (project / "gain_clip.py").read_text(encoding="utf-8")
    cpp = (project / "gain_clip_evaluate_impl.tpp").read_text(encoding="utf-8")

    assert "TODO" in python and "TODO" in cpp
    # The Horner evaluation, on both sides.
    assert "power *= samp_in" not in python
    assert "eval_gain_clip_horner" not in cpp and "eval_poly_horner" not in cpp


def test_the_framing_is_not_removed(project: Path) -> None:
    """Only the function goes.  The frame's protocol has to still be there.

    This is the half of the scaffold that is easy to get wrong in the other
    direction: cut too much and the agent re-derives the TLAST rules, which
    is exactly what the reference example exists to prevent.
    """
    python = (project / "gain_clip.py").read_text(encoding="utf-8")
    cpp = (project / "gain_clip_evaluate_impl.tpp").read_text(encoding="utf-8")

    assert "WRONG_NSAMP" in python
    assert "TLAST_EARLY_SAMP_IN" in python
    assert "VitisRegMap" in python
    assert "tlast_status" in cpp
    assert "read_axi4_stream_lane" in cpp


def test_a_stub_that_stops_matching_is_loud(project: Path, monkeypatch, tmp_path) -> None:
    """A silently-unapplied stub ships the reference function.  Refuse instead."""
    import waveflow.mcp.scaffold as scaffold

    frame = load_frame("stream_inband")
    broken = dict(frame.meta)
    broken["template"] = dict(frame.meta["template"])
    broken["template"]["stub"] = [
        {
            "file": "poly.py",
            "description": "a region that no longer exists",
            "start": "this line is not in the example",
            "end": "nor is this one",
            "replacement": "pass",
        }
    ]
    monkeypatch.setattr(
        scaffold,
        "load_frame",
        lambda name: type(frame)(name=frame.name, directory=frame.directory, meta=broken),
    )
    with pytest.raises(ScaffoldError, match="no longer match"):
        new_accel("broken_accel", dest=tmp_path / "broken")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["GainClip", "gain-clip", "1gain", "", "gain clip"])
def test_a_bad_project_name_is_refused(name: str, tmp_path) -> None:
    with pytest.raises(ScaffoldError):
        new_accel(name, dest=tmp_path / "x")


def test_a_nonempty_directory_is_not_clobbered(tmp_path) -> None:
    dest = tmp_path / "taken"
    dest.mkdir()
    (dest / "mine.txt").write_text("keep me")
    with pytest.raises(ScaffoldError, match="not empty"):
        new_accel("gain_clip", dest=dest)
    assert (dest / "mine.txt").read_text() == "keep me"


def test_the_tool_returns_errors_as_data(tmp_path) -> None:
    result = waveflow_new_accel_project("NotValid", frame=None, directory=str(tmp_path))
    assert "error" in result

    result = waveflow_new_accel_project(
        "ok_accel", frame="freerun_bfm", directory=str(tmp_path / "x")
    )
    assert "error" in result and "freerun_bfm" in result["error"]


def test_the_tool_names_what_to_read_first(tmp_path) -> None:
    result = waveflow_new_accel_project(
        "demo_accel", frame=None, directory=str(tmp_path / "demo")
    )
    assert result["read_first"] == ["AGENTS.md", "frame.md"]
    assert "--through py_sim" in result["next"]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_scaffolded_project_runs_pysim(project: Path) -> None:
    """The whole point: it runs before anything is edited.

    ``py_sim`` is the last step reachable without Vitis. The toolchain half of
    the gate -- ``--through validate_csim`` -- is
    ``test_scaffolded_project_runs_csim`` below, marked ``vitis``.
    """
    completed = subprocess.run(
        [sys.executable, "gain_clip_build.py", "--through", "py_sim"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert completed.returncode == 0, (
        "a freshly scaffolded project does not run:\n"
        + completed.stdout[-4000:]
        + completed.stderr[-4000:]
    )
    assert "PASSED" in completed.stdout
    assert (project / "results").is_dir()


@pytest.mark.vitis
def test_scaffolded_project_runs_csim(project: Path) -> None:
    """The frame's own gate: the template passes ``validate_csim`` unmodified.

    Parametrizing this over the frames is what makes a second frame cost
    nothing to gate; with one frame it is written out.
    """
    completed = subprocess.run(
        [sys.executable, "gain_clip_build.py", "--through", "validate_csim"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    assert completed.returncode == 0, (
        "the scaffolded project does not reach validate_csim:\n"
        + completed.stdout[-6000:]
        + completed.stderr[-6000:]
    )
