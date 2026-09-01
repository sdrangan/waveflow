#!/usr/bin/env bash
# Regenerate data/input.txt and the golden from the shipped Vitis BLAS library.
#
# The golden comes from instantiating xf::blas::gemv itself, never a reimplementation.  Compiles
# natively -- no Vitis run needed.
#
#   VITIS_INC=... BLAS_INC=... ./regen_golden.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

VITIS_INC="${VITIS_INC:-/tools/Xilinx/2025.1/Vitis/include}"
BLAS_INC="${BLAS_INC:-/home/marco/AmirProjects/Vitis_Libraries_2025.1/blas/L1/include/hw}"
PY="${PY:-$ROOT/../env/bin/python}"

[ -f "$VITIS_INC/hls_stream.h" ] || { echo "hls_stream.h not under VITIS_INC=$VITIS_INC" >&2; exit 1; }
[ -f "$BLAS_INC/xf_blas.hpp" ]   || { echo "xf_blas.hpp not under BLAS_INC=$BLAS_INC" >&2; exit 1; }

"$PY" "$HERE/gen_input.py"

BIN="$(mktemp -d)/dump_gemv"
g++ -std=c++14 -O0 -I"$VITIS_INC" -I"$BLAS_INC" -I"$BLAS_INC/xf_blas" \
    -o "$BIN" "$ROOT/cpp/dump_gemv.cpp"
OUT="$ROOT/golden/gemv_f32_M4_N64_P4.txt"
"$BIN" "$ROOT/data/input.txt" "$OUT" | grep -v "HLS SIM" || true
echo "wrote $OUT"
