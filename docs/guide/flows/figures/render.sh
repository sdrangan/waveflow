#!/usr/bin/env bash
# Render the flow-guide TikZ sources to committed SVGs — single source (.tex) -> artifact (.svg).
#
#   bash render.sh
#
# Every standalone `<name>.tex` here (one tikzpicture) renders to `<name>.svg`.
# Toolchain: MiKTeX pdflatex + dvisvgm (pdflatex -> PDF -> dvisvgm --pdf -> SVG).
set -euo pipefail
cd "$(dirname "$0")"

shopt -s nullglob
for tex in *.tex; do
  base="${tex%.tex}"
  case "$base" in _*) continue ;; esac
  echo "rendering ${tex} -> ${base}.svg ..."
  pdflatex -interaction=nonstopmode -halt-on-error "$base.tex" >/dev/null
  dvisvgm --pdf -p1 "${base}.pdf" -o "${base}.svg" >/dev/null 2>&1
done

rm -f ./*.aux ./*.log ./*.pdf
echo "done: $(ls *.svg 2>/dev/null | tr '\n' ' ')"
