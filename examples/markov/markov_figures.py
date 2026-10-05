"""markov_figures.py — the Markov example's docs figure, as two build steps.

* **chain_output.svg** — what the chain produces.  Top: the state ``x[k]`` over the first steps for
  two transition settings, a *sticky* chain (rare switches, long runs) and a *jumpy* one.  Bottom: the
  running fraction of ones for each, converging to the stationary probability
  ``pi_1 = p01 / (p01 + p10)`` (dashed).

The data is the golden model (``markov_golden``) -- which the RTL reproduces bit for bit
(``tests/examples/test_markov_xsi.py``) -- so the figure needs no toolchain.

``MarkovFiguresStep`` renders into ``results/`` (gitignored); ``SyncDocsFiguresStep`` promotes it into
``docs/examples/markov/images/`` with a provenance record, so a committed figure changes only when you
mean it to:

    python -m examples.markov.markov_build --figures
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
# Deterministic SVG: a stable hashsalt fixes the element ids matplotlib would otherwise randomize, so a
# re-render only diffs when the figure truly changed.
matplotlib.rcParams["svg.hashsalt"] = "markov_figures"
import matplotlib.pyplot as plt  # noqa: E402

from waveflow.build.build import BuildConfig, BuildStep  # noqa: E402

from examples.markov.markov import UBITS, markov_golden  # noqa: E402

FIGURE_MANIFEST = [
    {"name": "chain_output", "source": "results/chain_output.svg",
     "dest": "docs/examples/markov/images/chain_output.svg"},
]

Q = 1 << UBITS
#: The two settings the figure compares: (label, p01, p10) in Q16, and a colour.
SETTINGS = [
    ("sticky: p01 = p10 = 0.015", 1000, 1000, "#4C78A8"),
    ("jumpy: p01 = 0.69, p10 = 0.46", 45000, 30000, "#E45756"),
]
NSHOW, NLONG, SEED = 200, 20000, 2026


def stationary(p01: int, p10: int) -> float:
    """P(x = 1) in the long run: ``p01 / (p01 + p10)``."""
    return p01 / (p01 + p10)


def render_chain_output(path: Path) -> None:
    fig, (top, bot) = plt.subplots(2, 1, figsize=(8, 5.6), gridspec_kw={"height_ratios": [1, 1.2]})
    for i, (label, p01, p10, color) in enumerate(SETTINGS):
        x = markov_golden(dict(n=NLONG, x0=0, seed=SEED + i, p01=p01, p10=p10)).astype(float)
        off = 1.5 * (len(SETTINGS) - 1 - i)
        top.step(np.arange(NSHOW), x[:NSHOW] + off, where="post", color=color, lw=1.2, label=label)
        k = np.arange(1, NLONG + 1)
        bot.plot(k, np.cumsum(x) / k, color=color, lw=1.2, label=label)
        pi1 = stationary(p01, p10)
        bot.axhline(pi1, color=color, ls="--", lw=0.9)
        bot.annotate(f"$\\pi_1$ = {pi1:.2f}", xy=(NLONG, pi1), xytext=(4, 0),
                     textcoords="offset points", va="center", fontsize=8, color=color)
    top.set_yticks([0.5, 2.0])
    top.set_yticklabels(["jumpy", "sticky"])
    top.set_xlim(0, NSHOW)
    top.set_xlabel("step k")
    top.set_title("The chain's state x[k] (0 / 1), first steps")
    bot.set_xscale("log")
    bot.set_xlim(1, NLONG)
    bot.set_ylim(0, 1)
    bot.set_xlabel("steps k")
    bot.set_ylabel("fraction of x = 1")
    bot.set_title("Running fraction of ones, converging to the stationary probability (dashed)")
    bot.legend(loc="upper right", fontsize=8, frameon=False)
    fig.tight_layout()
    _save_svg(fig, path)


def _save_svg(fig, path: Path) -> None:
    """Write a deterministic SVG (no embedded timestamp)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class MarkovFiguresStep(BuildStep):
    """Render the Markov docs figure into ``results/`` from the golden model."""

    description: str = "Render the Markov chain-output figure from the golden model."
    params: ClassVar[dict] = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return []

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {f"{e['name']}_svg": Path(e["source"]) for e in FIGURE_MANIFEST}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        dst = root / "results" / "chain_output.svg"
        render_chain_output(dst)
        return {"chain_output_svg": dst}


@dataclass(kw_only=True)
class SyncDocsFiguresStep(BuildStep):
    """Promote the rendered SVG into the committed docs assets, with a provenance record."""

    description: str = "Copy the Markov figure into docs/images and record provenance."
    params: ClassVar[dict] = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [f"{e['name']}_svg" for e in FIGURE_MANIFEST]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"docs_figures_sync": Path("docs/examples/markov/images/sync_status.json")}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        repo_root = Path(config.root_dir).parents[1]
        records = []
        for entry in FIGURE_MANIFEST:
            src = Path(config.root_dir) / entry["source"]
            dst = repo_root / entry["dest"]
            if not src.exists():
                raise RuntimeError(f"Manifest source missing: {src} (run the markov_figures step first)")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            records.append({"name": entry["name"], "source": entry["source"],
                            "dest": entry["dest"], "source_sha256": _sha256(src)})
        sync_path = repo_root / "docs" / "examples" / "markov" / "images" / "sync_status.json"
        sync_path.write_text(json.dumps({"figures": records}, indent=2) + "\n", encoding="utf-8")
        return {"docs_figures_sync": sync_path}
