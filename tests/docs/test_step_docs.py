"""Every public build step is documented: its class name appears somewhere under ``docs/``.

``plans/ip_packaging_routes.md`` (Docs 0).  An audit found five steps that no page named
(``SvSimStep``, ``GenWrapperStep``, ``MemLockStep``, ``RfSampBufStep``, ``RfTxStreamStep``); this
keeps the next one from joining them.  The system DAG's inner steps are the exception: a user reaches
them through ``add_system_steps`` and meets them by their **DAG names** (``csynth``, ``system_rtl``,
...), which is how ``docs/guide/build/xsi_system.md`` documents them, so for those the DAG name must
appear there instead.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: ``class -> DAG name`` for the steps ``add_system_steps`` builds and ``xsi_system.md`` documents by
#: name.  The resource steps of the same module are not here: ``resource/systems.md`` names their
#: classes.
SYSTEM_DAG_NAMES = {
    "CsynthTopsStep": "csynth", "CsynthStep": "csynth", "ScenarioStep": "scenario",
    "PysimStep": "pysim", "CompareStep": "compare", "SystemRtlStep": "system_rtl",
    "XbarIpStep": "xbar_ip", "SystemTopStep": "system_top", "SystemXsiStep": "system_xsi",
    "HarnessStep": "harness", "XsiRunStep": "xsi_run",
}


def _step_classes() -> dict[str, Path]:
    """Every public class named ``*Step`` defined at module level under ``waveflow/``."""
    out: dict[str, Path] = {}
    for path in sorted((ROOT / "waveflow").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.endswith("Step") \
                    and not node.name.startswith("_"):
                out[node.name] = path.relative_to(ROOT)
    return out


def _docs_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "docs").rglob("*.md")))


def test_the_step_census_finds_the_steps():
    steps = _step_classes()
    assert len(steps) > 40
    assert {"SvSimStep", "GenWrapperStep", "SystemSynthStep", "MemLockStep"} <= set(steps)
    assert set(SYSTEM_DAG_NAMES) <= set(steps), "a system-DAG step was renamed or removed"


def test_every_public_step_is_named_in_the_docs():
    docs = _docs_text()
    missing = [f"{name} ({path})" for name, path in _step_classes().items()
               if name not in SYSTEM_DAG_NAMES and not re.search(rf"\b{name}\b", docs)]
    assert not missing, ("build steps no page under docs/ names -- document each where its users "
                         "will look (see docs/guide/build/index.md):\n  " + "\n  ".join(missing))


def test_the_system_dag_steps_are_documented_by_their_dag_names():
    page = (ROOT / "docs/guide/build/xsi_system.md").read_text(encoding="utf-8")
    missing = sorted({n for n in SYSTEM_DAG_NAMES.values() if f"`{n}`" not in page
                      and not re.search(rf"^#+ {n}\b", page, re.M)})
    assert not missing, f"xsi_system.md does not name these system-DAG steps: {missing}"
