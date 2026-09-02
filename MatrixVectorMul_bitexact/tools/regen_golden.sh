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
"$PY" "$HERE/gen_input_fixed.py"

BIN="$(mktemp -d)/dump_gemv"
g++ -std=c++14 -O0 -I"$VITIS_INC" -I"$BLAS_INC" -I"$BLAS_INC/xf_blas" \
    -o "$BIN" "$ROOT/cpp/dump_gemv.cpp"
# non-float path (dot_dsp) -- its own generator, decimal values rather than bit patterns
BIN_INT="$(mktemp -d)/dump_gemv_int"
g++ -std=c++14 -O0 -I"$VITIS_INC" -I"$BLAS_INC" -I"$BLAS_INC/xf_blas" \
    -o "$BIN_INT" "$ROOT/cpp/dump_gemv_int.cpp"
"$BIN_INT" "$ROOT/data/input_int_M3_N32.txt" "$ROOT/golden/gemv_int_M3_N32.txt" \
    | grep -v "HLS SIM" || true
echo "wrote $ROOT/golden/gemv_int_M3_N32.txt"

# ap_fixed path (S4) -- the SAME source built twice.  The plain build is the shipped library,
# defect and all; the -DWF_PATCHED build puts cpp/vendor_patched/dotHelper_patched.hpp ahead of
# it, whose include guard suppresses the shipped header.  One line differs between them.  Nothing
# in the vendor tree is edited.
BIN_FIX="$(mktemp -d)/dump_gemv_fixed"
g++ -std=c++14 -O0 -I"$VITIS_INC" -I"$BLAS_INC" -I"$BLAS_INC/xf_blas" \
    -o "$BIN_FIX" "$ROOT/cpp/dump_gemv_fixed.cpp"
BIN_FIX_P="$(mktemp -d)/dump_gemv_fixed_patched"
g++ -std=c++14 -O0 -DWF_PATCHED -I"$ROOT/cpp/vendor_patched" \
    -I"$VITIS_INC" -I"$BLAS_INC" -I"$BLAS_INC/xf_blas" \
    -o "$BIN_FIX_P" "$ROOT/cpp/dump_gemv_fixed.cpp"
for IN in "$ROOT"/data/input_fixed_*.txt; do
    BASE="$(basename "$IN" .txt)"; BASE="${BASE#input_fixed_}"
    "$BIN_FIX"   "$IN" "$ROOT/golden/gemv_fixed_${BASE}_shipped.txt" | grep -v "HLS SIM" || true
    "$BIN_FIX_P" "$IN" "$ROOT/golden/gemv_fixed_${BASE}_patched.txt" | grep -v "HLS SIM" || true
    echo "wrote $ROOT/golden/gemv_fixed_${BASE}_{shipped,patched}.txt"
done

for IN in "$ROOT"/data/input_M*_N*.txt; do
    BASE="$(basename "$IN" .txt)"; BASE="${BASE#input_}"
    OUT="$ROOT/golden/gemv_f32_${BASE}_sweepP.txt"
    "$BIN" "$IN" "$OUT" | grep -v "HLS SIM" || true
    echo "wrote $OUT"
done
