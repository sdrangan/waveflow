"""AC1 of ``plans/cpu_model.md``: the processor model stays a library.

Nothing in ``waveflow/cpu/`` or ``tests/cpu/`` imports from ``examples/`` (an example may use the
library, never the reverse), nor from ``specsense`` (tracerspecsense is the prior art this model
replaces; it is read, not depended on).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_ROOTS = {"examples", "specsense"}


def _imported_roots(path: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return [n.split(".")[0] for n in names]


def test_no_example_or_specsense_imports():
    files = sorted((ROOT / "waveflow" / "cpu").rglob("*.py")) + sorted(
        (ROOT / "tests" / "cpu").rglob("*.py")
    )
    assert len(files) >= 4
    bad = [
        f"{p.relative_to(ROOT)}: {root}"
        for p in files
        for root in _imported_roots(p)
        if root in FORBIDDEN_ROOTS
    ]
    assert not bad, bad


def test_the_public_names_import():
    from waveflow.cpu import CpuConfig, Processor  # noqa: F401
