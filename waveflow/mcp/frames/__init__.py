"""Frames: the unit of extension for "build me an accelerator".

A **frame** is a design pattern realized in one system shape and one
realization flow, with a reference example that proves it.  It is a bundle of
text with no code of its own::

    waveflow/mcp/frames/<frame>/
      frame.md      what the frame fixes, and what a spec must decide
      process.md    the frame's own steps, and which tool to use at each
      prompts/      example specs for this frame
      frame.toml    name, the three axes, references, the prompt list, and
                    an optional [template] for the scaffold

    waveflow/mcp/frames/_common/
      process.md    what every frame shares; its ``<!-- FRAME -->`` line is
                    where a frame's own process.md goes
      unframed.md   what fills that line when no frame is named: how to
                    choose one, and the menu (``<!-- MENU -->``)

Adding a frame is adding a directory.  Nothing here knows what
``stream_inband`` is; :func:`list_frames` reads whatever directories exist,
and a ``_``-prefixed directory is never a frame.

:attr:`Frame.process` is the single source for both :func:`waveflow_get_process`
and the ``AGENTS.md`` the scaffold writes, so the tool an agent calls and the
file sitting in its project cannot drift apart -- and because the shared part
is one file, it cannot drift between frames either.

There is no default frame.  Choosing the architecture is the first part of
building from a spec, so :func:`waveflow_get_process` with no frame returns
the generic process and the menu, not one frame's steps.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "FRAMES_DIR",
    "COMMON_DIR",
    "Frame",
    "list_frames",
    "load_frame",
    "generic_process",
    "scaffold_frames",
    "waveflow_list_frames",
    "waveflow_get_process",
]

#: The directory this package lives in; each subdirectory with a ``frame.toml``
#: is a frame.  Package data, so it ships in the wheel (see ``pyproject.toml``).
FRAMES_DIR = Path(__file__).resolve().parent

#: The text every frame shares.  Not a frame: its name starts with ``_``.
COMMON_DIR = FRAMES_DIR / "_common"

_FRAME_SLOT = "<!-- FRAME -->"
_MENU_SLOT = "<!-- MENU -->"


def _flat(text: Any) -> str:
    return " ".join(str(text or "").split())


def _common(filename: str) -> str:
    return (COMMON_DIR / filename).read_text(encoding="utf-8")


def _compose(own: str) -> str:
    """The shared process with *own* in its frame slot."""
    common = _common("process.md")
    if _FRAME_SLOT not in common:
        raise RuntimeError(f"{COMMON_DIR / 'process.md'} has no {_FRAME_SLOT} line")
    return common.replace(_FRAME_SLOT, own.strip())


@dataclass(frozen=True)
class Frame:
    """One frame, read from its directory."""

    name: str
    directory: Path
    meta: dict[str, Any]

    @property
    def synopsis(self) -> str:
        return _flat(self.meta.get("synopsis"))

    @property
    def reference_examples(self) -> list[str]:
        return list(self.meta.get("reference_examples", []))

    @property
    def pattern(self) -> str:
        return str(self.meta.get("pattern", ""))

    @property
    def pattern_doc(self) -> str:
        """The guide page for the frame's pattern, or ``""``."""
        return f"docs/guide/patterns/{self.pattern}.md" if self.pattern else ""

    @property
    def has_scaffold(self) -> bool:
        """Whether ``waveflow_new_accel_project`` can start a project in it."""
        return bool(self.meta.get("template"))

    def read(self, filename: str) -> str:
        return (self.directory / filename).read_text(encoding="utf-8")

    @property
    def own_process(self) -> str:
        """``process.md`` alone -- the frame's own steps."""
        return self.read("process.md")

    @property
    def process(self) -> str:
        """The shared process with this frame's steps in it.

        What :func:`waveflow_get_process` returns and the scaffold writes as
        ``AGENTS.md``.
        """
        return _compose(self.own_process)

    @property
    def specification(self) -> str:
        """``frame.md`` -- the part of every spec that does not change."""
        return self.read("frame.md")

    def prompts(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for entry in self.meta.get("prompts", []):
            name = entry.get("file", "")
            if (self.directory / "prompts" / name).is_file():
                out.append({"file": name, "synopsis": _flat(entry.get("synopsis"))})
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "synopsis": self.synopsis,
            "pattern": self.pattern,
            "pattern_doc": self.pattern_doc,
            "shape": _flat(self.meta.get("shape")),
            "flow": _flat(self.meta.get("flow")),
            "choose_when": _flat(self.meta.get("choose_when")),
            "reference_examples": self.reference_examples,
            "has_scaffold": self.has_scaffold,
            "prompts": self.prompts(),
        }


@lru_cache(maxsize=1)
def list_frames() -> dict[str, Frame]:
    """Every frame on disk, by name."""
    out: dict[str, Frame] = {}
    if not FRAMES_DIR.is_dir():
        return out
    for d in sorted(p for p in FRAMES_DIR.iterdir() if p.is_dir()):
        if d.name.startswith("_"):
            continue
        toml = d / "frame.toml"
        if not toml.is_file():
            continue
        meta = tomllib.loads(toml.read_text(encoding="utf-8"))
        out[meta.get("name", d.name)] = Frame(
            name=meta.get("name", d.name), directory=d, meta=meta
        )
    return out


def load_frame(name: str) -> Frame | None:
    return list_frames().get(name)


def scaffold_frames() -> list[str]:
    """The frames ``waveflow_new_accel_project`` can start a project in."""
    return [name for name, f in list_frames().items() if f.has_scaffold]


def _menu() -> str:
    """The frames as a Markdown list, for the generic process."""
    lines = ["### The frames", ""]
    for f in list_frames().values():
        d = f.to_dict()
        refs = ", ".join(f"`{r}`" for r in d["reference_examples"])
        lines += [
            f"- **`{f.name}`** -- {d['choose_when']}",
            f"  - pattern: `{d['pattern']}`; shape: {d['shape']}; flow: {d['flow']}",
            f"  - references: {refs}; scaffold: {'yes' if d['has_scaffold'] else 'no'}",
        ]
    return "\n".join(lines)


def generic_process() -> str:
    """The process for a request that names no frame: choose, then follow."""
    return _compose(_common("unframed.md").replace(_MENU_SLOT, _menu()))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def waveflow_list_frames() -> dict[str, Any]:
    """The architecture menu: each frame with its pattern, shape, flow and references."""
    return {
        "frames": [f.to_dict() for f in list_frames().values()],
        "hint": (
            "Pick the frame whose pattern, shape and flow match the spec, then "
            "call waveflow_get_process(frame). If none matches, call "
            "waveflow_list_examples() for the closest reference and "
            "waveflow_get_process() for the generic process."
        ),
    }


def waveflow_get_process(frame: str | None = None) -> dict[str, Any]:
    """The build process: generic with no *frame*, the frame's own with one.

    With a frame, returns the shared process with the frame's steps in it --
    the same text the scaffold writes as ``AGENTS.md`` -- and ``frame.md``,
    the specification the design must meet, alongside it: both are short, and
    an agent that fetches the process and then guesses at the protocol has
    been given half of what it needs.

    With none, returns the generic process: how to choose a frame, the menu,
    and the steps to follow a reference when no frame fits.
    """
    # `None` rather than a default string: the registry declares these schemas
    # strict, which makes every property required, so a client that wants the
    # generic process sends null.  A bare `str` annotation makes MCPServer
    # reject that (a pydantic "Input should be a valid string" error, still
    # true in mcp 2) before the function is ever called.
    if not frame:
        return {
            "frame": None,
            "process": generic_process(),
            "frames": [f.to_dict() for f in list_frames().values()],
            "hint": (
                "Choose a frame from 'frames' and call waveflow_get_process(frame) "
                "for its own steps, or follow the generic process with the "
                "closest example from waveflow_list_examples()."
            ),
        }
    found = load_frame(frame)
    if found is None:
        return {
            "error": f"no frame {frame!r}",
            "known": sorted(list_frames()),
            "hint": "call waveflow_list_frames() to see what each one is for",
        }
    return {
        "frame": found.name,
        "synopsis": found.synopsis,
        "reference_examples": found.reference_examples,
        "has_scaffold": found.has_scaffold,
        "process": found.process,
        "specification": found.specification,
        "prompts": found.prompts(),
    }
