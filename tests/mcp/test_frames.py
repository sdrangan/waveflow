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
    COMMON_DIR,
    FRAMES_DIR,
    generic_process,
    list_frames,
    load_frame,
    scaffold_frames,
    waveflow_get_process,
    waveflow_list_frames,
)

REPO = Path(__file__).resolve().parents[2]

#: The lab's frame.  Its prompts name it, so it must stay, under this name.
LAB_FRAME = "stream_inband"

#: The axes every frame declares in ``frame.toml`` (plan D2).
AXES = ("pattern", "shape", "flow", "choose_when")


def test_the_lab_frame_exists_and_scaffolds() -> None:
    assert LAB_FRAME in list_frames()
    assert LAB_FRAME in scaffold_frames()


def test_underscore_directories_are_not_frames() -> None:
    assert COMMON_DIR.is_dir()
    assert not any(name.startswith("_") for name in list_frames())
    assert all(not f.directory.name.startswith("_") for f in list_frames().values())


def test_every_frame_has_its_four_parts() -> None:
    for name, frame in list_frames().items():
        assert (frame.directory / "frame.md").is_file(), f"{name}: no frame.md"
        assert (frame.directory / "process.md").is_file(), f"{name}: no process.md"
        assert (frame.directory / "frame.toml").is_file(), f"{name}: no frame.toml"
        assert frame.synopsis, f"{name}: no synopsis"
        assert frame.reference_examples, f"{name}: names no reference example"


def test_every_frame_declares_its_axes() -> None:
    """The menu is only a menu if every entry says what it is for."""
    for name, frame in list_frames().items():
        entry = frame.to_dict()
        for axis in AXES:
            assert entry[axis], f"{name}: frame.toml has no {axis}"
        assert (REPO / frame.pattern_doc).is_file(), (
            f"{name}: pattern {frame.pattern!r} has no page {frame.pattern_doc}"
        )


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
    assert names == set(list_frames())
    entry = next(f for f in result["frames"] if f["name"] == LAB_FRAME)
    assert entry["has_scaffold"] is True
    assert entry["prompts"], "the frame ships no example function specs"
    for prompt in entry["prompts"]:
        assert prompt["synopsis"]
    assert "waveflow_get_process" in result["hint"]


def test_get_process_without_a_frame_is_the_generic_process() -> None:
    """No default frame: with none named, the agent gets the menu, not stream_inband."""
    generic = waveflow_get_process()
    assert generic == waveflow_get_process(None)
    assert generic["frame"] is None
    assert {f["name"] for f in generic["frames"]} == set(list_frames())
    text = generic["process"]
    assert text == generic_process()
    assert "Choose the architecture first" in text
    # Every frame is on the menu, and the fallback is the example cards.
    for name in list_frames():
        assert f"`{name}`" in text
    assert "waveflow_list_examples()" in text
    assert "Your reference design is" not in text
    assert "<!--" not in text, "a slot was left unfilled"


def test_get_process_with_a_frame_returns_both_documents() -> None:
    explicit = waveflow_get_process(LAB_FRAME)
    assert explicit["frame"] == LAB_FRAME
    assert explicit["process"].strip()
    assert explicit["specification"].strip()
    assert "<!--" not in explicit["process"], "a slot was left unfilled"


def test_get_process_miss_lists_the_frames() -> None:
    result = waveflow_get_process("freerun_bfm")
    assert "error" in result and LAB_FRAME in result["known"]


def test_the_shared_process_is_in_every_frame() -> None:
    """Plan D1: one shared text, composed at load time, so it cannot drift."""
    shared_rules = ("Never hand-pack words", "Never edit a generated file",
                    "Before you start: learn the machinery")
    for name, frame in list_frames().items():
        for needle in shared_rules:
            assert needle in frame.process, f"{name}: lost {needle!r}"
        assert frame.own_process.strip() in frame.process
        for needle in shared_rules:
            assert needle not in frame.own_process, (
                f"{name}: process.md repeats the shared {needle!r}; it belongs "
                "in _common/process.md only"
            )


#: The section headings ``stream_inband``'s AGENTS.md carried before the
#: shared process was split out of it; the composed text keeps each one.
_LAB_SECTIONS = (
    "## Before you start: learn the machinery",
    "## Stage 1: the specification",
    "## Stage 2: the accelerator",
    "## The rules",
)


def test_the_lab_process_keeps_its_sections() -> None:
    text = load_frame(LAB_FRAME).process
    positions = [text.find(h) for h in _LAB_SECTIONS]
    assert all(p >= 0 for p in positions), (
        f"missing: {[h for h, p in zip(_LAB_SECTIONS, positions) if p < 0]}"
    )
    assert positions == sorted(positions), "the sections are out of order"


# ---------------------------------------------------------------------------
# What the process text must still say
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def process() -> str:
    return waveflow_get_process(LAB_FRAME)["process"]


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
