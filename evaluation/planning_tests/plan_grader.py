"""plan_grader.py -- score a planning-only blind test (plans/guide_system_first.md, Stage 0b).

    python evaluation/planning_tests/plan_grader.py <trial folder> [--spec 01_qam16_llr]

Reads ``<folder>/plan.md`` (the agent's plan) and ``<folder>.blindtest/summary.md`` (the run's report)
and writes ``<folder>.grade/plan_grade.md`` beside them.  What it measures mechanically:

* **citations** -- every ``example:<name>/<file>::<symbol>``, ``docs/<path>.md#<anchor>`` and
  ``waveflow.<module>.<symbol>`` in the plan, resolved against this checkout: the file exists, the symbol
  occurs in it, the anchor is a heading of the page;
* **probes** -- which of a fixed set of names the plan uses (the frame and system form it chose, the
  Waveflow constructs a reference plan expects), as found / not found;
* **reading** -- the Waveflow tool calls from the summary: the frame and examples fetched, the doc pages
  read, the first write;
* **cost** -- turns, wall time, tokens.

What it does not do is judge the plan: the reference plan in ``answers/`` is read by a person, who adds a
rating.  The numbers here are what Stages 1 and 5 compare.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EXAMPLE_CITE = re.compile(r"example:([\w-]+)/([\w./-]+?)(?:::([\w.]+))?(?=[`\s,;)\]]|$)")
DOC_CITE = re.compile(r"(docs/[\w./-]+?\.md)(?:#([\w-]+))?(?=[`\s,;)\]]|$)")
FW_CITE = re.compile(r"(?<![\w/.])(waveflow(?:\.\w+)+)(?=[`\s,;)\]]|$)")

#: Names a plan may use, grouped by what they tell.  Found / not found, nothing more.
PROBES = {
    "frame / reference": ["stream_kernel", "stream_inband", "freerun_pipeline", "bus_system",
                          "interleaver", "memcpy", "markov", "ssr_fft"],
    "system form": ["harness(", "HwSystem(", "GraphSystem", "SeqTB", "add_system_steps", "add_calib_steps"],
    "module kind": ["FreeRunMod", "HostActivated", "KernelTask", "kernel_task", "VitisRegMap"],
    "schemas": ["DataList", "DataUnion", "FixedField", "ComplexField", "EnumField", "DataArray",
                "ArrayUtilsStep", "DataSchemaStep"],
    "protocol": ["m_status", "halted", "TLAST_EARLY", "self.event()", "Resp("],
    "timing": ["TimedPhase", "timed phase", "pipeline(", "loop_optimization"],
}


def _example_dirs() -> dict[str, Path]:
    """Curated example name -> its directory, from each page's ``example_dir:`` key."""
    out = {}
    for idx in (ROOT / "docs" / "examples").glob("*/index.md"):
        m = re.search(r"^example_dir:\s*(\S+)", idx.read_text(encoding="utf-8"), re.M)
        if m:
            out[idx.parent.name] = ROOT / m.group(1)
    return out


def _slug(heading: str) -> str:
    h = re.sub(r"\{#([\w-]+)\}\s*$", "", heading).strip()
    explicit = re.search(r"\{#([\w-]+)\}", heading)
    if explicit:
        return explicit.group(1)
    h = h.lower().replace("`", "")
    h = re.sub(r"[^\w\s-]", "", h)
    return re.sub(r"\s", "-", h.strip())          # each space a hyphen, as kramdown does


def _anchors(page: Path) -> set[str]:
    out = set()
    for line in page.read_text(encoding="utf-8").splitlines():
        m = re.match(r"#+\s+(.*)", line)
        if m:
            out.add(_slug(m.group(1)))
            explicit = re.search(r"\{#([\w-]+)\}", m.group(1))
            if explicit:
                out.add(explicit.group(1))
    return out


def check_citations(plan: str) -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []
    seen = set()
    examples = _example_dirs()
    for m in EXAMPLE_CITE.finditer(plan):
        name, file, sym = m.groups()
        key = m.group(0)
        if key in seen:
            continue
        seen.add(key)
        d = examples.get(name)
        if d is None:
            rows.append((key, False, f"no curated example '{name}'"))
            continue
        f = d / file
        if not f.is_file() and (ROOT / file).is_file():     # the repo-relative form the tool also takes
            f = ROOT / file
        if not f.is_file():
            rows.append((key, False, f"no file {file} in {d.relative_to(ROOT)}"))
            continue
        ok = sym is None or sym.split(".")[-1] in f.read_text(encoding="utf-8", errors="replace")
        rows.append((key, ok, "" if ok else f"'{sym}' not in {file}"))
    for m in DOC_CITE.finditer(plan):
        path, anchor = m.groups()
        key = m.group(0)
        if key in seen:
            continue
        seen.add(key)
        page = ROOT / path
        if not page.is_file():
            rows.append((key, False, "no such page"))
            continue
        ok = anchor is None or anchor in _anchors(page)
        rows.append((key, ok, "" if ok else f"no heading #{anchor}"))
    for m in FW_CITE.finditer(plan):
        dotted = m.group(1)
        if dotted in seen:
            continue
        seen.add(dotted)
        parts = dotted.split(".")
        ok, why = False, "not found"
        for cut in range(len(parts), 1, -1):
            mod = ROOT.joinpath(*parts[:cut])
            src = mod.with_suffix(".py") if mod.with_suffix(".py").is_file() else mod / "__init__.py"
            if src.is_file():
                rest = parts[cut:]
                name = re.escape(rest[0]) if rest else ""
                ok = not rest or re.search(rf"\b(def|class)\s+{name}\b|^{name}\s*[:=]"
                                           rf"|^from\s+\S+\s+import\s+.*\b{name}\b|^\s+{name},",   # re-exports
                                           src.read_text(encoding="utf-8"), re.M) is not None
                why = "" if ok else f"'{rest[0]}' not defined in {src.relative_to(ROOT)}"
                break
        rows.append((dotted, ok, why))
    return rows


def read_summary(summary: Path) -> dict:
    if not summary.is_file():
        return {}
    text = summary.read_text(encoding="utf-8")
    out: dict = {"choice": [], "docs": [], "examples": []}
    m = re.search(r"^\| 1 \| .*?\| (\d+) \| ([\d.]+ min) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|", text, re.M)
    if m:
        out.update(turns=m.group(1), wall=m.group(2), model=m.group(3).strip(),
                   tokens_in=m.group(4).strip(), tokens_out=m.group(5).strip(), outcome=m.group(6).strip())
    ch = re.search(r"## Choice\n\n(.*?)\n\n", text, re.S)
    if ch:
        out["choice"] = [ln[2:] for ln in ch.group(1).splitlines() if ln.startswith("- ")]
    out["docs"] = re.findall(r"`waveflow_get_doc` \| \{\"path\": \"([^\"]+)\"", text)
    out["examples"] = re.findall(r"`waveflow_get_example` \| (\{[^|]*\})", text)
    out["searches"] = re.findall(r"`waveflow_search` \| (\{[^|]*\})", text)
    return out


def grade(folder: Path, spec: str | None = None) -> str:
    plan_path = folder / "plan.md"
    plan = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else ""
    summ = read_summary(folder.with_name(folder.name + ".blindtest") / "summary.md")
    lines = [f"# Plan grade: {folder.name}", ""]
    if spec:
        lines.append(f"Reference plan: `evaluation/planning_tests/answers/{spec}.reference.md` (read it; rate below).")
        lines.append("")
    if not plan:
        lines += ["**No plan.md was written.**", ""]
    cites = check_citations(plan)
    good = sum(ok for _, ok, _ in cites)
    lines += ["## Citations", "", f"{good} of {len(cites)} resolve.", ""]
    bad = [(c, why) for c, ok, why in cites if not ok]
    if bad:
        lines += ["| citation | why not |", "|---|---|"] + [f"| `{c}` | {why} |" for c, why in bad] + [""]
    lines += ["## Probes", "", "| group | found | not found |", "|---|---|---|"]
    for group, names in PROBES.items():
        found = [n for n in names if n in plan]
        lines.append(f"| {group} | {', '.join(f'`{n}`' for n in found) or '--'} | "
                     f"{', '.join(f'`{n}`' for n in names if n not in found) or '--'} |")
    lines.append("")
    if summ:
        lines += ["## The run", "",
                  f"- turns {summ.get('turns')}, wall {summ.get('wall')}, tokens in {summ.get('tokens_in')}, "
                  f"out {summ.get('tokens_out')}, outcome {summ.get('outcome')}"]
        lines += [f"- {c}" for c in summ.get("choice", [])]
        lines += ["", f"### Doc pages read ({len(summ['docs'])})", ""] + [f"- `{d}`" for d in summ["docs"]]
        lines += ["", f"### Example files fetched ({len(summ['examples'])})", ""] + [f"- `{e}`" for e in summ["examples"]]
        if summ.get("searches"):
            lines += ["", "### Searches", ""] + [f"- `{s}`" for s in summ["searches"]]
        lines.append("")
    lines += ["## Rating (by hand)", "", "- would this plan build? (yes / with fixes / no):",
              "- must-haves missed:", "- notes:", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", type=Path)
    ap.add_argument("--spec")
    a = ap.parse_args()
    folder = a.folder.resolve()
    out = folder.with_name(folder.name + ".grade")
    out.mkdir(exist_ok=True)
    text = grade(folder, a.spec)
    (out / "plan_grade.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
