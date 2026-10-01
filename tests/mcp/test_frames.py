"""The frames: the process text, and the fact that it ships.

Two failure modes are worth a test here.

**The frame directory is text, so package discovery misses it.** A frame has
no ``.py`` file at all. Without an explicit ``package-data`` glob a
pip-installed Waveflow has no ``process.md``, and ``waveflow_get_process``
fails on the first call an agent makes. ``test_pyproject_ships_the_frames``
pins the glob.

**``process.md`` is one source with two consumers** -- the tool and the
``AGENTS.md`` the scaffold writes. The content tests below are not
proofreading: each one names a rule that, if it fell out of the text, would
let an agent do the specific wrong thing the plan is built to prevent.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from waveflow.mcp.frames import (
    DEFAULT_FRAME,
    FRAMES_DIR,
    list_frames,
    waveflow_get_process,
    waveflow_list_frames,
)

REPO = Path(__file__).resolve().parents[2]


def test_the_default_frame_exists() -> None:
    assert DEFAULT_FRAME in list_frames()


def test_every_frame_has_its_four_parts() -> None:
    for name, frame in list_frames().items():
        assert (frame.directory / "frame.md").is_file(), f"{name}: no frame.md"
        assert (frame.directory / "process.md").is_file(), f"{name}: no process.md"
        assert (frame.directory / "frame.toml").is_file(), f"{name}: no frame.toml"
        assert frame.synopsis, f"{name}: no synopsis"
        assert frame.reference_examples, f"{name}: names no reference example"


def test_reference_examples_are_real_toc_examples() -> None:
    """A frame points at an example an agent is allowed to copy.

    Naming an example outside the docs TOC would tell the agent to model its
    design on something `waveflow_get_example` will then refuse to return.
    """
    from waveflow.mcp.knowledge import get_index

    cards = get_index().cards
    for name, frame in list_frames().items():
        for example in frame.reference_examples:
            assert example in cards, (
                f"frame {name} names reference example {example!r}, which is "
                f"not in the docs TOC"
            )


def test_list_frames_tool() -> None:
    result = waveflow_list_frames()
    names = {f["name"] for f in result["frames"]}
    assert DEFAULT_FRAME in names
    entry = next(f for f in result["frames"] if f["name"] == DEFAULT_FRAME)
    assert entry["prompts"], "the frame ships no example function specs"
    for prompt in entry["prompts"]:
        assert prompt["synopsis"]


def test_get_process_defaults_and_returns_both_documents() -> None:
    default = waveflow_get_process()
    assert default["frame"] == DEFAULT_FRAME
    explicit = waveflow_get_process(DEFAULT_FRAME)
    assert default == explicit
    assert waveflow_get_process(None) == explicit

    assert default["process"].strip()
    assert default["specification"].strip()


def test_get_process_miss_lists_the_frames() -> None:
    result = waveflow_get_process("freerun_bfm")
    assert "error" in result and DEFAULT_FRAME in result["known"]


# ---------------------------------------------------------------------------
# What the process text must still say
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def process() -> str:
    return waveflow_get_process()["process"]


def test_process_names_the_tools_it_tells_the_agent_to_call(process: str) -> None:
    """Every tool the process names must actually be registered.

    The process is the one document an agent is guaranteed to read, so a tool
    renamed out from under it sends the agent looking for something that is
    not there.
    """
    from waveflow.mcp.registry import REGISTRY

    registered = {s["function"]["name"] for s in REGISTRY.tool_schemas()}
    named = {
        word.strip("`(),.")
        for word in process.replace("(", " (").split()
        if word.strip("`(),.").startswith("waveflow_")
    }
    assert named, "the process names no tools at all"
    unknown = named - registered
    assert not unknown, f"the process points at tools that do not exist: {sorted(unknown)}"


@pytest.mark.parametrize(
    "rule,needle",
    [
        ("stop after Stage 1", "Stop here"),
        ("the Stage 1 artifacts are frozen", "Stage 1 artifacts are now frozen"),
        ("never edit generated files", "Never edit a generated file"),
        ("never hand-pack words", "Never hand-pack words"),
        ("never write your own PASS column", "Never write your own PASS column"),
        ("every comparison must pass", "every comparison"),
        ("scenarios are pre-loaded", "pre-loaded"),
        ("the reference example", "stream_inband"),
    ],
)
def test_process_still_states_the_rule(process: str, rule: str, needle: str) -> None:
    assert needle in process, f"the process no longer states: {rule}"


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def test_pyproject_ships_the_frames() -> None:
    """A frame is markdown and TOML, so it needs an explicit package-data glob."""
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = config["tool"]["setuptools"]["package-data"]
    globs = package_data.get("waveflow.mcp.frames")
    assert globs, "waveflow.mcp.frames is not in [tool.setuptools.package-data]"

    shipped = {suffix for glob in globs for suffix in [Path(glob).suffix]}
    #: Python source is shipped by package discovery, and `__pycache__` is not
    #: shipped at all -- neither is this test's business.
    ignored = {".py", ".pyc", ".pyo"}
    on_disk = {
        p.suffix
        for p in FRAMES_DIR.rglob("*")
        if p.is_file() and p.suffix and p.suffix not in ignored
    }
    assert on_disk <= shipped, (
        f"frame files with these extensions would not ship: {sorted(on_disk - shipped)}"
    )
