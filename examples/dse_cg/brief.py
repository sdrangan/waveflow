"""brief.py -- render the dse skill and a design's DSE.md, for a user or for a run's sandbox.

The agent's instructions come in three layers, the way a user of the skill would have them:

=========  ============================================  ==========================================
layer      what                                          from
=========  ============================================  ==========================================
skill      how to explore a design space with dselib     ``skill/dse/`` (``SKILL.md`` + ``dselib``)
context    one design: system, parameters, workloads,    ``DSE.md.tmpl``, filled by this module
           metrics, costs
prompt     one task: the objective                       the user; ``prereg.py`` for a run
=========  ============================================  ==========================================

**The user flow.**  :func:`install_skill` puts the Waveflow arm of the skill in a project's
``.claude/skills/dse/``; :func:`write_context` writes ``examples/dse_cg/DSE.md`` with links into the
repository.  With the service running (``DSE_SERVICE``), the user types::

    /dse examples/dse_cg/DSE.md Minimize LUTs subject to median loss <= 0.1 dB at SINR targets ...

**A run.**  :func:`build_sandbox` writes the same layers for one arm into a fresh directory, with
nothing else in it (no ``CLAUDE.md``, no repository)::

    out_dir/
        .claude/skills/dse/   SKILL.md + dselib/, this arm
        DSE.md                this arm, links into docs/ and src/
        docs/                 the architecture and module pages, copied
        src/                  the modules' Python and HLS source, copied

and ``run_agent.py`` sends the same prompt a user would, ``/dse DSE.md <objective>``.  A run is the
user flow in a new session with no context of its own.

Both arms come from the same files and the same generator; the arm flag only removes the
``arm:waveflow`` blocks.  So the two sandboxes differ in ``pysim`` and nothing else, and a test
checks exactly that (plan ``plans/dse_cg.md``, *Fairness rules* 1).

Where each ``{{block}}`` of ``DSE.md.tmpl`` comes from:

=======================  ======================================================================
block                    source
=======================  ======================================================================
design_title, _name,     ``design.context()``, ``design.name``, ``design.platform``
platform
system_description       ``design.context()["description"]`` (hand-written)
data_flow                ``design.context()["data_flow"]`` (hand-written), plus links to its doc pages
subtop_tree              ``design.architecture()``: the tree of nodes (sub-tops and the crossbar),
                         each with its kind, what it holds, and the design parameters it depends on
                         (``design.rebuilds()``, inverted)
interconnects_table      ``design.architecture()``: crossbar slots, stream nets between sub-tops,
                         interrupt lines, what crosses the DUT boundary
modelled_table           ``design.context()['modelled']`` (hand-written): each modelled part, what
                         its model is in pysim and at RTL, and how its resources are counted
modules_table            the same walk, one row per module, grouped by node: class, docstring;
                         source via ``inspect``; docs via the front-matter ``api:`` scan of
                         ``docs/guide``
parameters_table         ``design.space()``, plus ``design.rebuilds()``: for each design parameter,
                         the nodes whose identity changes when it changes, or "none: a new RTL
                         simulation only"
constraints              ``design.space()['constraints']``, checked at the defaults
workloads_table          ``design.space()['workloads']``
metrics_table            ``design.context()['metrics']``: name, unit, which functions give it
hls_kernels              the HLS tops of ``design.architecture()``, each with the part that holds it
cost_*, k_workers        the recorded 2025.1 costs stored with the replay table; ``prereg.py``
=======================  ======================================================================

Links in the modules and architecture blocks are relative to where ``DSE.md`` is written: into the
repository for the user flow, into the copied ``docs/`` and ``src/`` for a sandbox.  That is the
only difference between the two renderings of one arm.

Skeleton: :func:`select_arm` and :func:`render` are written, because they define what "the arms
differ only in pysim" means; the rest is plan Phase 3 (``build_sandbox``: step 5).
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE / "skill" / "dse"
CONTEXT_TEMPLATE = HERE / "DSE.md.tmpl"

#: An arm block: in Markdown ``<!-- arm:waveflow -->`` ... ``<!-- /arm -->``; in Python
#: ``# arm:waveflow`` ... ``# /arm``, each marker on its own line.
_ARM_BLOCK = re.compile(
    r"^[ \t]*(?:<!--|#)[ \t]*arm:(?P<arm>\w+)[ \t]*(?:-->)?[ \t]*\n"
    r"(?P<body>.*?)"
    r"^[ \t]*(?:<!--|#)[ \t]*/arm[ \t]*(?:-->)?[ \t]*\n",
    re.S | re.M,
)
_COMMENT = re.compile(r"<!--.*?-->\n?", re.S)


def select_arm(text: str, arm: str) -> str:
    """Keep the blocks marked for ``arm`` (without their markers) and drop the others."""
    return _ARM_BLOCK.sub(lambda m: m["body"] if m["arm"] == arm else "", text)


def render(template: str, blocks: dict[str, str], arm: str) -> str:
    """Select the arm, drop the template's own comments, fill ``{{name}}`` blocks.

    An unfilled ``{{name}}`` is an error, not an empty string: a brief with a hole in it would be
    a silent difference between runs.
    """
    text = _COMMENT.sub("", select_arm(template, arm))
    missing = sorted(set(re.findall(r"\{\{(\w+)\}\}", text)) - blocks.keys())
    if missing:
        raise KeyError(f"unfilled blocks: {missing}")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: blocks[m[1]], text)


def build_blocks(design, link_root: Path) -> dict[str, str]:
    """Every ``{{block}}`` of ``DSE.md.tmpl``, generated (see the table above), with links
    relative to ``link_root``."""
    raise NotImplementedError("Phase 3")


def install_skill(skills_dir: Path, arm: str = "waveflow") -> Path:
    """Write ``skill/dse/`` for ``arm`` into ``skills_dir/dse/`` (``SKILL.md`` and ``dselib``, each
    through :func:`select_arm`, comments dropped) and return that directory."""
    raise NotImplementedError("Phase 3")


def write_context(design, path: Path, arm: str = "waveflow") -> Path:
    """Render ``DSE.md.tmpl`` for ``design`` and ``arm`` to ``path``, linking into the repository."""
    raise NotImplementedError("Phase 3")


def build_sandbox(design, arm: str, out_dir: Path) -> Path:
    """Write one run's sandbox (the tree above) and return ``out_dir``.  The objective is not in
    it: it is the prompt."""
    raise NotImplementedError("step 5")


def prompt(context_path: str, objective: dict) -> str:
    """The prompt a run sends, and a user would type: ``/dse <context_path> <objective text>``."""
    return f"/dse {context_path} {objective['text']}"
