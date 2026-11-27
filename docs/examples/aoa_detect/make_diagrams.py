"""make_diagrams.py — the architecture diagram of the emitter detection and AoA example, as static SVG.

Writes ``images/arch_overview.svg`` next to this script, for ``arch.md`` in this folder.
Run from the repo root::

    python docs/examples/aoa_detect/make_diagrams.py

Drawn from a few primitives and colour tokens (those of ``docs/examples/mimo_cg/make_diagrams.py``),
on an opaque light surface so that it reads the same in light and dark viewers and in slides.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent / "images"

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
LINE = "#c3c2b7"
NEUTRAL = "#f0efec"
BLUE, BLUE_T, BLUE_D = "#2a78d6", "#e3eefb", "#1c5cab"
VIOLET, VIOLET_T = "#4a3aa7", "#e9e6f7"
AMBER, AMBER_T = "#a8660b", "#fbf0de"
FONT = "Helvetica, Arial, 'DejaVu Sans', sans-serif"


class Svg:
    """A tiny SVG builder: shapes, text and arrows in user units (px)."""

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.items: list[str] = []
        self.colors: set[str] = set()

    def rect(self, x, y, w, h, fill=NEUTRAL, stroke=LINE, rx=8, sw=1.5, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.items.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{sw}"{d}/>'
        )

    def text(self, x, y, s, size=13, weight="normal", anchor="start", color=INK, italic=False):
        it = ' font-style="italic"' if italic else ""
        self.items.append(
            f'<text x="{x}" y="{y}" xml:space="preserve" font-family="{FONT}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}" fill="{color}"{it}>{escape(s)}</text>'
        )

    def lines(self, x, y, rows, size=12, gap=None, **kw):
        gap = gap or size * 1.4
        for i, r in enumerate(rows):
            self.text(x, y + i * gap, r, size=size, **kw)

    def arrow(self, pts, color=INK2, sw=1.8, dash=None, head=True, halo=False):
        """A polyline with an arrowhead; ``halo`` draws a surface-coloured band under it, so it
        crosses an earlier line with a visible gap (a bridge)."""
        self.colors.add(color)
        d = "M " + " L ".join(f"{x} {y}" for x, y in pts)
        if halo:
            self.items.append(f'<path d="{d}" fill="none" stroke="{SURFACE}" stroke-width="{sw + 6}"/>')
        mk = f' marker-end="url(#ah{color[1:]})"' if head else ""
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self.items.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}"{da}{mk} '
            f'stroke-linejoin="round"/>'
        )

    def label(self, x, y, s, color=INK2, anchor="middle", size=11.5):
        """A small label on a light pad, so it stays legible over a line."""
        w = 6.3 * len(s) + 10
        x0 = x - w / 2 if anchor == "middle" else (x if anchor == "start" else x - w)
        self.rect(x0, y - 13, w, 18, fill=SURFACE, stroke=SURFACE, rx=4, sw=0)
        tx = x0 + w / 2
        self.text(tx, y, s, size=size, anchor="middle", color=color)

    def render(self) -> str:
        markers = "".join(
            f'<marker id="ah{c[1:]}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="10" '
            f'markerHeight="10" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
            for c in sorted(self.colors)
        )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}">\n<defs>{markers}</defs>\n'
            f'<rect width="{self.w}" height="{self.h}" fill="{SURFACE}"/>\n'
            + "\n".join(self.items)
            + "\n</svg>\n"
        )


def block(s: Svg, x, y, w, h, title, rows, fill, stroke, tcolor):
    s.rect(x, y, w, h, fill=fill, stroke=stroke)
    s.text(x + 14, y + 24, title, size=14.5, weight="bold", color=tcolor)
    s.lines(x + 14, y + 46, rows, size=12, color=INK2)


def arch_overview() -> Svg:
    s = Svg(1300, 800)
    s.text(24, 34, "Emitter detection and AoA: the architecture", size=20, weight="bold")
    s.text(24, 56, "the sample path never stalls; every read of the samples goes through the "
           "ring's port A, where the jobs compete", size=13.5, color=INK2)

    # --- host --------------------------------------------------------------------------------
    block(s, 400, 80, 280, 74, "Host (A53)",
          ["sets thresholds, reads counters", "one summary + IRQ every P blocks"],
          "#ffffff", INK2, INK)

    # --- capture + power detector -----------------------------------------------------------
    block(s, 70, 200, 260, 190, "Capture + power detector",
          ["writes every sample to the ring",
           "C energy sums, in the same pass",
           "per channel: absent / active /",
           "    pending",
           "notify past a threshold;",
           "no credit: drop, never wait"],
          BLUE_T, BLUE, BLUE_D)
    s.arrow([(14, 300), (68, 300)], color=BLUE, sw=2.4)
    s.text(14, 284, "samples", size=12, color=BLUE_D)
    s.text(14, 322, "B·C·nrx", size=11.5, color=INK2)
    s.text(14, 337, "a block", size=11.5, color=INK2)

    # --- ring ---------------------------------------------------------------------------------
    block(s, 70, 520, 260, 150, "Ring: R slots, dual-port BRAM",
          ["slot[channel][sample][antenna]",
           "header (block index) written first,",
           "checked last by each reader:",
           "overwritten while read → late"],
          AMBER_T, AMBER, AMBER)
    s.arrow([(200, 392), (200, 518)], color=BLUE, sw=2.4)
    s.label(200, 462, "port B: write only", color=BLUE_D)

    # --- controller ---------------------------------------------------------------------------
    block(s, 400, 200, 280, 230, "Controller (hardware)",
          ["per channel: state, ℓ̂ (θ̂), γ̂,",
           "    last block",
           "dispatches jobs to free units",
           "final tests: a.4 detect, b.2 stop",
           "answers each notification:",
           "    state message + credit",
           "summary to host every P blocks"],
          VIOLET_T, VIOLET, VIOLET)
    # detector <-> controller
    s.arrow([(332, 250), (398, 250)], color=VIOLET, sw=2)
    s.label(365, 240, "notifications", color=VIOLET)
    s.label(365, 270, "(credit stream)", color=INK2, size=10.5)
    s.arrow([(398, 350), (332, 350)], color=VIOLET, sw=2, dash="6 4")
    s.label(365, 340, "state messages", color=VIOLET)
    s.label(365, 370, "(a queue)", color=INK2, size=10.5)
    # host links
    s.arrow([(400, 117), (200, 117), (200, 198)], color=INK2, dash="3 4")
    s.label(290, 110, "registers: thresholds, counters", color=INK2)
    s.arrow([(600, 198), (600, 156)], color=INK2, dash="3 4")
    s.label(600, 182, "summary", color=INK2)

    # --- dispatchers, pools, arbiter ----------------------------------------------------------
    s.rect(740, 222, 120, 54, fill="#ffffff", stroke=INK2)
    s.text(800, 245, "StreamDispatch", size=12, weight="bold", anchor="middle")
    s.text(800, 264, "first free", size=11.5, anchor="middle", color=INK2)
    s.rect(740, 478, 120, 54, fill="#ffffff", stroke=INK2)
    s.text(800, 501, "StreamDispatch", size=12, weight="bold", anchor="middle")
    s.text(800, 520, "first free", size=11.5, anchor="middle", color=INK2)
    s.arrow([(682, 249), (738, 249)], color=VIOLET, sw=2)
    s.label(710, 240, "arrival", color=VIOLET, size=10.5)
    s.arrow([(682, 400), (710, 400), (710, 505), (738, 505)], color=VIOLET, sw=2)
    s.label(710, 455, "departure", color=VIOLET, size=10.5)

    # systolic pool
    s.rect(900, 176, 250, 150, fill="#ffffff", stroke=LINE, rx=12, dash="5 4")
    s.text(912, 196, "systolic pool", size=12, weight="bold", color=INK2)
    for i, (y, name) in enumerate([(206, "Systolic unit 0"), (262, "Systolic unit k")]):
        s.rect(912, y, 226, 48, fill=BLUE_T, stroke=BLUE)
        s.text(924, y + 20, name, size=12.5, weight="bold", color=BLUE_D)
        s.text(924, y + 38, "a.2 Gram + a.3 scan, Q̂ inside", size=11.5, color=INK2)
    s.arrow([(862, 249), (880, 249), (880, 230), (910, 230)], color=VIOLET)
    s.arrow([(880, 249), (880, 286), (910, 286)], color=VIOLET)

    # vector pool
    s.rect(900, 432, 250, 150, fill="#ffffff", stroke=LINE, rx=12, dash="5 4")
    s.text(912, 452, "vector pool", size=12, weight="bold", color=INK2)
    for i, (y, name) in enumerate([(462, "Vector unit 0"), (518, "Vector unit m")]):
        s.rect(912, y, 226, 48, fill=BLUE_T, stroke=BLUE)
        s.text(924, y + 20, name, size=12.5, weight="bold", color=BLUE_D)
        s.text(924, y + 38, "b.2: ‖uᴴX‖², nrx MACs a cycle", size=11.5, color=INK2)
    s.arrow([(862, 505), (880, 505), (880, 486), (910, 486)], color=VIOLET)
    s.arrow([(880, 505), (880, 542), (910, 542)], color=VIOLET)

    # the systolic pool's reads, drawn first so the result lines bridge over them
    s.arrow([(1190, 688), (1190, 316), (1152, 316)], color=BLUE, sw=1.8)
    s.label(1190, 640, "reads", color=BLUE_D)

    # results: each unit to the arbiter, the arbiter back to the controller
    s.rect(1186, 146, 100, 48, fill="#ffffff", stroke=INK2)
    s.text(1236, 166, "StreamArbiter", size=11.5, weight="bold", anchor="middle")
    s.text(1236, 184, "results", size=11.5, anchor="middle", color=INK2)
    for y in (230, 286, 486, 542):
        s.arrow([(1140, y), (1236, y), (1236, 196)], color=VIOLET, halo=True)
    s.arrow([(1184, 168), (640, 168), (640, 198)], color=VIOLET, sw=2)
    s.label(920, 161, "results: ℓ* and p, or p_b; a late flag", color=VIOLET)

    # --- crossbar and reads -------------------------------------------------------------------
    s.rect(400, 690, 810, 42, fill=NEUTRAL, stroke=INK2)
    s.text(805, 716, "AXI crossbar: every job reads its channel's block here, in competition",
           size=12.5, weight="bold", anchor="middle", color=INK)
    s.arrow([(398, 711), (365, 711), (365, 600), (332, 600)], color=BLUE, sw=2.4, head=False)
    s.label(365, 655, "port A", color=BLUE_D)
    s.arrow([(1025, 688), (1025, 568)], color=BLUE, sw=1.8)
    s.label(1025, 632, "reads", color=BLUE_D)

    # --- legend --------------------------------------------------------------------------------
    y = 770
    s.arrow([(24, y), (64, y)], color=BLUE, sw=2.4)
    s.text(72, y + 4, "samples", size=12, color=INK2)
    s.arrow([(160, y), (200, y)], color=VIOLET, sw=2)
    s.text(208, y + 4, "jobs, results, notifications", size=12, color=INK2)
    s.arrow([(400, y), (440, y)], color=VIOLET, sw=2, dash="6 4")
    s.text(448, y + 4, "state messages", size=12, color=INK2)
    s.arrow([(570, y), (610, y)], color=INK2, dash="3 4")
    s.text(618, y + 4, "host, memory-mapped", size=12, color=INK2)
    return s


def main() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "arch_overview.svg").write_text(arch_overview().render(), encoding="utf-8")
    print(f"wrote {OUT / 'arch_overview.svg'}")


if __name__ == "__main__":
    main()
