"""Render the RF loopback's three figures from actual runs.

**Committed figures from deterministic runs**, the pattern the timing docs already use: every SVG in
``docs/`` is an output of this script, never hand-drawn, so a picture cannot drift from what the model
does.  Re-run it after changing the example and commit the result.

Three outputs, and each carries a claim a sentence carries badly:

======================== ============================================================================
``rf_source_sine.svg``   what goes *in*, before anything happens to it — the windowed sinusoid the
                         source plays, read back out of the bundle it was written to.
``rf_loopback_sine.svg`` the loopback: the same burst out, shifted by ``loop_blk_latency`` whole
                         blocks, behind the DAC's leading zero-fill.
``rf_late_producer.svg`` the late-producer fault: two extra flat blocks at the sink, which is what
                         ``adc_if.underrun == 2`` looks like.
======================== ============================================================================

The first two use the **sine** waveform, because it exercises the quantizer (on-grid samples make
``from_real`` a no-op — see ``RfLoopbackSim._grid_blocks``) and because a burst with a hard start and
stop makes a block shift visible rather than inferred from array indices.

The third uses the **grid** waveform for the opposite reason: every one of its blocks is full-scale
noise, so a flat block at the sink can *only* be zero-fill.  With the sine, the leading zero-fill and
the closed part of the window look identical, and the figure would prove nothing.

    python examples/rf_loopback/rf_loopback_figures.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
# Deterministic element ids.  matplotlib salts the ``id=`` attributes it writes into an SVG with a
# uuid4 unless this is set, so two renders of the same figure differ byte for byte and every
# regeneration shows up as a diff.  A committed artifact has to be reproducible to be checkable.
matplotlib.rcParams["svg.hashsalt"] = "waveflow-rf-loopback"

import matplotlib.pyplot as plt          # noqa: E402
import numpy as np                        # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from examples.rf_loopback.rf_loopback import RfLoopbackSim   # noqa: E402
from waveflow.simulation.rf_tb import read_rf_bundle          # noqa: E402

#: Where the guide and the example page read them from.
FIG_DIR = _ROOT / "docs" / "guide" / "rf" / "figures"
SOURCE_OUT = FIG_DIR / "rf_source_sine.svg"
OUT = FIG_DIR / "rf_loopback_sine.svg"          # historic name; kept, the pages link it
LATE_OUT = FIG_DIR / "rf_late_producer.svg"

N_BLK = 8

#: The late-producer scenario, matching ``tests/examples/test_rf_loopback.py``'s fault injection
#: exactly — same block size, same delay — so the figure and the assertion describe one run.
LATE_BLKSIZE = 64
LATE_DELAY_BLOCKS = 2.5

_IN_COLOUR = "#4c72b0"
_OUT_COLOUR = "#dd8452"


def _blocks(ax, n: int, blk: int) -> None:
    """Block boundaries, as the faint grid every one of these figures is read against."""
    for k in range(0, n // blk + 1):
        ax.axvline(k * blk, color="0.88", lw=0.8, zorder=0)


def _label(ax, text: str, colour: str) -> None:
    ax.text(0.012, 0.86, text, transform=ax.transAxes, fontsize=9, color=colour, fontweight="bold")
    ax.set_ylabel("amplitude")
    ax.spines[["top", "right"]].set_visible(False)


def _save(fig, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, format="svg", metadata={"Date": None})   # no timestamp: keep it byte-stable
    plt.close(fig)
    return out


def _leading_zero_blocks(captured: list[np.ndarray]) -> int:
    """How many whole blocks at the head of a capture are the zero-fill."""
    for k, b in enumerate(captured):
        if np.any(b):
            return k
    return len(captured)


# ---------------------------------------------------------------------------------------------
# 1.  The source data alone
# ---------------------------------------------------------------------------------------------

def render_source(out: Path = SOURCE_OUT) -> Path:
    """The windowed sinusoid the source plays — read back **from the bundle on disk**.

    Not from ``sim.sent``: the bundle is what both backends actually consume, so plotting the file
    means the picture cannot disagree with what the run was fed.
    """
    sim = RfLoopbackSim(n_src_blk=N_BLK, waveform="sine")
    with tempfile.TemporaryDirectory() as root:
        sim.write_scenario(root)
        blocks = read_rf_bundle(Path(root) / "vectors" / "rf_in", 1, sim.tb.blksize)

    x = np.concatenate([b[0] for b in blocks])
    blk, n = int(sim.tb.blksize), x.size
    fs = float(sim.tb.full_scale)

    fig, ax = plt.subplots(figsize=(9.0, 2.6))
    _blocks(ax, n, blk)
    ax.axhline(fs, color="0.6", lw=0.8, ls=(0, (4, 3)))
    ax.axhline(-fs, color="0.6", lw=0.8, ls=(0, (4, 3)))
    ax.text(n * 0.995, fs, " full scale", ha="right", va="bottom", fontsize=8, color="0.4")
    ax.plot(np.arange(n), x, lw=1.0, color=_IN_COLOUR)
    _label(ax, "in  ·  RfDataSource", _IN_COLOUR)

    # The window measured in WHOLE BLOCKS -- which blocks carry signal at all.  Measuring it from
    # the first non-zero *sample* is off by one: the window opens on a zero crossing, so
    # ``sin(0) == 0`` exactly and the first live sample sits one index inside the first live block.
    live = [k for k, b in enumerate(blocks) if np.any(b)]
    on, off = live[0] * blk, (live[-1] + 1) * blk
    for e in (on, off):
        ax.axvline(e, color="0.35", lw=0.9, ls=(0, (4, 3)))
    ax.annotate("", xy=(off, -fs * 0.72), xytext=(on, -fs * 0.72),
                arrowprops=dict(arrowstyle="<->", color="0.35", lw=1.0))
    ax.text((on + off) / 2, -fs * 0.68, f"window: {len(live)} blocks = {off - on} samples",
            ha="center", va="bottom", fontsize=8, color="0.25")
    ax.text(on / 2, 0.0, "silent", ha="center", va="bottom", fontsize=8, color="0.45")

    ax.set_xlabel("sample index  (grid lines = block boundaries)")
    ax.set_xlim(0, n)
    ax.set_ylim(-fs * 1.15, fs * 1.15)
    fig.tight_layout()
    return _save(fig, out)


# ---------------------------------------------------------------------------------------------
# 2.  The loopback
# ---------------------------------------------------------------------------------------------

def render(out: Path = OUT) -> Path:
    """In and out, stacked: the block shift and the leading zero-fill."""
    sim = RfLoopbackSim(n_src_blk=N_BLK, waveform="sine")
    tb = sim.run()
    lat = int(tb.loop_blk_latency)
    blk = int(tb.blksize)

    sent = np.concatenate([b[0] for b in sim.sent])
    got = np.concatenate([b[0] for b in sim.captured])
    n = min(sent.size, got.size)

    # TWO PANELS, not one.  Overlaid traces occlude: the output is drawn over the input, so in the
    # region where they coincide the input vanishes and the output looks twice as long as it is.
    # Stacked panels with a shared x-axis show a delay without hiding either signal.
    fig, (ax_in, ax_out) = plt.subplots(2, 1, figsize=(9.0, 4.2), sharex=True, sharey=True)

    on_in = int(np.argmax(np.abs(sent) > 1e-9))
    on_out = int(np.argmax(np.abs(got) > 1e-9))

    for ax, y, colour, label in ((ax_in, sent[:n], _IN_COLOUR, "in  ·  RfDataSource"),
                                 (ax_out, got[:n], _OUT_COLOUR, "out ·  RfDataSink")):
        _blocks(ax, n, blk)
        ax.plot(np.arange(n), y, lw=1.0, color=colour)
        _label(ax, label, colour)

    # The startup transient, on the output panel where it happens.
    ax_out.axvspan(0, lat * blk, color=_OUT_COLOUR, alpha=0.10, zorder=0)
    ax_out.text(lat * blk / 2, ax_out.get_ylim()[1] * 0.62, f"zero-fill: {lat} blocks",
                ha="center", va="top", fontsize=8, color="#a05a30")

    # The shift, measured between the two burst onsets rather than asserted -- drawn as a guide
    # from one panel to the other so it reads as a delay.
    for ax, x in ((ax_in, on_in), (ax_out, on_out)):
        ax.axvline(x, color="0.35", lw=0.9, ls=(0, (4, 3)))
    ax_out.annotate("", xy=(on_out, ax_out.get_ylim()[1] * 0.80),
                    xytext=(on_in, ax_out.get_ylim()[1] * 0.80),
                    arrowprops=dict(arrowstyle="<->", color="0.35", lw=1.0))
    ax_out.text((on_in + on_out) / 2, ax_out.get_ylim()[1] * 0.84,
                f"{(on_out - on_in) // blk} blocks = {on_out - on_in} samples",
                ha="center", va="bottom", fontsize=8, color="0.25")

    ax_out.set_xlabel("sample index  (grid lines = block boundaries)")
    ax_in.set_xlim(0, n)
    fig.tight_layout()
    return _save(fig, out)


# ---------------------------------------------------------------------------------------------
# 3.  The late-producer fault
# ---------------------------------------------------------------------------------------------

def _late_run(delay_blocks: float) -> RfLoopbackSim:
    sim = RfLoopbackSim(n_src_blk=N_BLK, blksize=LATE_BLKSIZE, waveform="grid")
    sim.tb.source.start_delay = delay_blocks * sim.tb.blk_period
    sim.run()
    return sim


def render_late_producer(out: Path = LATE_OUT) -> Path:
    """The same capture twice: on time, and with the source starting 2.5 block periods late.

    Both panels are the **sink's** output.  The fault is two extra flat blocks at the head, which is
    what ``adc_if.underrun == 2`` looks like from the far end of the loopback.
    """
    clean, late = _late_run(0.0), _late_run(LATE_DELAY_BLOCKS)
    blk = int(clean.tb.blksize)

    fig, axes = plt.subplots(2, 1, figsize=(9.0, 4.2), sharex=True, sharey=True)
    for ax, sim, title in ((axes[0], clean, "on time"),
                           (axes[1], late, f"source {LATE_DELAY_BLOCKS:g} block periods late")):
        y = np.concatenate([b[0] for b in sim.captured])
        z = _leading_zero_blocks(sim.captured)
        _blocks(ax, y.size, blk)
        ax.axvspan(0, z * blk, color=_OUT_COLOUR, alpha=0.12, zorder=0)
        ax.plot(np.arange(y.size), y, lw=0.7, color=_OUT_COLOUR)
        _label(ax, f"out ·  {title}", _OUT_COLOUR)
        # Low in the shaded band, clear of the panel label above it.
        ax.text(z * blk / 2, ax.get_ylim()[0] * 0.80,
                f"{z} flat blocks\nadc_if.underrun = {sim.tb.adc_if.underrun}",
                ha="center", va="bottom", fontsize=8, color="#a05a30")

    extra = _leading_zero_blocks(late.captured) - _leading_zero_blocks(clean.captured)
    axes[1].set_xlabel(f"sample index  ·  {extra} blocks the ADC had nothing to send  "
                       f"(grid lines = block boundaries)")
    axes[0].set_xlim(0, N_BLK * blk)
    fig.tight_layout()
    return _save(fig, out)


def render_all() -> list[Path]:
    return [render_source(), render(), render_late_producer()]


if __name__ == "__main__":
    for p in render_all():
        print(f"wrote {p.relative_to(_ROOT)}")
