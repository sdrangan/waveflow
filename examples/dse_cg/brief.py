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
        DSE.md                this arm, its links relative to out_dir
        docs/...              the architecture and module pages it links, each with its section
        examples/..., waveflow/...   the modules' Python source it links, at their repository paths

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
    re.DOTALL | re.MULTILINE,
)
_COMMENT = re.compile(r"<!--.*?-->\n?", re.DOTALL)


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


REPO = HERE.parents[1]


def _link(target: str, link_root: Path, text: str | None = None) -> str:
    """A Markdown link to repository path *target*, relative to *link_root* (where ``DSE.md`` is)."""
    import os

    rel = os.path.relpath(REPO / target, link_root).replace(os.sep, "/")
    return f"[{text or target}]({rel})"


def _table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _minutes(seconds: float | None) -> str:
    if seconds is None:
        return "to be measured"
    return f"about {seconds:.0f} s" if seconds < 90 else f"about {seconds / 60:.0f} min"


def measured_costs(design) -> dict[str, float | None]:
    """Each evaluation's typical tool time on the lab machine (``costs.py``, committed, so the
    generated ``DSE.md`` is the same on every checkout); ``None`` before it is measured."""
    from examples.dse_cg.costs import COSTS

    return dict(COSTS)


def build_blocks(design, link_root: Path, k_workers: int | None = None) -> dict[str, str]:
    """Every ``{{block}}`` of ``DSE.md.tmpl``, generated (see the table above), with links
    relative to ``link_root``."""
    from examples.dse_cg import prereg

    ctx = design.context()
    sp = design.space()
    arch = design.architecture()
    rebuilds = design.rebuilds()
    depends: dict[str, list[str]] = {}
    for prm, nodes in rebuilds.items():
        for n in nodes:
            depends.setdefault(n, []).append(f"`{prm}`")
    tree = []
    for n in arch["nodes"]:
        how = "built" if n["built"] else "modelled"
        dep = ", ".join(depends.get(n["name"], [])) or "no parameter"
        tree.append(f"- **{n['name']}** ({n['kind']}, {how}): {n['description']}; depends on {dep}.")
    ic = arch["interconnects"]
    inter = [[f"`{m}`", "crossbar master", p] for m, p in ic["crossbar_masters"]]
    inter += [[f"`{n}`", "crossbar slave", v] for n, v in ic["crossbar_slaves"]]
    inter += [[" -> ".join(f"`{n}`" for n, _p in ends), "stream", net] for net, ends in ic["streams"]]
    inter += [[f"`{n}`", "interrupt to the host", v] for n, v in ic["interrupts"]]
    params = []
    for p in sp["parameters"]:
        rb = rebuilds.get(p["name"])
        rb_txt = ("--" if p["kind"] == "workload" else
                  (", ".join(f"`{x}`" for x in rb) if rb else "none: a new RTL simulation only"))
        params.append([f"`{p['name']}`", p["kind"], ", ".join(map(str, p["values"])), p["default"],
                       rb_txt, p["description"]])
    cons = "\n".join(f"- `{c['rule']}`: {c['why']}" for c in sp["constraints"]) or "- none"
    wl = [[f"`{w['name']}`", ", ".join(f"{k} {v}" for k, v in w["fixes"].items()),
           ", ".join(f"`{m}`" for m in w["valid_for"]), ctx["workloads"].get(w["name"], "")]
          for w in sp["workloads"]]
    mets = [[f"`{m['name']}`", m["unit"], ", ".join(f"`{f}`" for f in m["from"]), m["description"]]
            for m in ctx["metrics"]]
    modelled = [[m["part"], m["pysim"], m["rtl"], m["resources"]] for m in ctx["modelled"]]
    hls = ", ".join(f"`{t}` ({n})" for n in sorted(design.BUILT_KERNELS) for t in design.BUILT_KERNELS[n])
    costs = measured_costs(design)
    docs = " ".join(_link(path, link_root, text) + ";" for text, path in ctx["docs"]).rstrip(";")
    return {
        "design_title": ctx["title"], "design_name": design.name, "platform": design.platform,
        "system_description": ctx["description"],
        "data_flow": ctx["data_flow"] + "\n\nMore: " + docs + ".",
        "subtop_tree": "\n".join(tree),
        "interconnects_table": _table(["node", "kind", "what"], inter),
        "modelled_table": _table(["part", "in pysim", "at RTL", "resources"], modelled),
        "modules_table": _modules_table(design, arch, link_root),
        "parameters_table": _table(["parameter", "kind", "values", "default", "rebuilds", "what"],
                                   params),
        "constraints": cons, "workloads_table": _table(["workload", "fixes", "metrics", "what"], wl),
        "metrics_table": _table(["metric", "unit", "from", "what"], mets), "hls_kernels": hls,
        "cost_golden": _minutes(costs["golden"]), "cost_pysim": _minutes(costs["pysim"]),
        "cost_hls_estimate": _minutes(costs["hls_estimate"]), "cost_synth": _minutes(costs["synth"]),
        "cost_rtlsim_build": _minutes(costs["rtlsim_build"]),
        "cost_rtlsim_run": _minutes(costs["rtlsim_run"]),
        "k_workers": str(k_workers or prereg.K_WORKERS),
    }


def _modules_table(design, arch, link_root: Path) -> str:
    """One row per module of each built node, from the default point's system: its class, the
    first line of its docstring, and its source."""
    import inspect

    spec = design.spec({})
    rows = []
    for k in spec.kernels:
        _mod_rows(k.owner, rows, link_root, inspect, depth=0)
    return _table(["module", "class", "what", "source"], rows) if rows else "(see the source links)"


def _summary(doc: str, width: int = 160) -> str:
    """A docstring's first paragraph as one Markdown table cell: reStructuredText's double-backtick
    literals as Markdown code spans, ``|`` escaped, cut at *width* characters without leaving a code
    span open."""
    text = re.sub(r"``(.+?)``", r"`\1`", doc.split("\n\n")[0].replace("\n", " "))
    if len(text) > width:
        text = text[:width]
        if text.count("`") % 2:
            text = text[: text.rindex("`")].rstrip()
        text += "..."
    return text.replace("|", "\\|")


def _mod_rows(comp, rows, link_root, inspect, depth) -> None:
    cls = type(comp)
    try:
        src = Path(inspect.getsourcefile(cls)).resolve().relative_to(REPO).as_posix()
        link = _link(src, link_root, src.rsplit("/", 1)[-1])
    except (TypeError, ValueError):
        link = ""
    rows.append([("&nbsp;&nbsp;" * depth) + f"`{getattr(comp, 'name', '?')}`", f"`{cls.__name__}`",
                 _summary(inspect.getdoc(cls) or ""), link])
    subs = getattr(comp, "sub_comps", None) or {}
    for sub in subs.values() if isinstance(subs, dict) else subs:
        _mod_rows(sub, rows, link_root, inspect, depth + 1)


def install_skill(skills_dir: Path, arm: str = "waveflow") -> Path:
    """Write ``skill/dse/`` for ``arm`` into ``skills_dir/dse/`` (``SKILL.md`` and ``dselib``, each
    through :func:`select_arm`, comments dropped) and return that directory."""
    out = Path(skills_dir) / "dse"
    (out / "dselib").mkdir(parents=True, exist_ok=True)
    md = _COMMENT.sub("", select_arm((SKILL / "SKILL.md").read_text(encoding="utf-8"), arm))
    (out / "SKILL.md").write_text(md, encoding="utf-8")
    lib = select_arm((SKILL / "dselib" / "__init__.py").read_text(encoding="utf-8"), arm)
    (out / "dselib" / "__init__.py").write_text(lib, encoding="utf-8")
    return out


def write_context(design, path: Path, arm: str = "waveflow") -> Path:
    """Render ``DSE.md.tmpl`` for ``design`` and ``arm`` to ``path``, linking into the repository."""
    path = Path(path)
    blocks = build_blocks(design, path.resolve().parent)
    text = render(CONTEXT_TEMPLATE.read_text(encoding="utf-8"), blocks, arm)
    path.write_text(text, encoding="utf-8")
    return path


def build_sandbox(design, arm: str, out_dir: Path) -> Path:
    """Write one run's sandbox (the tree above) and return ``out_dir``.  The objective is not in
    it: it is the prompt.

    ``DSE.md`` is rendered with its links relative to the repository's root and every file it links
    is copied to the same path under ``out_dir`` -- a documentation page with its whole section, so
    the page's own links still resolve; a source file alone -- so the agent reads what a user would,
    and nothing else of the repository is there.  Both arms get the same copies; they differ only in
    ``DSE.md`` and the skill, by the ``arm:waveflow`` blocks."""
    import shutil

    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out}: a sandbox goes into an empty directory")
    out.mkdir(parents=True, exist_ok=True)
    install_skill(out / ".claude" / "skills", arm)
    text = render(CONTEXT_TEMPLATE.read_text(encoding="utf-8"), build_blocks(design, REPO), arm)
    (out / "DSE.md").write_text(text, encoding="utf-8")
    for target in sorted(set(re.findall(r"\]\(([^)#\s]+)", text))):
        if "://" in target:
            continue
        src = (REPO / target).resolve()
        if REPO.resolve() not in src.parents:
            raise ValueError(f"DSE.md links outside the repository: {target}")
        if src.suffix == ".md" and src.parent.name and "docs" in src.relative_to(REPO.resolve()).parts:
            sec = src.parent
            shutil.copytree(sec, out / sec.relative_to(REPO.resolve()), dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif src.is_file():
            dst = out / src.relative_to(REPO.resolve())
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            raise FileNotFoundError(f"DSE.md links {target}, which is not in the repository")
    return out


def prompt(context_path: str, objective: dict) -> str:
    """The prompt a run sends, and a user would type: ``/dse <context_path> <objective text>``."""
    return f"/dse {context_path} {objective['text']}"
