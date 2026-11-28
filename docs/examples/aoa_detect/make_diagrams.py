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
    s = Svg(1360, 900)
    s.text(24, 34, "Emitter detection and AoA: the DUT, its devices and the testbench", size=20,
           weight="bold")
    s.text(24, 56, "the sample path never stalls; every read of the samples goes through the ring's "
           "port A, over the crossbar, where the units compete", size=13.5, color=INK2)

    # --- the testbench: host and sample source ------------------------------------------------
    block(s, 430, 78, 290, 70, "Host (A53): testbench",
          ["sets thresholds; reads results, counters;", "one IRQ every P blocks"], "#ffffff", INK2, INK)
    s.rect(10, 300, 92, 76, fill="#ffffff", stroke=INK2)
    s.lines(18, 322, ["Sample", "source:", "testbench"], size=12, color=INK)

    # --- the DUT -------------------------------------------------------------------------------
    s.rect(112, 172, 1236, 660, fill="none", stroke=INK, rx=16, sw=2.0)
    s.text(1334, 194, "DUT", size=15, weight="bold", anchor="end", color=INK)

    # front-end device
    s.rect(125, 196, 245, 520, fill="none", stroke=BLUE, rx=12, dash="6 4")
    s.text(137, 214, "front-end device", size=12, weight="bold", color=BLUE_D)
    block(s, 140, 228, 215, 196, "Capture + power detector",
          ["writes every sample to the ring", "C energy sums, same pass",
           "absent / active / pending", "notify past a threshold;", "no credit: drop, never wait"],
          BLUE_T, BLUE, BLUE_D)
    block(s, 140, 548, 215, 150, "Ring: dual-port BRAM",
          ["slot[channel][sample][antenna]", "header first, checked last",
           "slave adaptor: ring window", "(port A), registers, counters"],
          AMBER_T, AMBER, AMBER)
    s.arrow([(102, 338), (138, 338)], color=BLUE, sw=2.4)
    s.arrow([(247, 426), (247, 546)], color=BLUE, sw=2.4)
    s.label(247, 490, "port B: write", color=BLUE_D)

    # controller device: controller, two dispatchers, two arbiters
    s.rect(398, 196, 372, 404, fill="none", stroke=VIOLET, rx=12, dash="6 4")
    s.text(410, 214, "controller device", size=12, weight="bold", color=VIOLET)
    block(s, 412, 226, 222, 362, "Controller",
          ["per channel: state, ℓ̂, γ̂", "dispatches jobs to free units",
           "waits on both pools' results", "final tests: a.4, b.2",
           "answers each notification:", "  state message + credit", "a result message per decision"],
          VIOLET_T, VIOLET, VIOLET)
    s.arrow([(357, 270), (410, 270)], color=VIOLET, sw=2)
    s.label(384, 258, "notifications", color=VIOLET, size=10.5)
    s.arrow([(410, 380), (357, 380)], color=VIOLET, sw=2, dash="6 4")
    s.label(384, 368, "state msgs", color=VIOLET, size=10.5)

    def pool(y0, title, unit_rows, color_t, label):
        """A dispatcher, two units (each a device: unit + reader), and an arbiter, at rows from y0."""
        yd, ya = y0 + 14, y0 + 96                     # dispatcher and arbiter centres
        s.rect(664, yd - 20, 94, 40, fill="#ffffff", stroke=INK2)
        s.text(711, yd + 4, "Dispatch", size=12, weight="bold", anchor="middle")
        s.rect(664, ya - 20, 94, 40, fill="#ffffff", stroke=INK2)
        s.text(711, ya + 4, "Arbiter", size=12, weight="bold", anchor="middle")
        s.arrow([(636, yd), (662, yd)], color=VIOLET, sw=2)
        s.arrow([(662, ya), (636, ya)], color=VIOLET, sw=2)
        s.arrow([(711, ya - 22), (711, yd + 22)], color=VIOLET, dash="3 3")
        s.text(704, (ya + yd) / 2 + 4, "m_sel = credit", size=10.5, anchor="end", color=VIOLET)
        s.text(826, y0 - 16, title, size=12, weight="bold", color=INK2)
        for i, yu in enumerate(unit_rows):
            s.rect(822, yu - 8, 300, 62, fill="none", stroke=BLUE, rx=10, dash="5 4")
            s.rect(832, yu, 200, 46, fill=color_t, stroke=BLUE)
            s.text(842, yu + 19, f"{label} {['0', 'k'][i]}", size=12.5, weight="bold", color=BLUE_D)
            s.text(842, yu + 37, "unit device", size=11, color=INK2)
            s.rect(1040, yu, 74, 46, fill="#ffffff", stroke=BLUE)
            s.text(1077, yu + 20, "reader", size=11.5, weight="bold", anchor="middle", color=BLUE_D)
            s.text(1077, yu + 36, "MemRStream", size=9.5, anchor="middle", color=INK2)
        ya_in = [unit_rows[0] + 34, unit_rows[1] + 34]
        # jobs: dispatcher -> fan at x 805 -> each unit
        s.arrow([(760, yd), (805, yd), (805, unit_rows[0] + 12), (830, unit_rows[0] + 12)], color=VIOLET)
        s.arrow([(805, unit_rows[0] + 12), (805, unit_rows[1] + 12), (830, unit_rows[1] + 12)],
                color=VIOLET)
        # results: each unit -> collector at x 785 -> arbiter
        for y in ya_in:
            s.arrow([(830, y), (785, y), (785, ya), (760, ya)], color=VIOLET, halo=True)

    pool(240, "systolic pool: a.2 Gram + a.3 scan, Q̂ inside", [240, 312], BLUE_T, "Systolic unit")
    pool(478, "vector pool: b.2, ‖uᴴX‖²", [478, 550], BLUE_T, "Vector unit")

    # host links (over the crossbar, drawn direct)
    s.arrow([(430, 113), (247, 113), (247, 226)], color=INK2, dash="3 4")
    s.label(330, 106, "registers", color=INK2)
    s.arrow([(520, 224), (520, 150)], color=INK2, dash="3 4")
    s.label(520, 185, "results, summary", color=INK2)
    s.text(728, 112, "(over the crossbar)", size=11, color=INK2, italic=True)

    # crossbar and the reads
    s.rect(398, 760, 790, 40, fill=NEUTRAL, stroke=INK2)
    s.text(793, 785, "AXI crossbar: every unit reads its channel's block here, in competition",
           size=12.5, weight="bold", anchor="middle")
    s.arrow([(357, 650), (380, 650), (380, 780), (396, 780)], color=BLUE, sw=2.4, head=False)
    s.label(380, 720, "port A", color=BLUE_D)
    s.arrow([(1150, 758), (1150, 263), (1116, 263)], color=BLUE, sw=1.8)
    for yu in (335, 501, 573):
        s.arrow([(1150, yu), (1116, yu)], color=BLUE, sw=1.8)
    s.label(1150, 690, "reads", color=BLUE_D)

    # legend
    y = 868
    s.arrow([(24, y), (64, y)], color=BLUE, sw=2.4)
    s.text(72, y + 4, "samples", size=12, color=INK2)
    s.arrow([(160, y), (200, y)], color=VIOLET, sw=2)
    s.text(208, y + 4, "jobs, results, notifications", size=12, color=INK2)
    s.arrow([(400, y), (440, y)], color=VIOLET, sw=2, dash="6 4")
    s.text(448, y + 4, "state messages", size=12, color=INK2)
    s.arrow([(570, y), (610, y)], color=INK2, dash="3 4")
    s.text(618, y + 4, "host, memory-mapped", size=12, color=INK2)
    s.rect(800, y - 9, 30, 16, fill="none", stroke=BLUE, rx=4, dash="5 4")
    s.text(838, y + 4, "a device", size=12, color=INK2)
    return s


def main() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "arch_overview.svg").write_text(arch_overview().render(), encoding="utf-8")
    print(f"wrote {OUT / 'arch_overview.svg'}")


if __name__ == "__main__":
    main()
