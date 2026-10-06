"""poly_figures.py -- the streaming polynomial's docs figures, as two build steps.

* **protocol.svg** -- the contract at a glance: commands on ``in_stream``, responses on
  ``out_stream``, and only control and status on AXI-Lite.  Drawn, not measured.
* **error_path.svg** -- a failing command on the wire, from the RTL co-simulation of the
  ``early_tlast_vcd`` scenario (``vcd/error_path.vcd``, written by the ``error_vcd`` step):
  the host's ``ap_start``, two commands in and their responses out, the second command's
  sample burst ending early, the kernel closing its output with TLAST and returning, and the
  host reading ``ap_done`` and the status.  A final panel, drawn and labelled as such, shows
  what the capture cannot: the commands still queued, the reset, and the next run.

``PolyFiguresStep`` renders into ``results/`` (gitignored) from the committed VCD, so a refresh
needs only matplotlib; ``SyncDocsFiguresStep`` promotes the SVGs into
``docs/examples/stream_inband/images/`` with a provenance record:

    python examples/stream_inband/poly_build.py --through sync_docs_figures
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import matplotlib
from matplotlib.backends.backend_svg import FigureCanvasSVG
from matplotlib.figure import Figure
from matplotlib.patches import FancyArrowPatch, Rectangle

from waveflow.build.build import BuildConfig, BuildStep

# Deterministic SVG: a fixed hashsalt and no timestamp, so a re-render diffs only on a real change.
_SALT = {"svg.hashsalt": "poly_figures"}

_HERE = Path(__file__).resolve().parent
ERROR_VCD = _HERE / "vcd" / "error_path.vcd"

FIGURE_MANIFEST = [
    {"name": "protocol", "source": "results/protocol.svg",
     "dest": "docs/examples/stream_inband/images/protocol.svg"},
    {"name": "error_path", "source": "results/error_path.svg",
     "dest": "docs/examples/stream_inband/images/error_path.svg"},
]

HDR = "#F2A65A"      # headers (orange, as in timing_analysis.plot_poly_timing)
DATA = "#7FB77E"     # sample and result bursts (green)
LITE = "#8FA8C8"     # AXI-Lite
ERR = "#D1495B"      # the error
INK = "#333333"


def _block(ax, x, y, w, h, color, text, fontsize=8, **kw) -> None:
    ax.add_patch(Rectangle((x, y), w, h, facecolor=color, edgecolor=INK, lw=0.8, **kw))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fontsize, color=INK)


def _arrow(ax, x0, y0, x1, y1, **kw) -> None:
    ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), arrowstyle="-|>", mutation_scale=12,
                                 lw=1.2, color=INK, **kw))


# ---------------------------------------------------------------------------
# protocol.svg
# ---------------------------------------------------------------------------

@matplotlib.rc_context(_SALT)
def render_protocol(path: Path) -> None:
    fig = Figure(figsize=(9.6, 4.4))
    FigureCanvasSVG(fig)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 5.6)
    ax.axis("off")

    _block(ax, 0.2, 0.5, 1.6, 4.6, "#EDEDED", "Host", fontsize=11)
    _block(ax, 10.2, 0.5, 1.6, 4.6, "#EDEDED", "Kernel\n(poly)", fontsize=11)

    # in_stream: host -> kernel.  The oldest command is at the head, next to the kernel.
    y = 4.0
    ax.text(6.0, y + 0.95, "in_stream  (AXI4-Stream): commands -- everything the kernel computes with",
            ha="center", fontsize=9, color=INK)
    _arrow(ax, 1.85, y + 0.3, 10.15, y + 0.3)
    seq = [("CmdHdr\nEND", HDR, 0.9), ("samples", DATA, 1.1),
           ("CmdHdr DATA\ntx_id, nsamp,\nc0..c3", HDR, 1.5), ("samples", DATA, 1.4),
           ("CmdHdr DATA\ntx_id, nsamp,\nc0..c3", HDR, 1.5)]
    x = 2.2
    for text, color, w in seq:
        _block(ax, x, y - 0.1, w, 0.8, color, text, fontsize=7)
        x += w + 0.12
    ax.text(10.05, y - 0.35, "head (first in)", ha="right", fontsize=7, color=INK, style="italic")

    # out_stream: kernel -> host.  The oldest response is at the head, next to the host.
    y = 2.35
    ax.text(6.0, y + 0.95, "out_stream  (AXI4-Stream): one response per DATA command",
            ha="center", fontsize=9, color=INK)
    _arrow(ax, 10.15, y + 0.3, 1.85, y + 0.3)
    seq = [("RespHdr\ntx_id", HDR, 1.1), ("results", DATA, 1.6),
           ("RespHdr\ntx_id", HDR, 1.1), ("results", DATA, 1.6)]
    x = 2.2
    for text, color, w in seq:
        _block(ax, x, y - 0.1, w, 0.8, color, text, fontsize=7)
        x += w + 0.12
    ax.text(2.0, y - 0.35, "head (first out)", ha="left", fontsize=7, color=INK, style="italic")

    # AXI-Lite: control and status only.
    y = 0.75
    ax.text(6.0, y + 0.95, "AXI-Lite: control and status only -- no configuration",
            ha="center", fontsize=9, color=INK)
    _arrow(ax, 1.85, y + 0.45, 10.15, y + 0.45)
    _arrow(ax, 10.15, y + 0.05, 1.85, y + 0.05)
    _block(ax, 4.0, y + 0.25, 1.6, 0.4, LITE, "ap_start", fontsize=7)
    _block(ax, 4.0, y - 0.15, 1.6, 0.4, LITE, "ap_done", fontsize=7)
    _block(ax, 6.2, y - 0.15, 2.4, 0.4, LITE, "halted, error, tx_id", fontsize=7)
    _save_svg(fig, path)


# ---------------------------------------------------------------------------
# error_path.svg, from the co-simulation VCD
# ---------------------------------------------------------------------------

def _lite_events(vcd_path: Path) -> dict[str, Any]:
    """The AXI-Lite traffic in the capture: the write of ap_start, and each read (address, data)."""
    from vcdvcd import VCDVCD

    vcd = VCDVCD(str(vcd_path), store_tvs=True)
    tscale = 1e-3     # the capture is in ps; the figure is in ns

    def tv(suffix: str):
        name = next(k for k in vcd.signals if k.split(".")[-1].split("[")[0] == suffix)
        return vcd[name].tv

    def rising(suffix: str) -> list[float]:
        return [t * tscale for t, v in tv(suffix) if v == "1"]

    def value_at(suffix: str, t_ns: float) -> int:
        val = "0"
        for t, v in tv(suffix):
            if t * tscale > t_ns:
                break
            val = v
        return int(val, 2) if set(val) <= {"0", "1"} else 0

    reads = [(t, value_at("s_axi_control_ARADDR", t)) for t in rising("s_axi_control_ARVALID")]
    rdata = [value_at("s_axi_control_RDATA", t) for t in rising("s_axi_control_RVALID")]
    return {"start": rising("s_axi_control_AWVALID"),
            "reads": [(t, a, d) for (t, a), d in zip(reads, rdata)]}


@matplotlib.rc_context(_SALT)
def render_error_path(path: Path, vcd_path: Path = ERROR_VCD) -> None:
    from timing_analysis import analyze_poly_vcd

    res = analyze_poly_vcd(vcd_path)
    lite = _lite_events(vcd_path)
    cp = res.clk_period

    fig = Figure(figsize=(11.5, 4.6))
    FigureCanvasSVG(fig)
    ax = fig.add_axes((0.08, 0.17, 0.9, 0.7))
    t_end = max(t for t, _, _ in lite["reads"]) + 3 * cp
    t_tail = t_end + 2 * cp                     # where the drawn (not captured) panel starts
    ax.set_xlim(lite["start"][0] - 3 * cp, t_tail + 560)
    ax.set_ylim(-0.2, 3.3)
    ax.set_yticks([0.4, 1.4, 2.4])
    ax.set_yticklabels(["AXI-Lite", "out_stream\n(m_out)", "in_stream\n(s_in)"], fontsize=9)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.set_xlabel("time [ns], RTL co-simulation of early_tlast_vcd (32-bit words, 10 ns clock)",
                  fontsize=9)

    def burst(lane_y, b, color, text):
        """A burst from its first beat to its last, stalls included."""
        t0, t1 = b["tstart"], b["tstart"] + len(b["beat_type"]) * cp
        narrow = t1 - t0 < 45
        _block(ax, t0, lane_y - 0.3, t1 - t0, 0.6, color, "" if narrow else text, fontsize=7)
        if narrow:                                   # too thin to hold its label
            ax.text((t0 + t1) / 2, lane_y + 0.36, text.replace("\n", " "), ha="center",
                    va="bottom", fontsize=7, color=INK)
        return t0, t1

    # in_stream: each command's header, then its samples.
    for c in res.commands:
        tx = int(c.hdr.val["tx_id"])
        burst(2.4, c.hdr_burst, HDR, f"hdr\n{tx}")
        if c.samp_burst is not None:
            n, nsamp = len(c.x), int(c.hdr.val["nsamp"])
            short = n < nsamp
            t0, t1 = burst(2.4, c.samp_burst, ERR if short else DATA,
                           f"{n} of {nsamp}" if short else f"x[{nsamp}]")
            if short:
                ax.annotate("TLAST early\n(error 1)", xy=(t1, 2.7), xytext=(t1 + 40, 3.1),
                            fontsize=8, color=ERR, arrowprops=dict(arrowstyle="->", color=ERR))
    # out_stream: each response's header, then its results.
    for r in res.responses:
        tx = int(r.hdr.val["tx_id"])
        burst(1.4, r.hdr_burst, HDR, f"resp\n{tx}")
        if r.data_burst is not None:
            t0, t1 = burst(1.4, r.data_burst, DATA, f"y[{len(r.y)}]")
    last = res.responses[-1].data_burst
    t_close = last["tstart"] + len(last["beat_type"]) * cp
    ax.annotate("closed with TLAST\n(rule 6)", xy=(t_close, 1.1), xytext=(t_close - 120, 0.85),
                fontsize=8, color=ERR, ha="right", arrowprops=dict(arrowstyle="->", color=ERR))

    # AXI-Lite: ap_start, then the reads after the kernel returns.
    names = {0x00: "ap_ctrl", 0x10: "halted", 0x20: "error", 0x30: "tx_id"}
    t_start = lite["start"][0]
    _block(ax, t_start, 0.15, 2 * cp, 0.5, LITE, "", fontsize=7)
    ax.text(t_start + cp, -0.05, "ap_start", ha="center", va="top", fontsize=7)
    for t, _, _ in lite["reads"]:
        _block(ax, t, 0.15, 2 * cp, 0.5, LITE, "", fontsize=7)
    status = ", ".join(f"{names[a]} = {d}" for _, a, d in lite["reads"] if a in names and a)
    t_reads = lite["reads"][0][0]
    ax.text(t_reads - 15, 0.4, f"host sees ap_done, then reads\n{status}", ha="right",
            va="center", fontsize=7, color=INK)

    # After the capture: drawn, not measured.
    ax.axvline(t_tail - cp, color=INK, lw=0.8, ls=":")
    ax.text(t_tail + 270, 3.2, "after the capture (drawn, not measured)", ha="center",
            fontsize=8, style="italic", color=INK)
    _block(ax, t_tail, 2.1, 520, 0.6, "#F5F5F5",
           "commands queued behind the error\nmay still be here: contents undefined (rule 7)",
           fontsize=7, ls="--")
    _block(ax, t_tail, 0.15, 250, 0.5, "#F5F5F5", "host resets\nthe stream path", fontsize=7,
           ls="--")
    _block(ax, t_tail + 270, 0.15, 250, 0.5, "#F5F5F5", "ap_start:\na fresh run", fontsize=7,
           ls="--")
    _save_svg(fig, path)


def _save_svg(fig, path: Path) -> None:
    """Write a deterministic SVG (no embedded timestamp)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class PolyFiguresStep(BuildStep):
    """Render the docs figures into ``results/`` (the error path from the committed VCD)."""

    description: str = "Render the protocol diagram and the error-path timeline."
    params: ClassVar[dict] = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return ["poly_source"]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {f"{e['name']}_svg": Path(e["source"]) for e in FIGURE_MANIFEST}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        root = Path(config.root_dir)
        vcd = root / "vcd" / "error_path.vcd"
        if not vcd.exists():
            vcd = ERROR_VCD
        render_protocol(root / "results" / "protocol.svg")
        render_error_path(root / "results" / "error_path.svg", vcd)
        return {f"{e['name']}_svg": root / e["source"] for e in FIGURE_MANIFEST}


@dataclass(kw_only=True)
class SyncDocsFiguresStep(BuildStep):
    """Promote the rendered SVGs into the committed docs assets, with a provenance record."""

    description: str = "Copy the figures into docs/examples/stream_inband/images."
    params: ClassVar[dict] = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [f"{e['name']}_svg" for e in FIGURE_MANIFEST]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"docs_figures_sync": Path("docs/examples/stream_inband/images/sync_status.json")}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        repo_root = _HERE.parents[1]
        records = []
        for entry in FIGURE_MANIFEST:
            src = Path(config.root_dir) / entry["source"]
            dst = repo_root / entry["dest"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            records.append({"name": entry["name"], "source": entry["source"],
                            "dest": entry["dest"], "source_sha256": _sha256(src)})
        sync = repo_root / "docs" / "examples" / "stream_inband" / "images" / "sync_status.json"
        sync.write_text(json.dumps({"figures": records}, indent=2) + "\n", encoding="utf-8")
        return {"docs_figures_sync": sync}
