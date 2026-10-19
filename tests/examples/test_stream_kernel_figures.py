"""The polynomial's two timing figures: committed, embedded -- and, where the traced runs are on this
machine, exactly what the build renders from them, with the spans the pages state.

Rendering needs the two traced XSI runs (``xsi_work/fig_*``, build output: Vivado), so the
re-render checks skip without them.  What is always checked: every manifest figure is a committed
SVG, the pages embed it, the committed SVG carries the span lengths the timing page states, and the
bus system's traced top dumps its device sub-top.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.stream_kernel.stream_kernel import CALIB_NSAMP
from examples.stream_kernel.stream_kernel_build import MM_TOP, TOP, WORK_DIR
from examples.stream_kernel.stream_kernel_figures import (
    FIG_CMDS,
    FIG_MM_PREFIX,
    FIG_PREFIX,
    FIGURE_MANIFEST,
    fig_mm_system,
    render_mm_command,
    render_stream_command,
)

REPO = Path(__file__).resolve().parents[2]
HERE = REPO / "examples" / "stream_kernel"
STREAM_VCD = HERE / WORK_DIR / f"{FIG_PREFIX}{TOP}" / f"{TOP}_trace.vcd"
MM_VCD = HERE / WORK_DIR / f"{FIG_MM_PREFIX}{MM_TOP}" / f"{MM_TOP}_trace.vcd"
NSAMP = FIG_CMDS[0].nsamp
#: The kernel's fitted spans (docs/examples/stream_kernel/timing.md, Vitis 2025.1) at NSAMP.
STREAM_SPANS = {"latency": 29, "fill_time": NSAMP - 1, "status_time": 1}
#: In the bus system the samples arrive after the kernel's loop is ready: latency is the pipeline's
#: own 24, not the harness's 29 (composition.md, "One command on the bus").
MM_SPANS = {"latency": 24, "fill_time": NSAMP - 1, "status_time": 1, "command": 70}
PAGES = {"stream_command": "docs/examples/stream_kernel/timing.md",
         "mm_command": "docs/examples/stream_kernel_mm/composition.md"}


def test_the_figure_command_is_a_calibrated_length():
    assert NSAMP == CALIB_NSAMP[0] == 8


def test_every_manifest_figure_is_committed_and_embedded():
    for e in FIGURE_MANIFEST:
        dst = REPO / e["dest"]
        assert dst.stat().st_size > 0, f"{e['dest']} missing: --through sync_docs_figures"
        page = (REPO / PAGES[e["name"]]).read_text(encoding="utf-8")
        assert f"images/{dst.name}" in page, f"{PAGES[e['name']]} does not embed {dst.name}"


@pytest.mark.parametrize("name, spans", [("stream_command", STREAM_SPANS), ("mm_command", MM_SPANS)])
def test_the_committed_figure_states_the_spans(name, spans):
    """matplotlib writes each text as an SVG comment beside its glyphs: the labels are checkable."""
    dest = next(e["dest"] for e in FIGURE_MANIFEST if e["name"] == name)
    svg = (REPO / dest).read_text(encoding="utf-8")
    for span, n in spans.items():
        assert f"<!-- {span} = {n} -->" in svg, f"{dest}: no '{span} = {n}'"
    assert "<dc:date>" not in svg                               # deterministic: no timestamp


def test_sync_status_records_every_figure():
    """The provenance records are build output (the repo ignores *.json): checked when present."""
    for page_dir in {Path(e["dest"]).parent for e in FIGURE_MANIFEST}:
        path = REPO / page_dir / "sync_status.json"
        if not path.exists():
            pytest.skip("no local sync_status.json (a build output, not a committed asset)")
        for f in json.loads(path.read_text(encoding="utf-8"))["figures"]:
            assert Path(f["dest"]).parent == page_dir and len(f["source_sha256"]) == 64


def test_the_bus_trace_dumps_the_device_sub_top():
    """trace_bus: the device sub-top's nets (its bus groups, interrupts, the kernel's streams), not
    the kernel's own scope -- xsim dumps only the outer of two nested scopes."""
    from waveflow.build.system_top import SystemTopSpec

    spec = SystemTopSpec.from_system(fig_mm_system(), top=MM_TOP)
    assert spec.trace_scopes() == [f"{MM_TOP}.u_poly_kernel_sub.u_poly_kernel"]
    assert spec.trace_scopes(bus=True) == [f"{MM_TOP}.u_poly_kernel_sub"]


@pytest.mark.parametrize("name, render, vcd, spans", [
    ("stream_command", render_stream_command, STREAM_VCD, STREAM_SPANS),
    ("mm_command", render_mm_command, MM_VCD, MM_SPANS)])
def test_the_committed_figure_is_what_the_trace_renders(tmp_path, name, render, vcd, spans):
    if not vcd.is_file():
        pytest.skip(f"no traced run at {vcd} (--through sync_docs_figures, Vivado)")
    out = tmp_path / f"{name}.svg"
    data = render(out, vcd)
    assert {k: b - a for k, (a, b) in data["spans"].items()} == spans
    dest = next(e["dest"] for e in FIGURE_MANIFEST if e["name"] == name)
    assert out.read_bytes() == (REPO / dest).read_bytes(), (
        f"{dest} is stale: python -m examples.stream_kernel.stream_kernel_build "
        f"--through sync_docs_figures")
