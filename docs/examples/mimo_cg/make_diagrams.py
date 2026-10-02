"""make_diagrams.py — the concept diagrams of the CG massive-MIMO study, as static SVG.

Writes ``images/diagram_*.svg`` next to this script, for ``index.md`` in this folder.  Run from
the repo root::

    python docs/examples/mimo_cg/make_diagrams.py

Every diagram is drawn from the same few primitives and tokens below (the dataviz reference
palette: blue for compute, orange for memory and data movement, aqua for control, grey ink for
text), on an opaque light surface so they read the same in light and dark viewers and in slides.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent / "images"

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
LINE = "#c3c2b7"
NEUTRAL = "#f0efec"
BLUE, BLUE_T, BLUE_D = "#2a78d6", "#e3eefb", "#1c5cab"
ORANGE, ORANGE_T = "#eb6834", "#fde9e0"
AQUA, AQUA_T = "#1baf7a", "#ddf4ea"
VIOLET, VIOLET_T = "#4a3aa7", "#e9e6f7"
GOOD = "#0ca30c"
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


def study() -> Svg:
    s = Svg(1180, 400)
    title(
        s,
        "The study: one Python model, from link-level accuracy to hardware cost",
        "plans/mimo_cg/mimo_cg_paper_sims.md — phases 0–4 complete (milestones M0–M4 approved)",
    )
    phases = [
        (
            "0",
            "Toolchain",
            ["Vitis / Vivado 2024.1", "XSI RTL flow", "xczu48dr probe"],
            "done",
        ),
        (
            "1",
            "Float link + CG",
            ["M×K Rayleigh uplink", "ZF, MMSE, CG BER", "27 configurations"],
            "done",
        ),
        (
            "2",
            "Bit-exact CG",
            ["ap_fixed golden", "≡ Vitis C-sim", "division + zero guard"],
            "done",
        ),
        (
            "3",
            "Accuracy DSE",
            ["364 points × 21", "formats, no Vitis", "≤ 0.5 dB frontier"],
            "done",
        ),
        (
            "4",
            "Hardware",
            ["vector unit, systolic", "matmul, detector", "4 ns, RTL bit-exact"],
            "done",
        ),
        (
            "5",
            "Performance",
            ["per-block cycle and", "resource models,", "held-out validation"],
            "next",
        ),
        (
            "6",
            "Full DSE",
            ["Pareto: accuracy vs", "DSP / LUT / latency", "+ brute-force check"],
            "planned",
        ),
    ]
    x0, y0, w, h, gap = 24, 92, 148, 196, 18
    for i, (num, name, rows, status) in enumerate(phases):
        x = x0 + i * (w + gap)
        done = status == "done"
        fill = BLUE_T if done else NEUTRAL
        stroke = BLUE if done else LINE
        s.rect(x, y0, w, h, fill=fill, stroke=stroke, dash=None if done else "5 4")
        s.text(x + 14, y0 + 30, f"Phase {num}", size=12.5, color=INK2)
        s.text(x + 14, y0 + 54, name, size=16, weight="bold")
        s.lines(x + 14, y0 + 84, rows, size=12.5, color=INK2)
        color = GOOD if done else (ORANGE if status == "next" else MUTED)
        s.badge(x + 14, y0 + h - 34, "✓ done" if done else status, color)
        if i < len(phases) - 1:
            s.arrow([(x + w + 2, y0 + h / 2), (x + w + gap - 2, y0 + h / 2)])
    # the spine: what flows between phases
    yb = 318
    s.rect(24, yb, 1132, 58, fill="#ffffff", stroke=LINE)
    s.text(40, yb + 24, "One source of truth:", size=13.5, weight="bold")
    s.text(
        178,
        yb + 24,
        "the bit-exact Python model (mimo_cg_fixed.py) defines the detector; every "
        "accuracy number comes from it, and every hardware block",
        size=13.5,
        color=INK2,
    )
    s.text(
        40,
        yb + 44,
        "is proven equal to it — in C-sim, after synthesis, and cycle by cycle at RTL. "
        "Accuracy needs no Vitis; Vitis is spent only where the cost model needs it.",
        size=13.5,
        color=INK2,
    )
    return s


# --- 2. the fixed-point CG iteration and its hardware split ------------------------------


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


def ladder() -> Svg:
    s = Svg(1180, 610)
    title(
        s,
        "One golden, six levels, all bit-exact",
        "each level is checked against the one above it on the same scenarios — never against a tolerance",
    )
    rows = [
        (
            "Python golden",
            (
                "cg_fixed — integer numpy with ap_fixed semantics; splits into "
                "cg_init, mm_step, vec_step"
            ),
            "the reference",
            BLUE_T,
            BLUE,
        ),
        (
            "C++ reference",
            "cpp/cg_ref.h — ap_fixed template of the 9 register steps",
            "Vitis 2024.1 C-sim: 11 case sets, 580,992 words; saturation and zero-residual cases",
            BLUE_T,
            BLUE,
        ),
        (
            "Block models",
            (
                "FreeRunMod Python twins of each task, wired as composites with the "
                "framework mem-streams"
            ),
            "Python sim: every P, S, X word; 51 jobs × 3 formats × K = 4, 8, 16",
            VIOLET_T,
            VIOLET,
        ),
        (
            "HLS task bodies",
            (
                "hand-written C++ (hw/cpp/), framework mem-stream bodies, generated "
                "top"
            ),
            "sequential C-sim: all formats, K = 4, 8, 16, and every knob (L, R, C, widths, depths)",
            VIOLET_T,
            VIOLET,
        ),
        (
            "Synthesized RTL",
            "csynth for xczu48dr-ffvg1517-2-e at 4 ns",
            "estimated clock 3.35–3.39 ns for every unit and the detector at K = 4, 8, 16",
            ORANGE_T,
            ORANGE,
        ),
        (
            "RTL in simulation",
            "XSI: the testbench graph drives the RTL cycle by cycle (BFM)",
            "bit-exact; detector at K = 4 (M = 32), 8, 16, every nit; exact cycle counts",
            ORANGE_T,
            ORANGE,
        ),
    ]
    y, h, gap = 82, 70, 14
    langs = [
        "Python",
        "C++",
        "Python",
        "C++ (Vitis HLS)",
        "Verilog",
        "Verilog + C++ BFM",
    ]
    for i, (name, what, check, fill, stroke) in enumerate(rows):
        yy = y + i * (h + gap)
        s.rect(24, yy, 250, h, fill=fill, stroke=stroke)
        s.text(40, yy + 30, f"{i + 1}  {name}", size=15, weight="bold")
        s.text(40, yy + 52, langs[i], size=12, color=INK2)
        s.rect(290, yy, 866, h, fill="#ffffff", stroke=LINE)
        s.text(306, yy + 28, what, size=13, color=INK)
        s.text(
            306,
            yy + 52,
            ("✓  " if i else "") + check,
            size=13,
            color=GOOD if i else INK2,
            weight="bold" if i else "normal",
        )
        if i < len(rows) - 1:
            s.text(
                149,
                yy + h + gap / 2 + 6,
                "≡",
                size=17,
                weight="bold",
                anchor="middle",
                color=INK2,
            )
    return s


# --- 4. the integrated detector ---------------------------------------------------------


def _task(s: Svg, x, y, w, h, name, sub, kind):
    fill, stroke, col = {
        "glue": ("#ffffff", INK2, INK),
        "mem": (ORANGE_T, ORANGE, INK),
        "ctrl": (AQUA_T, AQUA, INK),
        "mm": (BLUE_T, BLUE, BLUE_D),
        "vec": (VIOLET_T, VIOLET, VIOLET),
    }[kind]
    s.rect(x, y, w, h, fill=fill, stroke=stroke, sw=2 if kind in ("mm", "vec") else 1.5)
    s.text(
        x + w / 2,
        y + (24 if sub else h / 2 + 5),
        name,
        size=14,
        weight="bold",
        anchor="middle",
        color=col,
        mono=True,
    )
    for i, line in enumerate(sub):
        s.text(x + w / 2, y + 44 + 17 * i, line, size=12, anchor="middle", color=INK2)


def detector() -> Svg:
    s = Svg(1200, 610)
    title(
        s,
        "CgDetector: a free-running composite of hls::tasks on xczu48dr",
        "one job = one CgCmd {A, B, X offsets, nit}; jobs pipeline through the tasks; "
        "verified at RTL by an XSI testbench generated from the same graph",
    )
    soB = {"color": ORANGE, "sw": 4.5}
    fifo = {"color": INK2, "sw": 1.6}
    q = {"color": AQUA, "sw": 2.2}
    # tasks
    _task(s, 24, 96, 110, 52, "host", [], "glue")
    _task(s, 168, 96, 140, 52, "cg_cmd_rx", [], "glue")
    _task(s, 340, 96, 160, 52, "MemRStream", [], "mem")
    _task(s, 530, 96, 140, 52, "cg_load", [], "glue")
    _task(s, 750, 96, 140, 52, "cg_ctrl", [], "ctrl")
    _task(s, 730, 208, 190, 82, "cg_mm", ["systolic R × C array", "S = q_S(A·P)"], "mm")
    _task(
        s,
        730,
        360,
        190,
        82,
        "cg_vec",
        ["L lanes, steps 2–9", "holds X, R, P, rz"],
        "vec",
    )
    _task(s, 990, 380, 150, 52, "cg_store", [], "glue")
    _task(s, 990, 470, 150, 52, "MemWStream", [], "mem")
    # DDR
    s.rect(24, 548, 1152, 44, fill=ORANGE_T, stroke=ORANGE, rx=8)
    s.text(
        600,
        576,
        "DDR over AXI-MM (m_axi):  A, B in per job  ·  X out per job",
        size=14,
        weight="bold",
        anchor="middle",
    )
    s.arrow([(420, 150), (420, 546)], color=ORANGE, sw=1.8, dash="6 4")
    s.text(428, 520, "m_in (gmem0)", size=12, color=ORANGE)
    s.arrow([(1065, 524), (1065, 546)], color=ORANGE, sw=1.8, dash="6 4")
    s.text(1072, 540, "m_out (gmem1)", size=12, color=ORANGE)
    # streams
    s.arrow([(134, 122), (166, 122)], **fifo)
    s.text(150, 88, "s_cmd", size=11.5, anchor="middle", color=INK2)
    s.arrow([(308, 122), (338, 122)], **fifo)
    s.arrow([(500, 122), (528, 122)], **fifo)
    s.arrow([(670, 122), (748, 122)], **fifo)
    s.text(709, 114, "desc", size=11.5, anchor="middle", color=INK2)
    # shared memory: stream-of-blocks
    s.arrow([(570, 148), (570, 236), (728, 236)], **soB)
    s.text(578, 228, "a_blk", size=12.5, weight="bold", color=ORANGE)
    s.arrow([(632, 148), (632, 410), (728, 410)], **soB)
    s.text(640, 402, "b_blk", size=12.5, weight="bold", color=ORANGE)
    s.arrow([(790, 358), (790, 292)], **soB)
    s.text(782, 330, "p_blk", size=12.5, weight="bold", anchor="end", color=ORANGE)
    s.arrow([(860, 292), (860, 358)], **soB)
    s.text(868, 330, "s_blk", size=12.5, weight="bold", color=ORANGE)
    s.arrow([(920, 412), (988, 412)], **soB)
    s.text(954, 404, "x_blk", size=12.5, weight="bold", anchor="middle", color=ORANGE)
    # command queues from the control
    s.arrow([(820, 148), (820, 206)], **q)
    s.text(828, 182, "mm_q", size=12, weight="bold", color=AQUA)
    s.arrow([(890, 132), (955, 132), (955, 376), (922, 376)], **q)
    s.text(962, 260, "vec_q", size=12, weight="bold", color=AQUA)
    s.arrow([(890, 112), (1065, 112), (1065, 378)], **fifo)
    s.text(1072, 250, "desc", size=11.5, color=INK2)
    s.arrow([(1065, 432), (1065, 468)], **fifo)
    s.arrow([(1140, 496), (1176, 496), (1176, 98)], **fifo)
    s.text(1170, 90, "s_done → host", size=11.5, anchor="end", color=INK2)
    # results panel
    s.rect(24, 178, 376, 182, fill="#ffffff", stroke=LINE)
    s.text(40, 204, "Synthesized and verified", size=14, weight="bold")
    s.lines(
        40,
        230,
        [
            "csynth for xczu48dr at 4 ns: est. 3.35 ns, K = 4, 8, 16",
            "DSP = 48 (vector unit) + 4·R·C (matmul)",
            "     = 112 / 176 / 304 at K = 4 / 8 / 16",
            "LUT 33k / 42k / 61k,  BRAM_18K 20 / 26 / 63",
            "RTL (XSI): X ≡ cg_fixed at K = 4 (M = 32), 8, 16",
            "K = 4: 20 jobs, 50 iterations in 61,395 cycles",
        ],
        size=12.5,
        color=INK2,
        gap=21,
    )
    # legend
    s.rect(24, 376, 376, 162, fill="#ffffff", stroke=LINE)
    s.text(40, 398, "Legend", size=14, weight="bold")
    s.arrow([(40, 418), (96, 418)], **soB, head=False)
    s.text(
        108,
        423,
        "stream-of-blocks: the shared memory (ping-pong)",
        size=12.5,
        color=INK2,
    )
    s.arrow([(40, 442), (96, 442)], **q, head=False)
    s.text(108, 447, "command queue (FIFO, cmd_depth)", size=12.5, color=INK2)
    s.arrow([(40, 466), (96, 466)], **fifo, head=False)
    s.text(108, 471, "framed stream (FIFO)", size=12.5, color=INK2)
    s.arrow([(40, 490), (96, 490)], color=ORANGE, sw=1.8, dash="6 4", head=False)
    s.text(108, 495, "AXI-MM master (m_axi)", size=12.5, color=INK2)
    s.rect(52, 508, 32, 18, fill=ORANGE_T, stroke=ORANGE, rx=4)
    s.text(108, 522, "framework mem-stream task", size=12.5, color=INK2)
    return s


# --- 5. the systolic matrix multiply -----------------------------------------------------


def systolic() -> Svg:
    s = Svg(1180, 680)
    title(
        s,
        "The matrix-multiply block: an output-stationary systolic array",
        "S = q_S(A·P) for one R × C tile of S; shown R = C = 4 (the default for K = 4: R = K, C = 4)",
    )
    R = C = 4
    pw, ph, gx, gy = 80, 58, 26, 26
    x0, y0 = 300, 300
    slot = 26
    # A feeding from the left, row i delayed i steps
    for i in range(R):
        cy = y0 + i * (ph + gy) + ph / 2
        for k in range(4):
            x = x0 - 34 - slot * (i + k)
            s.rect(x, cy - 11, 22, 22, fill=BLUE_T, stroke=BLUE, rx=4, sw=1.2)
            s.text(x + 11, cy + 5, str(k), size=12, anchor="middle", color=BLUE_D)
        s.arrow([(x0 - 8, cy), (x0 - 2, cy)], color=BLUE, sw=1.6)
    s.text(
        x0 - 34 - slot * 6,
        y0 - 16,
        "A[i][k], row i delayed i steps",
        size=12.5,
        color=BLUE_D,
    )
    # P feeding from the top, column j delayed j steps
    for j in range(C):
        cx = x0 + j * (pw + gx) + pw / 2
        for k in range(4):
            y = y0 - 34 - slot * (j + k)
            s.rect(cx - 11, y, 22, 22, fill=VIOLET_T, stroke=VIOLET, rx=4, sw=1.2)
            s.text(cx, y + 16, str(k), size=12, anchor="middle", color=VIOLET)
        s.arrow([(cx, y0 - 8), (cx, y0 - 2)], color=VIOLET, sw=1.6)
    s.text(372, 128, "P[k][j], column j delayed j steps", size=12.5, color=VIOLET)
    # the PE grid
    for i in range(R):
        for j in range(C):
            x, y = x0 + j * (pw + gx), y0 + i * (ph + gy)
            s.rect(x, y, pw, ph, fill="#ffffff", stroke=INK2, rx=6)
            s.text(
                x + pw / 2,
                y + 22,
                f"PE {i},{j}",
                size=12.5,
                weight="bold",
                anchor="middle",
            )
            s.text(
                x + pw / 2,
                y + 42,
                "acc += a·p",
                size=11.5,
                anchor="middle",
                color=INK2,
                mono=True,
            )
            if j < C - 1:
                s.arrow(
                    [(x + pw + 2, y + ph / 2 - 8), (x + pw + gx - 2, y + ph / 2 - 8)],
                    color=BLUE,
                    sw=1.4,
                )
            if i < R - 1:
                s.arrow(
                    [(x + pw / 2 + 14, y + ph + 2), (x + pw / 2 + 14, y + ph + gy - 2)],
                    color=VIOLET,
                    sw=1.4,
                )
    gy_end = y0 + R * (ph + gy) - gy
    s.text(
        x0 + (C * (pw + gx) - gx) / 2,
        gy_end + 28,
        "after K steps: S[i][j] = q_S(acc), "
        "one rounding, packed into lane groups on s_blk",
        size=12.5,
        anchor="middle",
        color=INK2,
    )
    # right panel
    px = 790
    s.rect(px, 86, 366, 560, fill="#ffffff", stroke=LINE)
    s.text(px + 16, 112, "Each processing element", size=15, weight="bold")
    s.lines(
        px + 16,
        138,
        [
            "At step t, PE (i, j) meets A[i][k] and P[k][j]",
            "for k = t − i − j (zeros outside 0 ≤ k < K, so",
            "no valid flag); it passes a right and p down.",
            "acc is exact (mm_ap_t): a sum of K full",
            "products, rounded once to the S format.",
        ],
        size=12.5,
        color=INK2,
        gap=19,
    )
    s.text(px + 16, 250, "Complex product: the cmul knob", size=15, weight="bold")
    s.text(
        px + 16, 276, "cmul = 4  (4 DSP per PE)", size=12.5, weight="bold", color=BLUE_D
    )
    s.lines(
        px + 16,
        296,
        ["re = ar·pr − ai·pi", "im = ar·pi + ai·pr"],
        size=12,
        mono=True,
        gap=18,
    )
    s.text(
        px + 16,
        346,
        "cmul = 3, Gauss  (3 DSP per PE)",
        size=12.5,
        weight="bold",
        color=BLUE_D,
    )
    s.lines(
        px + 16,
        366,
        [
            "k1 = pr·(ar + ai)",
            "k2 = ar·(pi − pr)",
            "k3 = ai·(pr + pi)",
            "re = k1 − k3,  im = k1 + k2",
        ],
        size=12,
        mono=True,
        gap=18,
    )
    s.text(
        px + 16,
        448,
        "the same exact value, so bit-exact too",
        size=12.5,
        color=GOOD,
        weight="bold",
    )
    s.text(px + 16, 484, "Tiling and cost", size=15, weight="bold")
    s.lines(
        px + 16,
        510,
        [
            "S (K × N) in (K/R)·(N/C) tiles; R | K, L | C",
            "DSP = cmul · R · C:",
            "  cmul 4: 64 / 128 / 256 at K = 4 / 8 / 16",
            "  cmul 3: 48 / 96 at K = 4 / 8 (−25%)",
            "est. clock 3.35 ns (cmul 4), 3.39 ns (cmul 3)",
        ],
        size=12.5,
        color=INK2,
        gap=19,
    )
    return s


# --- 6. the deadlock found at RTL --------------------------------------------------------


def deadlock() -> Svg:
    s = Svg(1180, 380)
    title(
        s,
        "Found at RTL: reads and writes per job must balance",
        "HLS feeds both m_axi pointer arguments through FIFOs that one process (entry_proc) "
        "fills in lockstep",
    )
    _task(
        s,
        24,
        140,
        150,
        100,
        "entry_proc",
        ["(HLS-generated)", "pushes m_in, m_out"],
        "glue",
    )
    # m_in_c: depth 3, empty
    s.text(214, 118, "m_in_c (depth 3)", size=12.5, weight="bold", color=INK2)
    for k in range(3):
        s.rect(214 + 34 * k, 128, 28, 28, fill="#ffffff", stroke=INK2, rx=3)
    s.text(214, 174, "empty: the reader has no token", size=12, color=MUTED)
    # m_out_c: depth 7, full
    s.text(214, 236, "m_out_c (depth 7)", size=12.5, weight="bold", color=INK2)
    for k in range(7):
        s.rect(214 + 34 * k, 246, 28, 28, fill=ORANGE, stroke=ORANGE, rx=3)
    s.text(452, 236, "full", size=12.5, weight="bold", anchor="end", color=ORANGE)
    s.arrow([(174, 170), (210, 142)], color=INK2)
    s.arrow([(174, 210), (210, 260)], color=INK2)
    _task(s, 520, 116, 170, 52, "MemRStream", [], "mem")
    _task(s, 520, 234, 170, 52, "MemWStream", [], "mem")
    s.arrow([(316, 142), (518, 142)], color=INK2, dash="5 4")
    s.arrow([(458, 260), (518, 260)], color=INK2)
    s.text(
        605,
        190,
        "reader starves:",
        size=12.5,
        anchor="middle",
        color=ORANGE,
        weight="bold",
    )
    s.text(605, 208, "waits for an m_in token", size=12.5, anchor="middle", color=INK2)
    s.rect(24, 300, 666, 56, fill=NEUTRAL, stroke=NEUTRAL)
    s.text(
        40,
        324,
        "Every task then waits on the reader, so the RTL deadlocks: exactly 6 jobs "
        "completed, whatever the data.",
        size=12.5,
        color=INK2,
    )
    s.text(
        40,
        344,
        "Found with a VCD trace of the top's channels (run.sh … trace).",
        size=12.5,
        color=INK2,
    )
    # cause and fix
    px = 720
    s.rect(px, 86, 436, 270, fill="#ffffff", stroke=LINE)
    s.text(px + 16, 112, "Cause", size=15, weight="bold")
    s.lines(
        px + 16,
        136,
        [
            "Each mem-stream firing takes one pointer token.",
            "The matmul unit read nit + 1 times per job (A, P)",
            "but wrote nit times (S): one m_out token was left",
            "over per job, until m_out_c filled.",
        ],
        size=12.5,
        color=INK2,
        gap=19,
    )
    s.text(
        px + 16, 232, "Fix (no framework change)", size=15, weight="bold", color=GOOD
    )
    s.lines(
        px + 16,
        256,
        [
            "A final zero-length write carries the done echo,",
            "so writes = reads per job, in every composite.",
            "Bit-exact at RTL for any number of jobs.",
            "Framework note: unbalanced composites are exposed.",
        ],
        size=12.5,
        color=INK2,
        gap=19,
    )
    return s


DIAGRAMS = {
    "diagram_systolic.svg": systolic,
    "diagram_deadlock.svg": deadlock,
    "diagram_detector.svg": detector,
    "diagram_study.svg": study,
    "diagram_cg_iteration.svg": cg_iteration,
    "diagram_verification.svg": ladder,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in DIAGRAMS.items():
        (OUT / name).write_text(fn().render(), encoding="utf-8")
        print(f"wrote {OUT / name}")


if __name__ == "__main__":
    main()
