---
title: Flow figure sources
nav_exclude: true
search_exclude: true
summary: "The flow diagrams are TikZ source compiled to cropped SVG, and both are committed. The SVG is deterministic output of the .tex, so a diagram cannot drift from its source the way a hand-exported image does."
---

# Flow figures — TikZ source → committed SVG

Same convention as [`docs/overview/figures/`](../../../overview/figures/README.md): the TikZ (`*.tex`)
is the source, `render.sh` compiles it to a cropped `*.svg`, and **both are committed**. The `.svg` is
deterministic output of the `.tex`, so the diagram cannot drift from its source the way a
hand-exported image does.

| Source | Output (embedded in) | Shows |
|---|---|---|
| `design_cut.tex` | `design_cut.svg` (`../modules.md`) | the same three modules under two cuts — the cut is a build choice, not a class fact |

Render, then commit both files:

```bash
bash render.sh
```

Toolchain: MiKTeX (`pdflatex` + `dvisvgm`). `render.sh` renders every standalone `<name>.tex` in this
directory to `<name>.svg` and removes the aux files.
