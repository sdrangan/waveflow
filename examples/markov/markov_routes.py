"""markov_routes.py — markov synthesized both ways, measured: the numbers and figure of the docs'
route comparison (``docs/examples/markov/synth.md``, ``plans/ip_packaging_routes.md`` Stage 6).

* **global** -- the system top in one Vivado run, then ``opt_design``
  (:func:`~waveflow.build.system_synth.synth_system` with ``opt``);
* **per device** -- each device and the memory out of context, the top (crossbar and glue) against
  black boxes, the netlists linked, then ``opt_design``
  (:func:`~waveflow.build.system_ooc.synth_ooc_link`).

Both are cached by content, so a re-run synthesizes nothing (the link, seconds, re-runs).  Every
number is read from the two runs' results -- counters from their utilization reports, seconds from
the runs' own timers -- never typed in.  ``MarkovRoutesStep`` writes ``results/routes.json`` and
renders ``results/routes.svg``; ``SyncRoutesFigureStep`` promotes both beside the docs, where
``tests/docs/test_markov_routes_page.py`` holds the page's table to the committed record and
``tests/examples/test_markov_routes.py`` holds the record's area to a fresh run.  Needs Vivado, so
opt-in:

    python -m examples.markov.markov_build --through sync_routes_figure
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from matplotlib.figure import Figure

from waveflow.build.build import BuildConfig, BuildStep

COUNTERS = ("lut", "ff", "bram")
#: The device sub-tops, in the order the page lists them.
PARTS = ("gen", "chain", "mem")
RESULTS = ("results/routes.json", "results/routes.svg")
DOCS = ("docs/examples/markov/images/routes.json", "docs/examples/markov/images/routes.svg")
_SALT = {"svg.hashsalt": "markov_routes"}


def route_numbers(glob, link) -> dict:
    """The comparison, from a global :class:`~waveflow.build.system_synth.SystemSynth` (run with
    ``opt``) and a per-device :class:`~waveflow.build.system_ooc.OocLink` of the same system."""
    def pick(c: dict) -> dict:
        return {k: int(c.get(k, 0)) for k in COUNTERS}

    g = pick(glob.opt_total)
    linked = pick(link.linked)
    seconds_global = round(glob.seconds + glob.opt_seconds, 1)
    seconds_parts = {p: round(link.part_seconds[p], 1) for p in PARTS}
    return {
        "part": glob.part, "period_ns": glob.period_ns, "tool": glob.tool,
        "global": {"synth": pick(glob.total), "opt": g},
        "per_device": {"parts": {p: pick(link.parts[p]) for p in PARTS},
                       "top": pick(link.top_counters), "link_term": pick(link.link_term),
                       "linked": linked},
        "gap": {k: linked[k] - g[k] for k in COUNTERS},
        "gap_pct": {k: round(100.0 * (linked[k] - g[k]) / g[k], 2) if g[k] else 0.0
                    for k in COUNTERS},
        # Vivado's own run timers (synth_design, opt_design, the link): start-up excluded.
        "seconds": {
            "global": seconds_global,
            "parts": seconds_parts,
            "top": round(link.top_seconds, 1),
            "link": round(link.link_seconds, 1),
            "per_device_cold": round(sum(seconds_parts.values()) + link.top_seconds
                                     + link.link_seconds, 1),
            "per_device_one_changed": {p: round(seconds_parts[p] + link.link_seconds, 1)
                                       for p in PARTS},
            "per_device_unchanged": round(link.link_seconds, 1),
        },
    }


def render_routes(nums: dict, path: Path) -> None:
    """Two panels: LUT by part for each route (the per-device link term below zero), and the
    Vivado seconds of each rebuild."""
    from matplotlib.backends.backend_svg import FigureCanvasSVG

    fig = Figure(figsize=(9.0, 3.6))
    FigureCanvasSVG(fig)
    ax, bx = fig.subplots(1, 2, gridspec_kw={"width_ratios": [1.0, 1.3]})
    colors = {"gen": "#4C78A8", "chain": "#F58518", "mem": "#54A24B", "top": "#B279A2",
              "link": "#9D9D9D"}
    pd = nums["per_device"]
    bottom = 0
    for name in PARTS:
        v = pd["parts"][name]["lut"]
        ax.bar(1, v, bottom=bottom, color=colors[name], label=f"{name} device" if name != "mem"
               else "memory")
        bottom += v
    ax.bar(1, pd["top"]["lut"], bottom=bottom, color=colors["top"], label="crossbar + glue")
    ax.bar(1, pd["link_term"]["lut"], color=colors["link"], label="link term")
    ax.bar(0, nums["global"]["opt"]["lut"], color="#555555")
    ax.axhline(0, color="black", lw=0.6)
    ax.set_xticks([0, 1], ["global", "per device"])
    ax.set_ylabel("LUT (after opt_design)")
    ax.set_title(f"area: {pd['linked']['lut']:,} vs {nums['global']['opt']['lut']:,} LUT "
                 f"({nums['gap_pct']['lut']:+.2f}%)", fontsize=10)
    ax.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3,
              frameon=False)

    s = nums["seconds"]
    worst = max(s["per_device_one_changed"].values())
    rows = [("global, any change", s["global"]), ("per device, cold", s["per_device_cold"]),
            ("per device, one device\nchanged (slowest)", worst),
            ("per device, unchanged", s["per_device_unchanged"])]
    bx.barh(range(len(rows)), [v for _n, v in rows],
            color=["#555555", "#4C78A8", "#4C78A8", "#4C78A8"])
    bx.set_yticks(range(len(rows)), [n for n, _v in rows], fontsize=8)
    bx.invert_yaxis()
    for i, (_n, v) in enumerate(rows):
        bx.text(v, i, f" {v:.0f} s", va="center", fontsize=8)
    bx.set_xlabel("Vivado run seconds (start-up excluded)")
    bx.set_title("rebuild time", fontsize=10)
    bx.set_xlim(0, 1.2 * max(v for _n, v in rows))
    fig.suptitle(f"markov, {nums['part']}, {nums['period_ns']:g} ns", fontsize=10)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    import matplotlib

    with matplotlib.rc_context(_SALT):
        fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})


@dataclass(kw_only=True)
class MarkovRoutesStep(BuildStep):
    """Synthesize markov both ways (each cached) and record the comparison: ``results/routes.json``
    and ``results/routes.svg``.  Opt-in: minutes of Vivado cold, seconds after."""

    description: str = "Synthesize markov globally and per device; record and plot the comparison."
    params: ClassVar[dict] = {}
    rtl: Any = None                  # the system_rtl step (its spec, work and crossbar IP)

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return ["rtl"]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"routes_json": Path(RESULTS[0]), "routes_svg": Path(RESULTS[1])}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False                 # both runs are cached by content: a re-run is cheap

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        import dataclasses

        from waveflow.build.axi_xbar import generate_axi_xbar
        from waveflow.build.composite_gen import tcl_target
        from waveflow.build.system_dag import rtl_rel
        from waveflow.build.system_ooc import synth_ooc_link
        from waveflow.build.system_synth import synth_system

        root = Path(config.root_dir)
        spec, work = self.rtl.spec, self.rtl.work_dir(config)
        part, period = tcl_target(config)
        ip = generate_axi_xbar(dataclasses.replace(spec.xbar, part=part), self.rtl.ip_dir(config))
        kdirs = {m: root / rtl_rel(m) for m in spec.modules}
        glob = synth_system(spec, top_v=work / f"{spec.top}.v", kernel_dirs=kdirs, ip=ip,
                            out_dir=work.parent / "system_synth", part=part, period_ns=period,
                            opt=True)
        link = synth_ooc_link(spec, kernel_dirs=kdirs, ip=ip, part=part, period_ns=period,
                              out_dir=work.parent / "ooc_link")
        nums = route_numbers(glob, link)
        js, svg = root / RESULTS[0], root / RESULTS[1]
        js.parent.mkdir(parents=True, exist_ok=True)
        js.write_text(json.dumps(nums, indent=2) + "\n", encoding="utf-8")
        render_routes(nums, svg)
        print(f"  markov routes: global {nums['global']['opt']['lut']} LUT, per device "
              f"{nums['per_device']['linked']['lut']} LUT ({nums['gap_pct']['lut']:+.2f}%)")
        return {"routes_json": js, "routes_svg": svg}


@dataclass(kw_only=True)
class SyncRoutesFigureStep(BuildStep):
    """Promote ``routes.json`` and ``routes.svg`` beside the docs, with their digests."""

    description: str = "Copy the routes record and figure into docs/examples/markov/images."
    params: ClassVar[dict] = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return ["routes_json", "routes_svg"]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"routes_sync": Path("docs/examples/markov/images/routes_sync.json")}

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        repo = root.parents[1]
        records = []
        for src_rel, dst_rel in zip(RESULTS, DOCS):
            src, dst = root / src_rel, repo / dst_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            records.append({"source": src_rel, "dest": dst_rel,
                            "sha256": hashlib.sha256(src.read_bytes()).hexdigest()})
        out = repo / "docs" / "examples" / "markov" / "images" / "routes_sync.json"
        out.write_text(json.dumps({"files": records}, indent=2) + "\n", encoding="utf-8")
        return {"routes_sync": out}
