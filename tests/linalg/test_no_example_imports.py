"""Step 7.1 and AC9 (plan ``plans/mimo_cg/mimo_cg_paper_sims.md``): Waveflow stays lean.

* Nothing in ``waveflow/linalg/`` or ``tests/linalg/`` imports from ``examples/``.
* Waveflow gained only the files of the committed manifests (gates 7.0, 8.0 and 9.0).
* None of those files carries the example's constants in its code: no ``INT_BITS``, no
  ``DEFAULT_N``, no part name, no clock, no plan path.  Comments and docstrings, which say where a
  rule was measured, are not code, and the packaged platform's JSON files are its data (they name
  the part they were measured on).  Three things are allowed and named here: the components' default
  clock, as every free-running module has one; the name of the packaged platform the cost models
  describe; and ``dsps_per_lane``, the 2024.1 DSP binding kept as code (it names no part).
"""

from __future__ import annotations

import ast
import io
import re
import shutil
import subprocess
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = "waveflow/calib/platforms/xczu48dr_250mhz_vitis2024_1"
_TASKS = (
    "systolic_rx_task", "systolic_load_task", "systolic_core_task", "systolic_store_task",
    "cg_vector_rx_task", "cg_vector_load_task", "cg_vector_task", "cg_vector_store_task",
)  # fmt: skip

#: The files Waveflow gained in Phases 7–9 (the manifests of gates 7.0, 8.0 and 9.0, §10).
MANIFEST = (
    *(
        f"waveflow/linalg/{m}.py"
        for m in (
            "__init__", "formats", "lanes", "message", "build", "matmul", "systolic", "cost",
            "cg", "cg_vector", "cg_cost",
        )
    ),
    *(f"waveflow/build/{h}.h" for h in ("wf_lanes", "wf_matrix_io", "wf_linalg_msg", *_TASKS)),
    f"{PLATFORM}/platform.json",
    f"{PLATFORM}/provenance.json",
    *(f"{PLATFORM}/models/{t}/params.json" for t in _TASKS),
    f"{PLATFORM}/models/systolic_unit_channels/params.json",
    f"{PLATFORM}/models/cg_vector_unit_channels/params.json",
    f"{PLATFORM}/components/systolic_unit/params.json",
    f"{PLATFORM}/components/cg_vector_unit/params.json",
)  # fmt: skip

#: What the scan allows in code, and why.
ALLOWED = {
    "Clock(freq=250e6)": "the components' default clock (a module's clock is set where it is used)",
    'PLATFORM = "xczu48dr_250mhz_vitis2024_1"': "the packaged platform the cost models describe",
}
#: The example's constants, a part, a clock, a plan path.
FORBIDDEN = re.compile(
    r"\bINT_BITS\b|\bDEFAULT_N\b|xczu48dr|plans/|250e6|250_000_000|250 ?MHz|\bPERIOD_NS\b"
)


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


def _gained() -> list[str]:
    """The files under ``waveflow/`` this branch added, against ``main``."""
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    run = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=A",
            "main...HEAD",
            "--",
            "waveflow/",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        pytest.skip(f"no main branch to compare with: {run.stderr.strip()}")
    return sorted(run.stdout.split())


def test_waveflow_gained_only_the_manifest():
    assert _gained() == sorted(MANIFEST)


def _tokens(text: str, skip_lines: frozenset = frozenset()) -> str:
    """Python source as its tokens joined by single spaces, without comments and without the
    string tokens that start on ``skip_lines`` (the docstrings)."""
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE):
            continue
        if tok.type == tokenize.STRING and tok.start[0] in skip_lines:
            continue
        if tok.string.strip():
            out.append(tok.string)
    return " ".join(out)


def _code(path: Path) -> str:
    """The file without its comments and docstrings."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        doc = set()
        for node in ast.walk(ast.parse(text)):
            kinds = (ast.Module, ast.ClassDef, ast.FunctionDef)
            if isinstance(node, kinds) and ast.get_docstring(node):
                doc.add(node.body[0].lineno)
        return _tokens(text, frozenset(doc))
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def test_the_new_files_carry_none_of_the_examples_constants():
    bad = []
    for name in MANIFEST:
        if name.startswith(PLATFORM):
            continue  # the packaged platform's data: it names the part it was measured on
        code = _code(ROOT / name)
        for allowed in ALLOWED:
            code = code.replace(_tokens(allowed), "")
        bad += [f"{name}: {m.group(0)}" for m in FORBIDDEN.finditer(code)]
    assert not bad, bad
