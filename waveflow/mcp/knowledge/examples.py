"""Example cards: what an agent is told about an example before reading it.

A card is the answer to "which example should I copy?".  Every field on it is
**derived** -- from the docs front matter a human already wrote for the TOC, or
from the source itself through the usage index.  Nothing is hand-labeled, so
nothing goes stale when the example moves.

The synopsis is the TOC page's ``summary:``.  That is not a fallback: those
summaries are written for humans browsing the docs, they are already accurate,
and the plan makes them the semantic index for the whole corpus.  A module
docstring's first paragraph stands in only when a page has no summary at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from waveflow.mcp.knowledge.corpus import Corpus, ExampleSource, generated_note
from waveflow.mcp.knowledge.usage import MODULE_KINDS, UsageIndex

__all__ = ["ExampleCard", "ModuleInfo", "build_cards"]


@dataclass(frozen=True)
class ModuleInfo:
    """One class an example defines, and what Waveflow kind it is."""

    name: str
    kind: str          #: "host-activated module", "schema (list)", ...
    bases: tuple[str, ...]
    path: str
    ports: tuple[tuple[str, str], ...] = ()  #: (attribute, interface type)


@dataclass
class ExampleCard:
    """The per-example summary ``waveflow_list_examples`` returns."""

    name: str
    title: str
    synopsis: str
    example_dir: str
    doc_path: str
    modules: list[ModuleInfo] = field(default_factory=list)
    hook_files: list[str] = field(default_factory=list)
    build_script: str = ""
    doc_pages: list[str] = field(default_factory=list)
    files: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self, *, with_files: bool = True) -> dict[str, object]:
        out: dict[str, object] = {
            "name": self.name,
            "title": self.title,
            "synopsis": self.synopsis,
            "example_dir": self.example_dir,
            "doc_path": self.doc_path,
            "modules": [
                {
                    "name": m.name,
                    "kind": m.kind,
                    "bases": list(m.bases),
                    "path": m.path,
                    "ports": [{"name": p, "interface": t} for p, t in m.ports],
                }
                for m in self.modules
            ],
            "hook_files": list(self.hook_files),
            "build_script": self.build_script,
            "doc_pages": list(self.doc_pages),
            "frames": frames_using(self.name),
        }
        if with_files:
            out["files"] = list(self.files)
        else:
            out["n_files"] = len(self.files)
        return out


def frames_using(example: str) -> list[str]:
    """The frames that name *example* as a reference, primary users first.

    A card is the fallback when no frame fits a spec; when one does, the card
    says so, so an agent that started from the examples still finds the
    process built around its reference.
    """
    from waveflow.mcp.frames import list_frames

    users = [(f.reference_examples.index(example), name)
             for name, f in list_frames().items() if example in f.reference_examples]
    return [name for _, name in sorted(users)]


#: Extensions a hand-written C++ hook body can have.  Which of them actually
#: *is* one is decided by `_hook_files`, not by the extension.
_HOOK_SUFFIXES = (".tpp", ".h", ".hpp", ".cpp", ".cc", ".cxx")


def _hook_files(
    corpus: Corpus, source: ExampleSource, usage: UsageIndex
) -> list[str]:
    """The hand-written C++ bodies of *source*, derived from its Python.

    Extension alone is useless here: an example directory holds generated
    schema headers, a copied XSI harness and the real hook bodies, all ``.h``,
    and some examples commit their generated headers while others ignore them.

    The Python says which is which, in the two ways Waveflow lets it:

    * ``@synthesizable`` with no ``synth_fn`` emits a call into
      ``<component>_<method>_impl.*`` -- so a tracked file whose stem ends
      ``_<method>_impl`` is that method's body;
    * ``KernelTask(header="bram_read_cmd_task.h")`` and friends name the file
      outright, as a string literal.

    When a named body appears twice -- once where it was written and once
    where the build copied it into ``include/`` -- the source copy wins.
    """
    py_files = [f for f in source.files if f.endswith(".py")]
    methods = {m for f in py_files for m in usage.synth_methods.get(f, ())}
    named = {n for f in py_files for n in usage.named_cpp.get(f, ())}

    # `gen/` is codegen output and `xsi/` is the BFM harness copied in from
    # `waveflow/build/xsi/`; neither is a body this example's author wrote,
    # even where the tree commits them.
    cpp = [
        f
        for f in source.files
        if f.endswith(_HOOK_SUFFIXES)
        and f in corpus.tracked
        and "/gen/" not in f"/{f}"
        and "/xsi/" not in f"/{f}"
    ]
    by_name: dict[str, list[str]] = {}
    for f in cpp:
        name = Path(f).name
        stem = Path(f).stem
        if name in named or any(stem.endswith(f"_{m}_impl") for m in methods):
            by_name.setdefault(name, []).append(f)

    out: list[str] = []
    for name in sorted(by_name):
        paths = sorted(by_name[name], key=lambda p: ("/include/" in f"/{p}", p))
        out.append(paths[0])
    return out


def _module_docstring_lead(corpus: Corpus, source: ExampleSource) -> str:
    """First paragraph of the example's main module docstring."""
    import ast

    stem = Path(source.example_dir).name
    candidates = [f for f in source.files if f.endswith(".py")]
    # The "main" module is the one named after the directory when there is one,
    # otherwise the shortest path -- the top-level file rather than a helper.
    candidates.sort(key=lambda p: (Path(p).stem != stem, len(p), p))
    for rel in candidates:
        try:
            doc = ast.get_docstring(ast.parse(corpus.read(rel)))
        except (OSError, SyntaxError):
            continue
        if doc:
            return " ".join(doc.strip().split("\n\n")[0].split())
    return ""


def _kind_of(bases: tuple[str, ...]) -> str:
    for base in bases:
        leaf = base.rsplit(".", 1)[-1]
        if leaf in MODULE_KINDS:
            return MODULE_KINDS[leaf]
    return ""


def build_cards(corpus: Corpus, usage: UsageIndex) -> dict[str, ExampleCard]:
    """One :class:`ExampleCard` per TOC example."""
    cards: dict[str, ExampleCard] = {}
    for name, source in corpus.examples.items():
        synopsis = source.summary or _module_docstring_lead(corpus, source)

        modules: list[ModuleInfo] = []
        for rel in source.files:
            for cls, bases in usage.classes.get(rel, []):
                kind = _kind_of(bases)
                if not kind:
                    continue
                modules.append(
                    ModuleInfo(
                        name=cls,
                        kind=kind,
                        bases=bases,
                        path=rel,
                        ports=tuple(
                            (attr, ctor)
                            for owner, attr, ctor in usage.ports.get(rel, ())
                            if owner == cls
                        ),
                    )
                )

        hooks = _hook_files(corpus, source, usage)
        build = next(
            (f for f in source.files if f.endswith("_build.py")),
            next((f for f in source.files if f.endswith("run.tcl")), ""),
        )

        files: list[dict[str, object]] = []
        for rel in list(source.files) + list(source.extra_files):
            entry: dict[str, object] = {"path": rel}
            note = generated_note(rel, corpus.tracked)
            if note:
                entry["generated"] = True
                entry["note"] = note
            if rel in source.extra_files:
                entry["linked_from_docs"] = True
            files.append(entry)

        cards[name] = ExampleCard(
            name=name,
            title=source.title,
            synopsis=synopsis,
            example_dir=source.example_dir,
            doc_path=source.doc_path,
            modules=modules,
            hook_files=hooks,
            build_script=build,
            doc_pages=list(source.doc_pages),
            files=files,
        )
    return cards
