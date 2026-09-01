#!/usr/bin/env bash
# Regenerate the golden twiddle table from the shipped Vitis DSP library.
#
# The golden is produced by instantiating Vitis's OWN TwiddleTable template (not a
# reimplementation) and dumping raw stored integers.  Compiles natively -- no Vitis run needed.
#
# Override the two paths if your install differs:
#   VITIS_INC=... VLIB=... ./regen_golden.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

VITIS_INC="${VITIS_INC:-/tools/Xilinx/2025.1/Vitis/include}"
VLIB="${VLIB:-/home/marco/AmirProjects/Vitis_Libraries_2025.1/dsp/L1/include/hw/vitis_fft/fixed}"

[ -f "$VITIS_INC/ap_fixed.h" ] || { echo "ap_fixed.h not under VITIS_INC=$VITIS_INC" >&2; exit 1; }
[ -d "$VLIB/vitis_fft" ]       || { echo "vitis_fft/ not under VLIB=$VLIB" >&2; exit 1; }

BIN="$(mktemp -d)/dump_twiddle"
g++ -std=c++14 -O0 -I"$VITIS_INC" -I"$VLIB" -o "$BIN" "$ROOT/cpp/dump_twiddle.cpp"

OUT="$ROOT/golden/twiddle_L16_R4_W18_I2.json"
"$BIN" > "$OUT"
echo "wrote $OUT"

# S2 -- the transform itself
BIN2="$(mktemp -d)/dump_fft"
g++ -std=c++14 -O0 -I"$VITIS_INC" -I"$VLIB" -o "$BIN2" "$ROOT/cpp/dump_fft.cpp"
OUT2="$ROOT/golden/fft_L16_R4_noscale_natural.json"
"$BIN2" "$OUT2" >/dev/null
echo "wrote $OUT2"
