"""make_diagrams.py — the CG iteration diagram of the CG massive-MIMO example, as static SVG.

Writes ``images/diagram_cg_iteration.svg`` next to this script, for ``index.md`` in this folder.
Run from the repo root::

    python docs/examples/mimo_cg/make_diagrams.py

The diagram is drawn from the few primitives and colour tokens below, on an opaque light surface
so that it reads the same in light and dark viewers and in slides.
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
FONT = "Helvetica, Arial, 'DejaVu Sans', sans-serif"
MONO = "'DejaVu Sans Mono', Menlo, Consolas, monospace"


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

    def text(
        self,
        x,
        y,
        s,
        size=14,
        weight="normal",
        anchor="start",
        color=INK,
        mono=False,
        italic=False,
    ):
        fam = MONO if mono else FONT
        it = ' font-style="italic"' if italic else ""
        self.items.append(
            f'<text x="{x}" y="{y}" xml:space="preserve" font-family="{fam}" font-size="{size}" '
            f'font-weight="{weight}" '
            f'text-anchor="{anchor}" fill="{color}"{it}>{escape(s)}</text>'
        )

    def lines(self, x, y, rows, size=13, gap=None, **kw):
        gap = gap or size * 1.35
        for i, r in enumerate(rows):
            self.text(x, y + i * gap, r, size=size, **kw)

    def arrow(self, pts, color=INK2, sw=1.8, dash=None, head=True):
        self.colors.add(color)
        d = "M " + " L ".join(f"{x} {y}" for x, y in pts)
        mk = f' marker-end="url(#ah{color[1:]})"' if head else ""
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self.items.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}"{da}{mk} '
            f'stroke-linejoin="round"/>'
        )

    def curve(self, d, color=INK2, sw=1.8, dash=None, head=True):
        self.colors.add(color)
        mk = f' marker-end="url(#ah{color[1:]})"' if head else ""
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self.items.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}"{da}{mk}/>'
        )

    def circle(self, x, y, r, fill, stroke=None, sw=1.5):
        s = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        self.items.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}"{s}/>')

    def badge(self, x, y, label, color):
        w = 9 + 7.2 * len(label)
        self.rect(x, y, w, 20, fill=color, stroke=color, rx=10)
        self.text(
            x + w / 2,
            y + 14,
            label,
            size=11.5,
            weight="bold",
            anchor="middle",
            color="#ffffff",
        )

    def render(self) -> str:
        markers = "".join(
            f'<marker id="ah{c[1:]}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="11" '
            f'markerHeight="11" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" '
            f'fill="{c}"/></marker>'
            for c in sorted(self.colors)
        )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}">\n<defs>{markers}</defs>\n'
            f'<rect width="{self.w}" height="{self.h}" fill="{SURFACE}"/>\n'
            + "\n".join(self.items)
            + "\n</svg>\n"
        )


def title(s: Svg, t: str, sub: str | None = None) -> None:
    s.text(24, 34, t, size=20, weight="bold")
    if sub:
        s.text(24, 56, sub, size=13.5, color=INK2)


# --- 1. the study -------------------------------------------------------------------------


def cg_iteration() -> Svg:
    s = Svg(1180, 560)
    title(
        s,
        "One CG iteration, bit-exact, split across two hardware blocks",
        "multi-RHS conjugate gradient on (A/M) X = B/M — every column of B has its own α and β",
    )
    # inputs
    s.rect(24, 92, 230, 150, fill="#ffffff", stroke=LINE)
    s.text(38, 118, "Per coherence block", size=14, weight="bold")
    s.lines(
        38,
        144,
        [
            "A = HᴴH + σ²I,  K × K",
            "B = HᴴY,  K × N  (N = 32)",
            "formed in floating point,",
            "÷ M, quantized to A, B formats",
        ],
        size=12.5,
        color=INK2,
    )
    # init
    s.rect(24, 272, 230, 132, fill="#ffffff", stroke=LINE)
    s.text(38, 298, "Start (in the vector unit)", size=14, weight="bold")
    s.lines(
        38,
        324,
        ["X = 0", "R = q_R(B),  P = q_P(R)", "rz = q_rz(Σ_k |R|²)"],
        size=12.5,
        color=INK2,
        mono=True,
    )
    s.arrow([(139, 242), (139, 270)])
    # loop frame
    s.rect(290, 84, 640, 380, fill="#ffffff", stroke=INK2, dash="6 5", rx=14)
    s.text(
        306,
        108,
        "repeat nit times  (nit ≤ K, a runtime field)",
        size=13,
        weight="bold",
        color=INK2,
    )
    # matmul block
    s.rect(318, 126, 270, 108, fill=BLUE_T, stroke=BLUE)
    s.text(334, 152, "Matrix-multiply block", size=15, weight="bold", color=BLUE_D)
    s.text(334, 180, "1  S = q_S(A · P)", size=13.5, mono=True)
    s.lines(
        334,
        206,
        ["systolic R × C array, exact sums", "one rounding per entry"],
        size=12,
        color=INK2,
    )
    # vector unit
    s.rect(318, 268, 590, 180, fill=VIOLET_T, stroke=VIOLET)
    s.text(
        334,
        294,
        "Vector unit (per column, L columns in parallel)",
        size=15,
        weight="bold",
        color=VIOLET,
    )
    rows_l = [
        "2  ps = q_ps(Σ Re(conj(P)·S))",
        "3  α  = q_α(rz/ps),  0 if ps = 0",
        "4  X  = q_X(X + P·α)",
        "5  R  = q_R(R − S·α)",
    ]
    rows_r = [
        "6  rz' = q_rz(Σ |R|²)",
        "7  β   = q_β(rz'/rz),  0 if rz = 0",
        "8  rz  = rz'",
        "9  P   = q_P(R + P·β)",
    ]
    s.lines(334, 324, rows_l, size=12.5, mono=True, gap=26)
    s.lines(614, 324, rows_r, size=12.5, mono=True, gap=26)
    # loop arrows between blocks
    s.arrow([(254, 338), (316, 338)])
    s.arrow([(588, 168), (760, 168), (760, 266)], color=BLUE, sw=2.2)
    s.text(770, 200, "S", size=15, weight="bold", color=BLUE)
    s.text(770, 220, "(K × N)", size=12, color=INK2)
    s.arrow([(420, 268), (420, 236)], color=VIOLET, sw=2.2)
    s.text(430, 258, "P", size=15, weight="bold", color=VIOLET)
    s.arrow([(254, 172), (316, 172)])
    s.text(270, 164, "A", size=13, weight="bold", anchor="middle", color=INK2)
    # output
    s.rect(960, 268, 196, 180, fill="#ffffff", stroke=LINE)
    s.text(974, 294, "After nit iterations", size=14, weight="bold")
    s.lines(
        974,
        320,
        [
            "X  (K × N estimates)",
            "÷ μ  (MMSE unbiasing)",
            "QAM slicer → bits",
            "→ BER at 1e-3",
        ],
        size=12.5,
        color=INK2,
    )
    s.arrow([(908, 358), (958, 358)])
    # notes
    s.rect(24, 482, 1132, 62, fill=NEUTRAL, stroke=NEUTRAL)
    s.text(
        40,
        506,
        "Exact arithmetic between registers; the only rounding is the assignment "
        "q_V (ap_fixed AP_RND, AP_SAT).  Vectors are W bits wide; the two quadratic forms",
        size=13,
        color=INK2,
    )
    s.text(
        40,
        528,
        "ps and rz get g_s guard bits.  Python golden: mm_step (step 1), vec_step "
        "(steps 2–9), cg_init — cg_fixed composes them, and the hardware splits the same way.",
        size=13,
        color=INK2,
    )
    return s


# --- 3. the verification ladder ---------------------------------------------------------


DIAGRAMS = {
    "diagram_cg_iteration.svg": cg_iteration,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in DIAGRAMS.items():
        (OUT / name).write_text(fn().render(), encoding="utf-8")
        print(f"wrote {OUT / name}")


if __name__ == "__main__":
    main()
