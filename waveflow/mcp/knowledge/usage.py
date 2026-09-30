"""The usage index: "show me X in use", answered from source, not from labels.

An agent that has never seen Waveflow does not need a description of
``HostActivated`` half as much as it needs the four places a real example
subclasses one.  This module parses the corpus's example sources and records,
for every Waveflow name, each spot that actually uses it.

Everything here is **derived** (the plan's first principle).  There are no
``teaches:`` tags to maintain, because a hand label answers only the question
its author thought of and goes stale the first time the code moves.

Two parsers:

* **Python, through the AST.**  Names imported from ``waveflow.*``, every later
  use of them, base classes, decorators, and the methods a module declares
  through a hook.  Going through the AST rather than a regex is what keeps a
  name inside a string or a comment out of the index.
* **C++, line by line.**  ``#pragma HLS`` directives, ``ns::fn`` calls into the
  generated utilities (``streamutils::``, ``*_array_utils::``), and ``#include``
  of a generated header.  A real parse would buy nothing: these three forms are
  the whole of what a hook file says about Waveflow.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import get_close_matches

from waveflow.mcp.knowledge.corpus import Corpus

__all__ = ["Use", "UsageIndex", "build_usage_index"]

#: Base classes worth calling out on an example card even when the source
#: imports them indirectly.  Used only to classify a class's *kind*; a class
#: with some other base is still indexed, just without a kind.
MODULE_KINDS: dict[str, str] = {
    "HostActivated": "host-activated module",
    "FreeRunMod": "free-running module",
    "HwModule": "hardware module",
    "SeqTB": "sequential testbench",
    "SimObj": "simulation object",
    "DataList": "schema (list)",
    "DataArray": "schema (array)",
    "DataSchema": "schema",
    "IntField": "schema field",
    "FloatField": "schema field",
    "EnumField": "schema field",
    "MemAddr": "schema field",
    "VitisRegMap": "register map",
    "BuildDag": "build graph",
}

#: Constructors that declare a port.  Matched on the callee name, so a
#: ``StreamIFSlave`` reached through an alias still counts.
_PORT_CTORS = re.compile(r"(?:^|\.)((?:[A-Z][A-Za-z0-9]*)?(?:IF|IFMaster|IFSlave))$")

#: Keyword arguments whose string value names a hand-written C++ body.
_HOOK_KEYWORDS = frozenset({"header", "impl_file", "task_header", "body", "impl"})

#: C++ source extensions, for both parsers.
_CPP_SUFFIXES = (".h", ".hpp", ".tpp", ".cpp", ".cc", ".cxx", ".c")

_PRAGMA = re.compile(r"^\s*#pragma\s+HLS\s+(\w+)")
_NS_CALL = re.compile(r"\b([a-z_][A-Za-z0-9_]*(?:::[a-z_][A-Za-z0-9_]*)+)\s*[(<]")
_INCLUDE = re.compile(r'^\s*#include\s+["<]([^">]+)[">]')


@dataclass(frozen=True)
class Use:
    """One place a symbol is used."""

    path: str
    line: int
    example: str
    how: str  #: "import", "call", "base", "decorator", "port", "pragma", "include"
    context: str = ""  #: the enclosing class or function, when there is one


@dataclass
class UsageIndex:
    """Symbol -> every use of it, plus the per-file facts the cards need."""

    uses: dict[str, list[Use]] = field(default_factory=lambda: defaultdict(list))
    #: path -> [(class name, base classes)]
    classes: dict[str, list[tuple[str, tuple[str, ...]]]] = field(default_factory=dict)
    #: path -> [(declaring class, attribute name, constructor)] for ports
    #: assigned to ``self``.  Keyed by class because one module file routinely
    #: holds the accelerator, its testbench and its schemas, and handing the
    #: testbench's ports to a schema card is worse than showing none.
    ports: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    #: path -> the names it imports from ``waveflow.*``
    imports: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: path -> the methods it marks ``@synthesizable``.  The hook body for one
    #: is ``<component>_<method>_impl.*`` unless ``impl_file=`` overrides it.
    synth_methods: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: path -> C++ filenames the source names in a string literal, which is how
    #: a ``KernelTask(header=...)`` points at a hand-written task body
    named_cpp: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def find(self, symbol: str) -> list[Use]:
        return list(self.uses.get(symbol, ()))

    def suggest(self, symbol: str, n: int = 5) -> list[str]:
        """Close symbol names, for a query that missed."""
        exact = get_close_matches(symbol, self.uses.keys(), n=n, cutoff=0.6)
        if exact:
            return exact
        low = symbol.lower()
        return sorted(
            (s for s in self.uses if low in s.lower()),
            key=lambda s: (len(s), s),
        )[:n]


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def _dotted(node: ast.expr) -> str:
    """``a.b.C`` for an attribute chain, ``C`` for a name, ``""`` otherwise."""
    parts: list[str] = []
    cur: ast.expr | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


class _PyVisitor(ast.NodeVisitor):
    """Collects uses of Waveflow names from one module."""

    def __init__(self, index: UsageIndex, path: str, example: str) -> None:
        self.index = index
        self.path = path
        self.example = example
        self.waveflow_names: set[str] = set()
        self.touched: set[str] = set()
        self._at: dict[tuple[str, int], Use] = {}
        self.scope: list[str] = []
        self.classes: list[tuple[str, tuple[str, ...]]] = []
        self.ports: list[tuple[str, str, str]] = []
        self.synth_methods: list[str] = []
        self.named_cpp: set[str] = set()

    # -- helpers --------------------------------------------------------
    #: `how` values, most specific first.  A class base is also a `Name` load,
    #: so both fire on the same line; only the stronger one is kept.
    _RANK = ("base", "decorator", "port", "import", "call", "ref")

    def _add(self, symbol: str, line: int, how: str) -> None:
        self.touched.add(symbol)
        seen = self._at.get((symbol, line))
        if seen is not None:
            if self._RANK.index(how) < self._RANK.index(seen.how):
                self.index.uses[symbol].remove(seen)
            else:
                return
        self.index.uses[symbol].append(
            use := Use(
                path=self.path,
                line=line,
                example=self.example,
                how=how,
                context=".".join(self.scope),
            )
        )
        self._at[(symbol, line)] = use

    def _interesting(self, symbol: str) -> bool:
        """Only names the example got from Waveflow, so ``np`` stays out."""
        return symbol in self.waveflow_names or symbol in MODULE_KINDS

    # -- visits ---------------------------------------------------------
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.split(".")[0] == "waveflow":
            for alias in node.names:
                local = alias.asname or alias.name
                self.waveflow_names.add(local)
                self._add(alias.name, node.lineno, "import")
                if local != alias.name:
                    self.waveflow_names.add(alias.name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] == "waveflow":
                self.waveflow_names.add(alias.asname or alias.name.split(".")[0])
                self._add(alias.name, node.lineno, "import")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = tuple(b for b in (_dotted(b) for b in node.bases) if b)
        self.classes.append((node.name, bases))
        for base in bases:
            leaf = base.rsplit(".", 1)[-1]
            if self._interesting(leaf):
                self._add(leaf, node.lineno, "base")
        for dec in node.decorator_list:
            self._decorator(dec)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # The method's own name goes on the scope first, so a decorator is
        # recorded against `PolyAccel.evaluate` rather than `PolyAccel`.  Which
        # method carries the hook is the whole answer to "show me
        # @synthesizable in use".
        self.scope.append(node.name)
        for dec in node.decorator_list:
            self._decorator(dec)
            target = dec.func if isinstance(dec, ast.Call) else dec
            if _dotted(target).rsplit(".", 1)[-1] == "synthesizable":
                self.synth_methods.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _function  # type: ignore[assignment]
    visit_AsyncFunctionDef = _function  # type: ignore[assignment]

    def _decorator(self, dec: ast.expr) -> None:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = _dotted(target).rsplit(".", 1)[-1]
        if name and self._interesting(name):
            self._add(name, dec.lineno, "decorator")

    def visit_Assign(self, node: ast.Assign) -> None:
        # `self.s_in = StreamIFSlave(...)` -- a port declaration.  Ports are
        # what an agent reads first off a card, and they are never written
        # down anywhere but here.
        if isinstance(node.value, ast.Call):
            ctor = _dotted(node.value.func).rsplit(".", 1)[-1]
            if ctor and _PORT_CTORS.search(ctor):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Attribute)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "self"
                    ):
                        owner = self.scope[0] if self.scope else ""
                        self.ports.append((owner, tgt.attr, ctor))
                        self._add(ctor, node.lineno, "port")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func).rsplit(".", 1)[-1]
        if name and self._interesting(name):
            self._add(name, node.lineno, "call")
        # A hand-written task body the module points at by name.  Two forms,
        # both in use: `KernelTask("vec_mult_task", "vec_mult_task.h", ...)`
        # positionally, and `KernelTask(header="bram_read_cmd_task.h")` by
        # keyword.  Scoped to the hook constructors and to those keywords
        # because any *other* string ending in `.h` is as likely to be an
        # include list or a generated header the example merely mentions.
        hook_call = name.endswith("Task")
        args: list[ast.expr] = list(node.args) if hook_call else []
        args += [
            kw.value
            for kw in node.keywords
            if hook_call or kw.arg in _HOOK_KEYWORDS
        ]
        for arg in args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.endswith(_CPP_SUFFIXES):
                    self.named_cpp.add(arg.value.rsplit("/", 1)[-1])
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # A bare mention -- a type annotation, an `isinstance`, a default.
        # Recorded as a call-site-less reference so `find_usage` still points
        # at it; `how="call"` would overstate what the line does.
        if isinstance(node.ctx, ast.Load) and self._interesting(node.id):
            self._add(node.id, node.lineno, "ref")
        self.generic_visit(node)


def _index_python(index: UsageIndex, path: str, text: str, example: str) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    v = _PyVisitor(index, path, example)
    v.visit(tree)
    if v.classes:
        index.classes[path] = v.classes
    if v.ports:
        index.ports[path] = v.ports
    if v.waveflow_names:
        index.imports[path] = tuple(sorted(v.waveflow_names))
    if v.synth_methods:
        index.synth_methods[path] = tuple(v.synth_methods)
    if v.named_cpp:
        index.named_cpp[path] = tuple(sorted(v.named_cpp))

    # Collapse the `ref` flood: a name used forty times in one file is one
    # fact, not forty.  Keep the structural uses and the first few refs.
    for symbol in v.touched:
        uses = index.uses[symbol]
        here = [u for u in uses if u.path == path]
        if len(here) <= 6:
            continue
        keep = [u for u in here if u.how != "ref"]
        refs = [u for u in here if u.how == "ref"]
        trimmed = keep + refs[: max(0, 6 - len(keep))]
        if len(trimmed) < len(here):
            index.uses[symbol] = [u for u in uses if u.path != path] + trimmed


# ---------------------------------------------------------------------------
# C++
# ---------------------------------------------------------------------------


def _index_cpp(index: UsageIndex, path: str, text: str, example: str) -> None:
    for n, line in enumerate(text.splitlines(), start=1):
        m = _PRAGMA.match(line)
        if m:
            index.uses[f"#pragma HLS {m.group(1).lower()}"].append(
                Use(path=path, line=n, example=example, how="pragma")
            )
            continue
        m = _INCLUDE.match(line)
        if m:
            index.uses[m.group(1)].append(
                Use(path=path, line=n, example=example, how="include")
            )
            continue
        for call in _NS_CALL.finditer(line):
            qualified = call.group(1)
            index.uses[qualified].append(
                Use(path=path, line=n, example=example, how="call")
            )
            # Also reachable by the namespace alone: "what does streamutils do
            # here" is the question an agent actually asks.
            ns = qualified.split("::", 1)[0]
            index.uses[f"{ns}::"].append(
                Use(path=path, line=n, example=example, how="call")
            )


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_usage_index(corpus: Corpus) -> UsageIndex:
    """Parse every example source in *corpus* into a :class:`UsageIndex`."""
    index = UsageIndex()
    for rel, example in sorted(corpus.file_owner.items()):
        try:
            text = corpus.read(rel)
        except OSError:
            continue
        if rel.endswith(".py"):
            _index_python(index, rel, text, example)
        elif rel.endswith(_CPP_SUFFIXES):
            _index_cpp(index, rel, text, example)
    return index
