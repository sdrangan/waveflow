"""``waveflow new-accel``: a project that runs as generated, with the function removed.

The scaffold's whole job is to hand someone a tree where **everything but the
function already works** -- the schemas, the module, the testbenches, the
build DAG, the hook wiring -- so that the first thing they debug is their own
maths and not Waveflow's plumbing.  A scaffold that does not run is worse than
no scaffold, because it teaches the agent that the reference flow is broken.

**The template is the reference example, renamed.**  There is no committed
copy of it.  A frame names its reference example in ``frame.toml``, and the
scaffold copies that example out of the checkout and renames every identifier
derived from it.  The alternative -- a ``template/`` directory in the frame --
means a 1,300-line derived duplicate that silently rots the first time the
example changes, and the plan's own first principle is *derived, not
hand-labeled*.  The cost is that scaffolding needs a checkout, the same
condition the knowledge index already has.

**What is stubbed, and what is not.**  Only the arithmetic is removed: the
Python function (``poly_eval`` -> ``<name>_eval``) and its C++ twin in the
kernel body both become an identity with a ``TODO``.  Framing, error handling,
the response header, the register map, the scenarios and their checker, the
C++ testbench and the DAG are left exactly as the example has them, because
those are what the frame specifies and what the agent is meant to read rather
than reinvent.  The expected responses in ``scenarios.py`` call the same
function, so the stubbed project checks green end to end.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from waveflow.mcp.frames import DEFAULT_FRAME, Frame, load_frame
from waveflow.mcp.knowledge.corpus import load_corpus
from waveflow.mcp.knowledge.roots import repo_root

__all__ = ["ScaffoldError", "ScaffoldResult", "new_accel"]


class ScaffoldError(RuntimeError):
    """The project could not be written."""


@dataclass
class ScaffoldResult:
    name: str
    frame: str
    directory: Path
    files: list[str]
    source_example: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "frame": self.frame,
            "directory": str(self.directory),
            "source_example": self.source_example,
            "files": list(self.files),
            "next": (
                f"cd {self.directory.name} && python {self.name}_build.py "
                f"--through check_pysim"
            ),
        }


#: A project name has to be a Python identifier: it becomes a module name, a
#: C++ kernel name and a namespace.
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

#: Files of the reference example that are its *results*, not its source.
#: Copying a worked run into a fresh project is how someone ends up comparing
#: their design against the example's numbers without noticing.
_SKIP_DIRS = frozenset(
    {"__pycache__", "gen", "include", "logs", "results", "vcd", "data", "src"}
)
_SKIP_SUFFIXES = (".ipynb", ".bin", ".vcd", ".json", ".csv", ".log")


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_") if part)


def _renamer(old: str, new: str):
    """Substitute the example's name token for the project's, both cases.

    Word boundaries here are not ``\\b``: ``_`` is a word character, so
    ``\\bpoly\\b`` would refuse to touch ``poly_build``.  What is wanted is a
    *token* boundary -- the identifier ``poly`` in ``poly_build.py``,
    ``poly_source`` and ``PolyAccel``, but never the English word inside
    ``polynomial``.

    So: lowercase ``poly`` matches when not surrounded by a letter or digit on
    the left and not followed by a lowercase letter or digit, which catches
    ``poly_x`` and bare ``poly`` while skipping ``polynomial``.  Capitalised
    ``Poly`` may be followed by an uppercase letter, which is the CamelCase
    boundary in ``PolyCmdHdr``, but not by a lowercase one, which is what
    ``Polynomial`` is.
    """
    lower = re.compile(rf"(?<![A-Za-z0-9]){re.escape(old)}(?![a-z0-9])")
    upper = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(old.capitalize())}(?![a-z0-9])"
    )

    def rename(text: str) -> str:
        return upper.sub(_camel(new), lower.sub(new, text))

    return rename


# ---------------------------------------------------------------------------
# Stubbing the compute
# ---------------------------------------------------------------------------


def apply_stubs(
    text: str, stubs: list[dict[str, str]], name: str
) -> tuple[str, list[str]]:
    """Cut each declared region out of *text* and put the replacement in.

    Anchors, not patterns.  An earlier version of this found the compute with
    a regex over arbitrary example source, and it silently matched nothing in
    the Python -- producing a project that claimed to be stubbed and was not.
    A frame declares its compute region by the first and last line of it, in
    ``frame.toml``; if an anchor stops matching, that is a loud failure with
    the anchor's own text in the message.

    Anchors are matched on the stripped line, and the replacement is
    re-indented to the indentation the start anchor had, so the same
    declaration works for a line nested three levels deep in a loop.
    """
    lines = text.splitlines()
    applied: list[str] = []
    for stub in stubs:
        start_anchor = stub["start"].strip()
        end_anchor = stub["end"].strip()
        try:
            i = next(n for n, ln in enumerate(lines) if ln.strip() == start_anchor)
        except StopIteration:
            continue
        try:
            j = next(
                n for n, ln in enumerate(lines) if n >= i and ln.strip() == end_anchor
            )
        except StopIteration:
            raise ScaffoldError(
                f"stub {stub.get('description', '?')!r}: found its start anchor "
                f"but not its end anchor {end_anchor!r}"
            ) from None

        pad = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        replacement = stub["replacement"].format(name=name).strip("\n")
        body = [
            (pad + line) if line.strip() else "" for line in replacement.splitlines()
        ]
        lines[i : j + 1] = body
        applied.append(stub.get("description", stub["file"]))
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), applied


# ---------------------------------------------------------------------------
# The layout.md stub
# ---------------------------------------------------------------------------

_LAYOUT = """# Wire layout

**Obtain every row below by serializing an instance with Waveflow and writing
down what came out.** Do not derive it by reading the schema. Hand-derived
layouts are where packing bugs come from, and this file exists to make the
real one visible before any C++ is written.

For each of the command header, the response header, the footer and the
sample burst: the word index, the bit range, the field, and its type.

## Command header

| Word | Bits | Field | Type |
| --- | --- | --- | --- |
| | | | *TODO* |

## Response header

| Word | Bits | Field | Type |
| --- | --- | --- | --- |
| | | | *TODO* |

## Response footer

| Word | Bits | Field | Type |
| --- | --- | --- | --- |
| | | | *TODO* |

## Sample burst

Samples per word, packing order, and what happens to an odd final sample.

*TODO*
"""


def _pointer(name: str, tool: str) -> str:
    return (
        f"# {name}\n\n"
        f"The process for this project is in [AGENTS.md](AGENTS.md), and the\n"
        f"specification it has to meet is in [frame.md](frame.md). Read both\n"
        f"before writing anything.\n\n"
        f"({tool} reads this file; the content is in AGENTS.md so that all of\n"
        f"the assistants, and `waveflow_get_process`, are reading one text.)\n"
    )


# ---------------------------------------------------------------------------
# new_accel
# ---------------------------------------------------------------------------


def _template_example(frame: Frame) -> str:
    names = frame.reference_examples
    if not names:
        raise ScaffoldError(f"frame {frame.name!r} names no reference example")
    return names[0]


def new_accel(
    name: str,
    frame: str | None = None,
    dest: Path | str | None = None,
    *,
    force: bool = False,
) -> ScaffoldResult:
    """Write a new accelerator project named *name*, in *frame*.

    The project runs as generated: ``python <name>_build.py --through check_pysim``
    passes before a line of it has been edited.
    """
    if not _NAME.match(name):
        raise ScaffoldError(
            f"{name!r} is not a usable project name -- it becomes a Python "
            "module, a C++ kernel name and a namespace, so it must be "
            "lower_snake_case starting with a letter"
        )

    frame_name = frame or DEFAULT_FRAME
    found = load_frame(frame_name)
    if found is None:
        raise ScaffoldError(
            f"no frame {frame_name!r}; known frames: {sorted(__import__('waveflow.mcp.frames', fromlist=['x']).list_frames())}"
        )

    template = found.meta.get("template", {})
    example = template.get("source_example") or _template_example(found)
    stubs = list(template.get("stub", []))
    skip_dirs = frozenset(template.get("skip_dirs", _SKIP_DIRS))
    skip_suffixes = tuple(template.get("skip_suffixes", _SKIP_SUFFIXES))
    corpus = load_corpus()
    source = corpus.examples.get(example)
    if source is None or not source.example_dir:
        raise ScaffoldError(
            f"frame {frame_name!r} names reference example {example!r}, which "
            "is not in the docs table of contents -- scaffolding reads the "
            "example out of the checkout, so it has to be one the index knows"
        )
    src_dir = repo_root() / source.example_dir
    if not src_dir.is_dir():
        raise ScaffoldError(f"{src_dir} does not exist")

    out_dir = Path(dest) if dest is not None else Path.cwd() / name
    out_dir = out_dir.resolve()
    if out_dir.exists() and any(out_dir.iterdir()) and not force:
        raise ScaffoldError(
            f"{out_dir} already exists and is not empty; pass force=True to "
            "write into it anyway"
        )

    # The token the example's identifiers are built from.  `examples/mem_copy`
    # holds `mem_copy.py`, `examples/stream_inband` holds `poly.py`: the
    # directory name is not it, so a frame may state the token outright and
    # the build-script stem is the fallback.
    stem = template.get("name_token") or _main_module_stem(src_dir, source.files)
    rename = _renamer(stem, name)

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    applied: set[str] = set()

    for path in sorted(src_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src_dir)
        if set(rel.parts[:-1]) & skip_dirs or rel.name.endswith(skip_suffixes):
            continue
        if rel.name.startswith("."):
            continue

        target = out_dir / Path(*[rename(part) for part in rel.parts])
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            shutil.copy2(path, target)
            written.append(target.relative_to(out_dir).as_posix())
            continue

        # Stub first, then rename: the anchors in `frame.toml` are written in
        # the example's own names, which is what a reviewer comparing them
        # against the example can actually check.
        mine = [s for s in stubs if s.get("file") == rel.as_posix()]
        if mine:
            text, done = apply_stubs(text, mine, name)
            applied.update(done)
        target.write_text(rename(text), encoding="utf-8")
        written.append(target.relative_to(out_dir).as_posix())

    # --- the frame's own documents -------------------------------------
    for filename, content in (
        ("AGENTS.md", found.process),
        ("frame.md", found.specification),
        ("CLAUDE.md", _pointer(name, "Claude Code")),
        ("GEMINI.md", _pointer(name, "Gemini")),
    ):
        (out_dir / filename).write_text(content, encoding="utf-8")
        written.append(filename)

    # --- layout.md -------------------------------------------------------
    # The one Stage 1 artifact the reference design does not already carry: the
    # scenarios, their expected responses and the checker come along in
    # scenarios.py, the function and the schemas in <name>.py.
    (out_dir / "layout.md").write_text(_LAYOUT, encoding="utf-8")
    written.append("layout.md")

    # A stub that did not land means the example moved and the project still
    # contains the reference function.  That is exactly the failure worth
    # being loud about: it looks like a working scaffold and is not one.
    missed = [
        s.get("description", s["file"]) for s in stubs
        if s.get("description", s["file"]) not in applied
    ]
    if missed:
        raise ScaffoldError(
            "the frame declares compute regions that no longer match "
            f"{example}: {missed}. The project would have shipped with the "
            "reference function still in it, so nothing was written. Fix the "
            f"anchors in {found.directory / 'frame.toml'}."
        )

    return ScaffoldResult(
        name=name,
        frame=frame_name,
        directory=out_dir,
        files=sorted(written),
        source_example=example,
    )


def _main_module_stem(src_dir: Path, files: tuple[str, ...]) -> str:
    """The token every identifier in the example is built from.

    Derived from the build script rather than from the directory name: the
    example that `frame.toml` calls ``stream_inband`` names everything
    ``poly``, and its ``poly_build.py`` is the one file guaranteed to carry
    that token.
    """
    builds = sorted(
        (f for f in files if f.endswith("_build.py")), key=len
    )
    if builds:
        return Path(builds[0]).name[: -len("_build.py")]
    mains = sorted((f for f in files if f.endswith(".py")), key=len)
    if mains:
        return Path(mains[0]).stem
    raise ScaffoldError(f"no Python source found under {src_dir}")


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


def waveflow_new_accel_project(
    name: str, frame: str | None = None, directory: str | None = None
) -> dict[str, object]:
    """Write a new accelerator project that runs before it is edited.

    The MCP wrapper around :func:`new_accel`.  Errors come back as data, not
    as an exception, because a tool that raises tells the model only that
    something went wrong.
    """
    try:
        result = new_accel(name, frame=frame, dest=directory)
    except ScaffoldError as exc:
        return {"error": str(exc)}
    out = result.to_dict()
    out["read_first"] = ["AGENTS.md", "frame.md"]
    return out
