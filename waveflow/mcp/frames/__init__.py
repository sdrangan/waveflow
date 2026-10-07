"""Frames: the unit of extension for "build me an accelerator".

A **frame** is one architecture an agent can be asked to build in.  It is a
bundle of text with no code of its own::

    waveflow/mcp/frames/<frame>/
      frame.md      the specification: protocol, errors, stages, comparisons, report
      process.md    the ordered steps and which tool to use at each
      prompts/      example function specs for this frame
      frame.toml    name, synopsis, reference examples, the prompt list

Adding a frame is adding a directory.  Nothing here knows what
``stream_inband`` is; :func:`list_frames` reads whatever directories exist.

``process.md`` is the single source for both :func:`waveflow_get_process` and
the ``AGENTS.md`` the scaffold writes, so the tool an agent calls and the file
sitting in its project cannot drift apart.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "FRAMES_DIR",
    "DEFAULT_FRAME",
    "Frame",
    "list_frames",
    "load_frame",
    "waveflow_list_frames",
    "waveflow_get_process",
]

#: The directory this package lives in; each subdirectory with a ``frame.toml``
#: is a frame.  Package data, so it ships in the wheel (see ``pyproject.toml``).
FRAMES_DIR = Path(__file__).resolve().parent

#: The frame an accelerator request means when it does not say which.  The lab
#: needs exactly one, and this is it.
DEFAULT_FRAME = "stream_inband"


@dataclass(frozen=True)
class Frame:
    """One frame, read from its directory."""

    name: str
    directory: Path
    meta: dict[str, Any]

    @property
    def synopsis(self) -> str:
        return " ".join(str(self.meta.get("synopsis", "")).split())

    @property
    def reference_examples(self) -> list[str]:
        return list(self.meta.get("reference_examples", []))

    def read(self, filename: str) -> str:
        return (self.directory / filename).read_text(encoding="utf-8")

    @property
    def process(self) -> str:
        """``process.md`` -- the steps, and what the scaffold writes as AGENTS.md."""
        return self.read("process.md")

    @property
    def specification(self) -> str:
        """``frame.md`` -- the part of every spec that does not change."""
        return self.read("frame.md")

    def prompts(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for entry in self.meta.get("prompts", []):
            name = entry.get("file", "")
            if (self.directory / "prompts" / name).is_file():
                out.append(
                    {"file": name, "synopsis": " ".join(str(entry.get("synopsis", "")).split())}
                )
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "synopsis": self.synopsis,
            "reference_examples": self.reference_examples,
            "module_kind": self.meta.get("module_kind", ""),
            "testbench_kind": self.meta.get("testbench_kind", ""),
            "control": self.meta.get("control", ""),
            "parameters": self.meta.get("parameters", ""),
            "prompts": self.prompts(),
        }


@lru_cache(maxsize=1)
def list_frames() -> dict[str, Frame]:
    """Every frame on disk, by name."""
    out: dict[str, Frame] = {}
    if not FRAMES_DIR.is_dir():
        return out
    for d in sorted(p for p in FRAMES_DIR.iterdir() if p.is_dir()):
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


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def waveflow_list_frames() -> dict[str, Any]:
    """The architectures an accelerator can be asked for, with their synopses."""
    frames = list_frames()
    return {
        "frames": [f.to_dict() for f in frames.values()],
        "hint": (
            "Call waveflow_get_process(frame) for the ordered steps before "
            "writing anything."
        ),
    }


def waveflow_get_process(frame: str | None = None) -> dict[str, Any]:
    """The build process for *frame*: the steps, the tools, and the rules.

    Returns ``process.md`` verbatim.  The scaffold writes the same text as the
    project's ``AGENTS.md``, so an agent that reads either one is reading the
    same instructions.  ``frame.md``, the specification the design must meet,
    comes back alongside it -- both are short, and an agent that fetches the
    process and then guesses at the protocol has been given half of what it
    needs.
    """
    # `None` rather than a default string: the registry declares these schemas
    # strict, which makes every property required, so a client that wants the
    # default sends null.  A bare `str` annotation makes MCPServer reject that
    # (a pydantic "Input should be a valid string" error, still true in mcp 2)
    # before the function is ever called.
    frame = frame or DEFAULT_FRAME
    found = load_frame(frame)
    if found is None:
        known = sorted(list_frames())
        return {
            "error": f"no frame {frame!r}",
            "known": known,
            "hint": "call waveflow_list_frames() to see what each one is for",
        }
    return {
        "frame": found.name,
        "synopsis": found.synopsis,
        "reference_examples": found.reference_examples,
        "process": found.process,
        "specification": found.specification,
        "prompts": found.prompts(),
    }
