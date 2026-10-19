"""The simulation-speed pages quote their results files (``plans/sim_speed_cpu_linalg.md``, AC5).

Every load-bearing number on ``docs/evaluation/sim_speed/cpu.md`` and ``linalg.md`` -- and their rows
in the section's index, and the speed note of the processor model's guide -- is recomputed here from
the JSON its script wrote (``examples/cpu_sched/runtime.json``, ``tests/linalg/sim_speed_runtime.json``)
and looked up in the page: in the row of the table it belongs to, where it is in a table.  A
re-measurement that moves a number fails here, naming the page to edit.  Re-measure with the commands
the pages give, then update the pages, not this test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"
CPU_JSON = REPO / "examples" / "cpu_sched" / "runtime.json"
LINALG_JSON = REPO / "tests" / "linalg" / "sim_speed_runtime.json"
CPU_PAGE = "evaluation/sim_speed/cpu.md"
LINALG_PAGE = "evaluation/sim_speed/linalg.md"
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


def _has(where: str, text: str, *wanted: str) -> None:
    for w in wanted:
        assert w in text, f"{where}: {w!r} not found -- re-measured? update the page"


def _ratio(x: float) -> str:
    return f"{x:.0f}x" if x >= 10 else f"{x:.1f}x"


@pytest.fixture(scope="module")
def cpu() -> dict:
    return json.loads(CPU_JSON.read_text(encoding="utf-8"))["sched200_x124"]


@pytest.fixture(scope="module")
def lin() -> dict:
    data = json.loads(LINALG_JSON.read_text(encoding="utf-8"))
    return {b: data[f"{b}_centre"] for b in ("systolic", "cg")}


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
        "gem5_region": f"{gr:.0f} us",
        "gem5_region_setup": f"{g['setup_ms'] / 1e3:.2f} s",
        "gem5_wall": f"{gw / 1e3:.1f} ms",
        "gem5_wall_setup": f"{g['wall']['setup_ms'] / 1e3:.1f} s",
        "gem5_build": f"{g['build_s']:.2f} s",
        "vs_region": f"{gr / p:.0f}x",
        "vs_wall": f"{gw / p:,.0f}x",
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
    text = _text(CPU_PAGE)
    _has(CPU_PAGE, _row(CPU_PAGE, "untimed (Python"), f"**{f['untimed']}**")
    _has(CPU_PAGE, _row(CPU_PAGE, "pysim (`Processor`)"), f"**{f['pysim']}**")
    _has(
        CPU_PAGE,
        _row(CPU_PAGE, "gem5 HPI, simulating"),
        f["gem5_region"],
        f["gem5_region_setup"],
    )
    _has(CPU_PAGE, _row(CPU_PAGE, "gem5 HPI, a whole run"), f["gem5_wall"], f["gem5_wall_setup"],
         f["gem5_build"])  # fmt: skip
    _has(CPU_PAGE, _row(CPU_PAGE, "the whole example, per job"), f["system"], f["system_setup"],
         f["ops_job"])  # fmt: skip
    _has(CPU_PAGE, _row(CPU_PAGE, "`waveflow.cpu.bench`"), f["stream"], f["burst"])
    _has(CPU_PAGE, _row(CPU_PAGE, "SimPy events per operation"), f["events"], f["events_job"],
         f"{f['events_stream']}, {f['events_burst']}")  # fmt: skip
    _has(
        CPU_PAGE,
        text,
        f["ratio"],
        f["overhead"],
        f["per_event"],
        f["vs_region"],
        f["vs_wall"],
    )
    meta = cpu["meta"]
    _has(CPU_PAGE, text, f"**{meta['trace']['ops']} list operations**",
         f"{meta['trace']['work_per_op']['n_tasks']:.1f} jobs",
         f"{meta['trace']['work_per_op']['scanned_moved']:.1f} entries")  # fmt: skip


def test_the_cpu_page_quotes_gem5_s_cycles_and_the_model_s(cpu):
    g = cpu["gem5"]
    text = _text(CPU_PAGE)
    cyc, mod = g["region_cycles"], g["model_cycles"]
    assert g["counters_match"] and g["anchor"]["ok"]
    _has(CPU_PAGE, text, f"{cyc['1']:,.0f} cycles", f"charge {mod['1']:,.0f}",
         f"+{100 * (mod['1'] / cyc['1'] - 1):.1f} %", f"+{100 * (mod['4'] / cyc['4'] - 1):.1f} %",
         f"about {cyc['4'] / cpu['ops']['4']:.0f} an operation")  # fmt: skip


def test_the_cpu_page_quotes_where_the_time_goes(cpu):
    prof = {
        k: cpu["profile"][k]["shares"]
        for k in ("untimed", "pysim_trace", "pysim_system")
    }

    def pct(fid: str, area: str) -> str:
        return f"{100 * prof[fid].get(area, 0.0):.0f}%"

    rows = {
        "processor model: ready queue": "processor model (scheduling, accounting)",
        "SimPy itself": "SimPy core",
        "the software: list operations": "software bodies (list operations, driver)",
        "pricing: features": "pricing (cost models, features)",
        "bus, registers, accelerators": "bus, registers, accelerators",
    }
    for label, area in rows.items():
        cells = [
            c.strip() for c in _row(CPU_PAGE, label).strip().strip("|").split("|")
        ][1:]
        want = [pct(fid, area) if area in prof[fid] else "--"
                for fid in ("untimed", "pysim_trace", "pysim_system")]  # fmt: skip
        assert (
            cells == want
        ), f"{CPU_PAGE}: row {label!r} is {cells}, the profile gives {want}"


def test_the_cpu_page_quotes_how_it_got_here(cpu):
    for h in cpu["history"]:
        row = _row(CPU_PAGE, str(h["round"]))
        p, u, s = h["pysim_us_op"], h["untimed_us_op"], h["system_ms_job"]
        assert p["helps"], f"round {h['round']} is on the page as a win"
        _has(CPU_PAGE, row, f"{p['before']:.1f} → {p['after']:.1f}",
             f"{u['before']:.2f} → {u['after']:.2f}", f"{s['before']:.3f} → {s['after']:.3f}")  # fmt: skip
    first = cpu["history"][0]
    before = first["pysim_us_op"]["before"] / first["untimed_us_op"]["before"]
    now = cpu["pysim_trace"]["per_op_ms"] / cpu["untimed"]["per_op_ms"]
    overhead_before = first["pysim_us_op"]["before"] - first["untimed_us_op"]["before"]
    _has(CPU_PAGE, _text(CPU_PAGE), f"from {before:.1f}x to {_ratio(now)}",
         f"fell from {overhead_before:.0f} to {_cpu_figures(cpu)['overhead'].removesuffix(' us')} us",
         "{:.2f} events an operation became {:.2f}".format(*first["events_per_op"]))  # fmt: skip


# --- the linear-algebra units ------------------------------------------------------------------


def _lin_figures(e: dict) -> dict[str, str]:
    u, p = e["untimed"]["per_job_ms"], e["pysim"]["per_job_ms"]
    r = e["rtl"]
    rtl, last = r["per_job_ms"], r["to_last_reply"]["per_job_ms"]
    end = e["pysim"]["end_s"]

    def ms(v: float) -> str:
        return f"{v:.3f} ms" if v < 0.1 else f"{v:.2f} ms" if v < 10 else (
            f"{v:.1f} ms" if v < 100 else f"{v:.0f} ms")  # fmt: skip

    pys_interval = (end["100"] - end["50"]) / 50 * 250e6
    return {
        "untimed": ms(u), "pysim": ms(p), "ratio": _ratio(p / u), "rtl": ms(rtl),
        "rtl_vs_pysim": f"{rtl / p:.0f}x", "last": ms(last),
        "construction": f"{e['construction']['per_job_ms']:.2f}",
        "csynth": f"{r['csynth_s']:.0f}", "build": f"{sum(r['build_s'].values()):.1f}",
        "elaborate": f"{r['build_s']['elaborate']:.1f}",
        "events": f"{e['events']['total']:.1f}",
        "per_event": f"{(p - u) / e['events']['total'] * 1e3:.0f} us",
        "overhead": f"{p - u:.2f} ms",
        "sim_job": f"{(end['100'] - end['50']) / 50 * 1e6:.2f} us",
        "pysim_interval": f"{pys_interval:,.0f}",
        "rtl_interval": f"{r['phases']['100']['job_interval_cycles']:,}",
        "interval_diff": f"{100 * (pys_interval / r['phases']['100']['job_interval_cycles'] - 1):+.1f} %",
    }  # fmt: skip


def test_the_linalg_page_quotes_its_results(lin):
    s, c = _lin_figures(lin["systolic"]), _lin_figures(lin["cg"])
    for b in lin.values():
        assert (
            b["untimed"]["bits_exact"]
            and b["pysim"]["bits_exact"]
            and b["rtl"]["bits_exact"]
        )
        assert b["rtl"]["tool"].endswith("2024.1")
    P = LINALG_PAGE
    _has(
        P,
        _row(P, "untimed (bit-exact model)"),
        f"**{s['untimed']}**",
        f"**{c['untimed']}**",
    )
    _has(P, _row(P, "pysim"), f"**{s['pysim']}** ({s['ratio']})", f"**{c['pysim']}** ({c['ratio']})",
         f"{s['construction']} / {c['construction']} ms")  # fmt: skip
    _has(P, _row(P, "RTL under XSI"), f"**{s['rtl']}** ({s['rtl_vs_pysim']} the pysim)",
         f"**{c['rtl']}** ({c['rtl_vs_pysim']} the pysim)", f"csynth {s['csynth']} / {c['csynth']} s",
         f"{s['build']} / {c['build']} s")  # fmt: skip
    _has(P, _row(P, "simulated time a job"), f"{s['sim_job']}, {s['pysim_interval']} cycles",
         f"{c['sim_job']}, {c['pysim_interval']} cycles")  # fmt: skip
    _has(
        P,
        _row(P, "the RTL's interval"),
        f"{s['rtl_interval']} cycles",
        f"{c['rtl_interval']} cycles",
    )
    _has(P, _row(P, "SimPy events a job"), s["events"], c["events"])
    _has(P, _row(P, "RTL time pro-rated"), s["last"], c["last"])
    text = _text(P)
    _has(P, text, f"{s['elaborate']} and {c['elaborate']} s", f"**{s['per_event']} per SimPy",
         f"{s['overhead']} over {s['events']} events", f"{c['overhead']} over {c['events']}",
         f"{c['interval_diff']}", f"{lin['systolic']['untimed']['per_job_ms'] * 1e3:.0f} us of numpy")  # fmt: skip


def test_the_linalg_page_quotes_where_the_time_goes(lin):
    prof = {b: lin[b]["profile"]["pysim"]["shares"] for b in ("systolic", "cg")}
    rows = {
        "words and schemas": "words and schemas (lanes, headers, dataschema)",
        "stream and block interfaces": "stream and block interfaces",
        "SimPy itself": "SimPy core",
        "memory, memory streams": "memory, memory streams, bench framers",
        "the model arithmetic": "model arithmetic (matmul, CG, fixed point)",
    }
    rest = {b: 100.0 for b in prof}
    for label, area in rows.items():
        cells = [
            x.strip() for x in _row(LINALG_PAGE, label).strip().strip("|").split("|")
        ][1:]
        want = [f"{100 * prof[b][area]:.0f}%" for b in ("systolic", "cg")]
        assert (
            cells == want
        ), f"{LINALG_PAGE}: row {label!r} is {cells}, the profile gives {want}"
        for b in prof:
            rest[b] -= 100 * prof[b][area]
    cells = [
        x.strip()
        for x in _row(LINALG_PAGE, "timing models").strip().strip("|").split("|")
    ][1:]
    assert cells == [f"{rest[b]:.0f}%" for b in ("systolic", "cg")]
    text = _text(LINALG_PAGE)
    own = {b: 100 * (prof[b]["timing models (timed phases, cost models)"]
                     + prof[b]["unit task bodies (systolic.py, cg_vector.py)"]) for b in prof}  # fmt: skip
    arith = {
        b: 100 * prof[b]["model arithmetic (matmul, CG, fixed point)"] for b in prof
    }
    unt = {b: 100 * lin[b]["profile"]["untimed"]["shares"]["model arithmetic (matmul, CG, fixed point)"]
           for b in prof}  # fmt: skip
    _has(LINALG_PAGE, text,
         f"{own['systolic']:.0f}% of the systolic pysim and {own['cg']:.0f}% of CG's",
         f"arithmetic {arith['systolic']:.0f}% and {arith['cg']:.0f}%",
         f"{unt['systolic']:.0f}% (systolic) and {unt['cg']:.0f}% (CG)",
         f"most of the {100 * prof['systolic']['words and schemas (lanes, headers, dataschema)']:.0f}%")  # fmt: skip


# --- the section's index and the guide --------------------------------------------------------


def test_the_index_rows_quote_the_pages(cpu, lin):
    f = _cpu_figures(cpu)
    _has(INDEX, _row(INDEX, "[processor model]"), f["untimed"], f"{f['pysim']} ({f['events']} events)",
         f"{f['gem5_region']} simulating it", f"{f['gem5_wall']} a run")  # fmt: skip
    for label, b in (("[systolic unit]", "systolic"), ("[CG vector unit]", "cg")):
        g = _lin_figures(lin[b])
        _has(INDEX, _row(INDEX, label), g["untimed"], f"{g['pysim']} ({g['events']} events)", g["rtl"],
             f"{g['csynth']} s csynth", f"{g['build']} s XSI build")  # fmt: skip
    text = _text(INDEX)
    _has(INDEX, text, f"about {_cpu_figures(cpu)['per_event']}",
         f"about {_lin_figures(lin['systolic'])['per_event']}", f"{f['vs_region']} faster than gem5",
         f"{_lin_figures(lin['systolic'])['rtl_vs_pysim']} and {_lin_figures(lin['cg'])['rtl_vs_pysim']}")  # fmt: skip


def test_the_scheduling_guide_quotes_the_bench_and_the_page(cpu):
    page = "guide/cpu/scheduling.md"
    text = _text(page)
    b = cpu["bench"]
    _has(page, text, f"about {round(b['stream']['tasks_per_s'], -3):,.0f} tasks",
         f"about {round(b['burst']['tasks_per_s'], -3):,.0f} with 20,000 queued",
         f"about {cpu['pysim_trace']['per_op_ms'] * 1e3:.0f} us to simulate",
         f"{cpu['untimed']['per_op_ms'] * 1e3:.1f} us untimed",
         f"{cpu['gem5']['per_op_ms'] * 1e3:.0f} us on gem5")  # fmt: skip
