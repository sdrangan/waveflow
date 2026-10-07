"""Step 7.1 (and AC9): nothing in ``waveflow/linalg/`` or ``tests/linalg/`` imports from ``examples/``."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_no_example_imports():
    files = sorted((ROOT / "waveflow" / "linalg").glob("*.py")) + sorted(
        (ROOT / "tests" / "linalg").glob("*.py")
    )
    assert len(files) >= 8
    bad = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            bad += [f"{path.name}: {n}" for n in names if n.split(".")[0] == "examples"]
    assert not bad, bad
