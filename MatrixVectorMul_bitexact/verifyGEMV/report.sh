#!/usr/bin/env bash
# Print the synthesis numbers for every DUT, straight from the csynth reports.
# Used to keep README.md's resource table honest; run after run.tcl.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf "%-9s %-8s %-6s %-5s %-6s %-7s\n" DUT latency II DSP FF LUT
for d in f32 f32_wide f32_pad f64 ab i32 u32 fixed fix24; do
    f=$(ls "$HERE/${d}_proj/solution1/syn/report/gemv_"*"_top_csynth.rpt" 2>/dev/null | head -1)
    [ -f "$f" ] || { printf "%-9s (not built)\n" "$d"; continue; }
    lat=$(sed -n '/+ Latency:/,/^$/p' "$f" | sed -n '7p' | awk -F'|' '{gsub(/ /,"");print $2" "$6}')
    util=$(sed -n '/== Utilization Estimates/,/^$/p' "$f" | grep -E '^\|Total' | head -1 \
           | awk -F'|' '{gsub(/ /,"");print $4" "$5" "$6}')
    printf "%-9s %s %s\n" "$d" "$(printf '%-8s %-6s' $lat)" "$(printf '%-5s %-6s %-7s' $util)"
done
