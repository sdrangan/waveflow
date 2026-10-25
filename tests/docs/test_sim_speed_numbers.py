"""The processor model's simulation-speed page quotes its results file (AC5 of
``plans/sim_speed_cpu_linalg.md``, its CPU half: the linear-algebra units' page is not on this branch).

Every load-bearing number on ``docs/evaluation/sim_speed/cpu.md`` -- and its row in the section's
index, the speed note of the processor model's guide, and the replay figure of the ``cpu_sched``
example's page -- is recomputed here from the JSON its script wrote
(``examples/cpu_sched/runtime.json``) and looked up in the page: in the row of the table it belongs
to, where it is in a table.  The results themselves must be
what the pages claim they are: every run exact, measured on committed code.  A re-measurement that
moves a number fails here, naming the page to edit.  Re-measure with the commands the pages give,
then update the pages, not this test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"
CPU_JSON = REPO / "examples" / "cpu_sched" / "runtime.json"
CPU_PAGE = "evaluation/sim_speed/cpu.md"
INDEX = "evaluation/sim_speed/index.md"


def _text(rel: str) -> str:
    """The page with its whitespace collapsed, so a number split across lines is still found."""
    return " ".join((DOCS / rel).read_text(encoding="utf-8").split())


def _row(rel: str, label: str) -> str:
    """The first table row of *rel* whose first cell starts with *label*."""
    for line in (DOCS / rel).read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.lstrip().startswith("|") and cells and cells[0].startswith(label):
            return line
    raise AssertionError(f"{rel}: no table row starting with {label!r}")


def _cells(rel: str, label: str) -> list[str]:
    return [c.strip() for c in _row(rel, label).strip().strip("|").split("|")][1:]


def _has(where: str, text: str, *wanted: str) -> None:
    for w in wanted:
        assert w in text, f"{where}: {w!r} not found -- re-measured? update the page"


def _ratio(x: float) -> str:
    return f"{x:.0f}x" if x >= 10 else f"{x:.1f}x"


@pytest.fixture(scope="module")
def cpu() -> dict:
    return json.loads(CPU_JSON.read_text(encoding="utf-8"))["sched200_x124"]


def _stamped(entry: dict) -> dict[str, dict]:
    return {k: v for k, v in entry.items() if isinstance(v, dict) and "commit" in v}


def test_the_results_are_exact_and_from_committed_code(cpu):
    """A page may quote only runs that checked out, measured on a clean tree."""
    assert cpu["untimed"]["exact"] and cpu["pysim_trace"]["exact"]
    assert cpu["pysim_system"]["all_correct"]
    assert cpu["gem5"]["counters_match"] and cpu["gem5"]["anchor"]["ok"]
    dirty = {k for k, v in _stamped(cpu).items() if v.get("dirty")}
    assert not dirty, f"measured on a dirty tree: {sorted(dirty)}"


# --- the processor model -------------------------------------------------------------------------


def _cpu_figures(c: dict) -> dict[str, str]:
    u = c["untimed"]["per_op_ms"] * 1e3
    p = c["pysim_trace"]["per_op_ms"] * 1e3
    g = c["gem5"]
    gr, gw = g["per_op_ms"] * 1e3, g["wall"]["per_op_ms"] * 1e3
    ev = c["events"]["per_op"]["total"]
    return {
        "untimed": f"{u:.2f} us",
        "pysim": f"{p:.1f} us",
        "ratio": _ratio(p / u),
        "overhead": f"{p - u:.1f} us",
        "per_event": f"{(p - u) / ev:.1f} us",
        "events": f"{ev:.2f}",
        "cold_untimed": f"{c['cold']['untimed_us_op']:.2f} us",
        "cold_pysim": f"{c['cold']['pysim_us_op']:.1f} us",
        "gem5_region": f"{gr:.0f} us",
        "gem5_region_setup": f"{g['setup_ms'] / 1e3:.2f} s",
        "gem5_wall": f"{gw / 1e3:.1f} ms",
        "gem5_wall_setup": f"{g['wall']['setup_ms'] / 1e3:.1f} s",
        "gem5_build": f"{g['build_s']:.2f} s",
        "vs_region": f"{gr / p:.0f}x",
        "vs_wall": f"{gw / p:,.0f}x",
        "wall_over_region": f"{gw / gr:.0f} times",
        "system": f"{c['pysim_system']['per_job_ms']:.3f} ms",
        "system_setup": f"{c['pysim_system']['setup_ms']:.1f} ms",
        "ops_job": f"{c['pysim_system']['ops_per_job']['200']:.2f}",
        "stream": f"{c['bench']['stream']['tasks_per_s']:,.0f}",
        "burst": f"{c['bench']['burst']['tasks_per_s']:,.0f}",
        "events_job": f"{c['events']['per_job']['total']:.1f}",
        "events_stream": f"{c['events']['per_task_stream']['total']:.1f}",
        "events_burst": f"{c['events']['per_task_burst']['total']:.1f}",
    }


def test_the_cpu_page_quotes_its_results(cpu):
    f = _cpu_figures(cpu)
    P = CPU_PAGE
    _has(P, _row(P, "untimed (Python"), f"**{f['untimed']}**")
    _has(P, _row(P, "pysim (`Processor`)"), f"**{f['pysim']}**")
    _has(P, _row(P, "untimed, the first pass"), f["cold_untimed"])
    _has(P, _row(P, "pysim, the first pass"), f["cold_pysim"])
    _has(P, _row(P, "gem5 HPI, simulating"), f["gem5_region"], f["gem5_region_setup"])
    _has(
        P,
        _row(P, "gem5 HPI, a whole run"),
        f["gem5_wall"],
        f["gem5_wall_setup"],
        f["gem5_build"],
    )
    _has(
        P,
        _row(P, "the whole example, per job"),
        f["system"],
        f["system_setup"],
        f["ops_job"],
    )
    _has(P, _row(P, "`waveflow.cpu.bench`"), f["stream"], f["burst"])
    _has(P, _row(P, "SimPy events per operation"), f["events"], f["events_job"],
         f"{f['events_stream']}, {f['events_burst']}")  # fmt: skip
    text = _text(P)
    _has(P, text, f["overhead"], f["per_event"], f"**{f['vs_region']}**", f"**{f['vs_wall']}**",
         f"costs {f['wall_over_region']} the measured pass",
         f"{cpu['cold']['points']} distinct prices for its {cpu['cold']['calls']} calls")  # fmt: skip
    meta = cpu["meta"]
    _has(P, text, f"**{meta['trace']['ops']} list operations**",
         f"{meta['trace']['work_per_op']['n_tasks']:.1f} jobs",
         f"{meta['trace']['work_per_op']['scanned_moved']:.1f} entries")  # fmt: skip


def test_the_cpu_page_quotes_gem5_s_cycles_and_the_model_s(cpu):
    g = cpu["gem5"]
    cyc, mod = g["region_cycles"], g["model_cycles"]
    _has(CPU_PAGE, _text(CPU_PAGE), f"{cyc['1']:,.0f} cycles", f"charge {mod['1']:,.0f}",
         f"+{100 * (mod['1'] / cyc['1'] - 1):.1f} %", f"+{100 * (mod['4'] / cyc['4'] - 1):.1f} %",
         f"about {cyc['4'] / cpu['ops']['4']:.0f} an operation")  # fmt: skip


def test_the_cpu_page_quotes_where_the_time_goes(cpu):
    prof = {
        k: cpu["profile"][k]["shares"]
        for k in ("untimed", "pysim_trace", "pysim_system")
    }
    rows = {
        "processor model: ready queue": "processor model (scheduling, accounting)",
        "SimPy itself": "SimPy core",
        "the software: list operations": "software bodies (list operations, driver)",
        "pricing: features": "pricing (cost models, features)",
        "bus, registers, accelerators": "bus, registers, accelerators",
    }
    for label, area in rows.items():
        want = [f"{100 * prof[fid][area]:.0f}%" if area in prof[fid] else "--"
                for fid in ("untimed", "pysim_trace", "pysim_system")]  # fmt: skip
        assert (
            _cells(CPU_PAGE, label) == want
        ), f"{CPU_PAGE}: row {label!r}, the profile gives {want}"


def test_the_cpu_page_quotes_how_it_got_here(cpu):
    for h in cpu["history"]:
        p, u, s = h["pysim_us_op"], h["untimed_us_op"], h["system_ms_job"]
        assert p["helps"], f"round {h['round']} is on the page as a win"
        _has(CPU_PAGE, _row(CPU_PAGE, str(h["round"])), f"{p['before']:.1f} → {p['after']:.1f}",
             f"{u['before']:.2f} → {u['after']:.2f}", f"{s['before']:.3f} → {s['after']:.3f}")  # fmt: skip
    first = cpu["history"][0]
    before = first["pysim_us_op"]["before"] / first["untimed_us_op"]["before"]
    now = cpu["pysim_trace"]["per_op_ms"] / cpu["untimed"]["per_op_ms"]
    overhead_before = first["pysim_us_op"]["before"] - first["untimed_us_op"]["before"]
    _has(CPU_PAGE, _text(CPU_PAGE), f"from {before:.1f}x to {_ratio(now)}",
         f"fell from {overhead_before:.0f} to {_cpu_figures(cpu)['overhead']}",
         "{:.2f} events an operation became {:.2f}".format(*first["events_per_op"]))  # fmt: skip


# --- the section's index, the guide and the example ------------------------------------------


def test_the_index_rows_quote_the_pages(cpu):
    f = _cpu_figures(cpu)
    _has(INDEX, _row(INDEX, "[processor model]"), f["untimed"], f"{f['pysim']} ({f['events']} events)",
         f"{f['gem5_region']} simulating it",
         f"{f['gem5_wall']} of a whole run, plus {f['gem5_wall_setup']} a run",
         f"cold first pass {f['cold_pysim']}")  # fmt: skip
    _has(
        INDEX,
        _text(INDEX),
        f"about {f['per_event']}",
        f"{f['vs_region']} faster than gem5",
    )


def test_the_scheduling_guide_quotes_the_bench_and_the_page(cpu):
    page = "guide/cpu/scheduling.md"
    b = cpu["bench"]
    _has(page, _text(page), f"about {round(b['stream']['tasks_per_s'], -3):,.0f} tasks",
         f"about {round(b['burst']['tasks_per_s'], -3):,.0f} with 20,000 queued",
         f"about {cpu['pysim_trace']['per_op_ms'] * 1e3:.0f} us to simulate",
         f"{cpu['untimed']['per_op_ms'] * 1e3:.1f} us untimed",
         f"{cpu['gem5']['per_op_ms'] * 1e3:.0f} us on gem5")  # fmt: skip


def test_the_example_page_quotes_the_replay_pricing(cpu):
    page = "examples/cpu_sched/index.md"
    _has(page, _text(page), f"takes about {cpu['replay_pricing']['ms']:.0f} ms")
