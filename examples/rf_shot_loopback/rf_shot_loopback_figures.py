"""Committed-figure workflow for ``rf_shot_loopback``'s docs page.

One figure, and it has one job: make **the delay is an address difference** legible without reading a
gate.

* **address_delay.svg** — the transmitter's memory and the receiver's memory drawn against the *same*
  address axis, one buffer wide.  The waveform sits at address ``a`` on top and at ``a + D`` on the
  bottom, and the shift is annotated with the number the gate reads.  There is no time axis anywhere
  on the figure, which is the point: the measurement never looks at one.

**Why this figure and not a playout.**  ``examples/rf_shot_tx``'s figure draws a *sample axis*
because what that design's page is about is playout shape.  This page is about a **correspondence
between two memories**, and a sample axis would hide it — the reader would see two waveforms offset
in time and have to take on faith that the offset is visible in an address.  Drawing the address axis
is what makes the claim checkable by eye.

**The source is pysim, not a VCD**, for the reason ``rf_shot_tx``'s figures module records: a
VCD-sourced figure needs Vivado to re-render, so it is re-rendered rarely and rots quietly.  This one
is a plain ``python rf_shot_loopback_build.py --through sync_docs_figures`` on any machine, and the
numbers it draws are the ones ``tests/examples/test_rf_shot_loopback.py`` asserts.

Generated SVGs land in ``results/`` (gitignored); :class:`SyncDocsFiguresStep` promotes them into
``docs/examples/rf_shot_loopback/images/`` as committed assets and writes ``sync_status.json`` beside
them — source path plus content hash, the cheap staleness signal a docs lint can check.  Same shape
as ``rf_shot_tx``'s and ``bram_access``'s.

Run it with::

    python rf_shot_loopback_build.py --through sync_docs_figures
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

import matplotlib

matplotlib.use("svg")
# Deterministic SVG: a stable hashsalt fixes the element ids matplotlib would otherwise randomize, so
# a re-render only diffs when the figure truly changed.
matplotlib.rcParams["svg.hashsalt"] = "rf_shot_loopback_figures"
import matplotlib.pyplot as plt  # noqa: E402

from waveflow.build.build import BuildConfig, BuildStep  # noqa: E402

FIGURE_MANIFEST = [
    {"name": "address_delay", "source": "results/address_delay.svg",
     "dest": "docs/examples/rf_shot_loopback/images/address_delay.svg"},
]

#: What the transmitter holds, what the receiver holds, and the shift between them.
C_TX = "#4c72b0"
C_RX = "#c44e52"
C_MARK = "#55a868"


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _measured() -> dict[str, Any]:
    """Run the loopback once and reduce it to what the figure draws.

    Both curves are *by address*: for each of the buffer's :data:`NSAMP` sample slots, which waveform
    sample sits there.  The transmit side is the waveform itself (the loader wrote sample *j* at
    address *j*); the receive side is read out of the captured windows through their headers, which
    is exactly what the gate does.
    """
    from examples.rf_shot_loopback.rf_shot_loopback import (
        CODE_A,
        NSAMP,
        REGION_WORDS,
        SPW,
        channel_delay,
        measured_delay,
        run_pysim,
        waveform,
        window_frames,
        windows_as_codes,
    )
    from waveflow.hw.rf_shot_rx import window_abs_index

    tb = run_pysim()
    frames = window_frames(tb)
    rx = np.full(NSAMP, np.nan)
    for w, (hdr, codes) in enumerate(windows_as_codes(frames)):
        k = window_abs_index(w, int(hdr.n_dropped), REGION_WORDS)
        for off, v in enumerate(np.asarray(codes, dtype=np.int64).tolist()):
            if CODE_A <= v < CODE_A + NSAMP:
                rx[(k * SPW + off) % NSAMP] = v
    return {"tx": waveform(CODE_A).astype(float),
            "rx": rx,
            "raw": measured_delay(frames),
            "delay": channel_delay(tb, frames),
            "loop": int(tb.loop_latency_samp),
            "nsamp": int(NSAMP)}


def render(m: dict[str, Any], out: Path) -> Path:
    """Draw both memories against one address axis and write *out*."""
    nsamp, raw = int(m["nsamp"]), int(m["raw"])
    fig, axes = plt.subplots(2, 1, figsize=(9.5, 4.6), sharex=True, sharey=True)
    x = np.arange(nsamp)

    axes[0].plot(x, m["tx"], lw=1.2, color=C_TX)
    axes[0].set_title("transmitter memory — sample $j$ of the waveform is written at address $j$",
                      fontsize=10, loc="left")
    axes[1].plot(x, m["rx"], lw=1.2, color=C_RX)
    axes[1].set_title(f"receiver memory — the same sample arrives {raw} addresses later",
                      fontsize=10, loc="left")

    # ONE marker pair, so the eye has something to measure rather than a shape to trust.  Sample 0 of
    # the waveform: address 0 above, address `raw` below.
    axes[0].axvline(0, color=C_MARK, lw=1.2, ls="--")
    axes[1].axvline(raw % nsamp, color=C_MARK, lw=1.2, ls="--")
    axes[1].annotate("", xy=(raw % nsamp, m["tx"][0]), xytext=(0, m["tx"][0]),
                     arrowprops={"arrowstyle": "<->", "color": C_MARK, "lw": 1.2})
    axes[1].annotate(f"{raw} addresses  =  path {m['delay']} + loop {m['loop']}",
                     xy=((raw % nsamp) / 2, m["tx"][0]), xytext=(0, 8),
                     textcoords="offset points", ha="center", fontsize=9, color=C_MARK)

    for ax in axes:
        ax.set_ylabel("converter code")
        ax.margins(x=0)
    axes[-1].set_xlabel(f"memory address, in samples  (one buffer is {nsamp}; the reading aliases "
                        f"here)")
    fig.suptitle("rf_shot_loopback — the channel delay IS an address difference "
                 "(no time axis appears on this figure)", fontsize=11)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    # metadata={"Date": None} is what makes the SVG deterministic: matplotlib otherwise stamps a
    # <dc:date> and every re-render diffs for no reason.
    fig.savefig(out, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)
    return out


@dataclass(kw_only=True)
class AddressDelayFigureStep(BuildStep):
    """Render ``results/address_delay.svg`` from one pysim run of the loopback."""

    description: str = "Render the two memories against one address axis."
    consumes: ClassVar[list] = ["rf_shot_loopback_source"]
    produces: ClassVar[dict] = {"address_delay_svg": Path("results/address_delay.svg")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        out = Path(config.root_dir) / "results" / "address_delay.svg"
        return {"address_delay_svg": render(_measured(), out)}


@dataclass(kw_only=True)
class SyncDocsFiguresStep(BuildStep):
    """Promote the generated SVG into the committed docs assets, on demand."""

    description: str = "Copy the address-delay figure into docs/images and record provenance."
    params: ClassVar[dict] = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [f"{e['name']}_svg" for e in FIGURE_MANIFEST]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"docs_figures_sync":
                Path("docs/examples/rf_shot_loopback/images/sync_status.json")}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        repo_root = Path(config.root_dir).parents[1]
        records = []
        for entry in FIGURE_MANIFEST:
            src = Path(config.root_dir) / entry["source"]
            dst = repo_root / entry["dest"]
            if not src.exists():
                raise RuntimeError(f"Manifest source missing: {src} "
                                   f"(run --through address_delay_figure first)")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            records.append({"name": entry["name"], "source": entry["source"],
                            "dest": entry["dest"], "source_sha256": _sha256(src)})
        sync_path = (repo_root / "docs" / "examples" / "rf_shot_loopback" / "images"
                     / "sync_status.json")
        sync_path.parent.mkdir(parents=True, exist_ok=True)
        sync_path.write_text(json.dumps({"figures": records}, indent=2) + "\n", encoding="utf-8")
        return {"docs_figures_sync": sync_path}
