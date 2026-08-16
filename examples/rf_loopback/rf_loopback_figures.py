"""Render the guide's RF loopback figure from an actual run.

A **committed figure from a deterministic run**, the pattern the timing docs already use: the SVG in
``docs/`` is an output of this script, never hand-drawn, so it cannot drift from what the model does.
Re-run it after changing the example and commit the result.

The waveform is a windowed sinusoid, and that choice is doing two jobs.  It exercises the quantizer
(on-grid samples make ``from_real`` a no-op — see ``RfLoopbackSim._grid_blocks``), and it makes the
two structural facts of an RF loopback *visible* rather than inferred from array indices:

- the output is the input shifted by ``loop_blk_latency`` whole blocks, and
- the leading blocks are the DAC's zero-fill, before any samples have reached it.

Both are things a reader is otherwise asked to take on trust from an assertion about indices.

    python examples/rf_loopback/rf_loopback_figures.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import numpy as np                        # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from examples.rf_loopback.rf_loopback import RfLoopbackSim   # noqa: E402

#: Where the guide reads it from.
OUT = _ROOT / "docs" / "guide" / "rf" / "figures" / "rf_loopback_sine.svg"

N_BLK = 8


def render(out: Path = OUT) -> Path:
    sim = RfLoopbackSim(n_src_blk=N_BLK, waveform="sine")
    tb = sim.run()
    lat = int(tb.loop_blk_latency)
    blk = int(tb.blksize)

    sent = np.concatenate([b[0] for b in sim.sent])
    got = np.concatenate([b[0] for b in sim.captured])
    n = min(sent.size, got.size)

    fig, ax = plt.subplots(figsize=(9.0, 3.4))

    # Block boundaries first, so the traces sit on top of them.
    for k in range(0, n // blk + 1):
        ax.axvline(k * blk, color="0.88", lw=0.8, zorder=0)

    ax.plot(np.arange(n), sent[:n], lw=1.0, color="#4c72b0", label="in  (RfDataSource)")
    ax.plot(np.arange(n), got[:n], lw=1.0, color="#dd8452", label="out (RfDataSink)")

    # The startup transient: the DAC emits before anything has reached it.
    ax.axvspan(0, lat * blk, color="#dd8452", alpha=0.10, zorder=0)
    ax.text(lat * blk / 2, ax.get_ylim()[1] * 0.80,
            f"zero-fill\n{lat} blocks", ha="center", va="top", fontsize=8, color="#a05a30")

    # The shift, measured between the two burst onsets rather than asserted.
    on_in = int(np.argmax(np.abs(sent) > 1e-9))
    on_out = int(np.argmax(np.abs(got) > 1e-9))
    y = ax.get_ylim()[1] * 0.55
    ax.annotate("", xy=(on_out, y), xytext=(on_in, y),
                arrowprops=dict(arrowstyle="<->", color="0.35", lw=1.0))
    ax.text((on_in + on_out) / 2, y * 1.06,
            f"{(on_out - on_in) // blk} block = {on_out - on_in} samples",
            ha="center", va="bottom", fontsize=8, color="0.25")

    ax.set_xlabel("sample index  (grid lines = block boundaries)")
    ax.set_ylabel("amplitude")
    ax.set_xlim(0, n)
    ax.legend(loc="lower right", fontsize=8, framealpha=0.9)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, format="svg", metadata={"Date": None})   # no timestamp: keep it byte-stable
    plt.close(fig)
    return out


if __name__ == "__main__":
    p = render()
    print(f"wrote {p.relative_to(_ROOT)}")
