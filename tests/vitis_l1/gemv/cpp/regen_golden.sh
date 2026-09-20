#!/usr/bin/env bash
# Regenerate ../data/ and ../golden/ from the Vitis BLAS L1 library.
#
# The goldens come from instantiating `xf::blas::gemv` itself, never a reimplementation.
# Everything compiles natively -- no Vitis run needed.
#
# UNLIKE the FFT side, this needs a Vitis_Libraries CHECKOUT: Vitis ships `tps/xf_dsp` but no
# `xf_blas`, so there is no installed copy to fall back on.  Get one with:
#
#   git clone --depth 1 --filter=blob:none --sparse https://github.com/Xilinx/Vitis_Libraries
#   cd Vitis_Libraries && git sparse-checkout set blas/L1/include/hw
#   git fetch --depth 1 origin tag v2025.1_re && git checkout v2025.1_re
#
#   VITIS_INC=... BLAS_INC=.../blas/L1/include/hw ./regen_golden.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"          # tests/vitis_l1/gemv

# Measured, not precautionary: `axpy` computes `alpha*x + y` in one expression, and letting the
# compiler contract that into an FMA changes 25 of 576 rows of the alpha/beta golden.  -O0
# happens to suppress it on some hosts, but a host whose default -march has FMA would silently
# produce a different golden -- so the flag is explicit.  Verified to change nothing for the
# other three dumpers (the float dot path has no mul-add in one expression; the rest are integer).
FPFLAGS="${FPFLAGS:--ffp-contract=off}"
# gnu++14 rather than c++14 so M_PI resolves on mingw; harmless on Linux.
STD="${STD:--std=gnu++14 -D_USE_MATH_DEFINES}"
CXX="${CXX:-g++}"
PY="${PY:-python}"

_first_file_dir() { for d in "$@"; do [ -f "$d/hls_stream.h" ] && { echo "$d"; return; }; done; }
VITIS_INC="${VITIS_INC:-$(_first_file_dir \
    /tools/Xilinx/2025.1/Vitis/include \
    /c/Xilinx/2025.1/Vitis/include \
    /opt/Xilinx/2025.1/Vitis/include)}"

[ -n "${VITIS_INC:-}" ] && [ -f "$VITIS_INC/hls_stream.h" ] || { echo "set VITIS_INC (no hls_stream.h found)" >&2; exit 1; }
[ -n "${BLAS_INC:-}" ] && [ -f "$BLAS_INC/xf_blas.hpp" ] || {
    echo "set BLAS_INC to <Vitis_Libraries>/blas/L1/include/hw -- see the header of this script" >&2; exit 1; }
echo "VITIS_INC=$VITIS_INC"; echo "BLAS_INC=$BLAS_INC"

"$PY" "$HERE/gen_input.py"
"$PY" "$HERE/gen_input_fixed.py"
"$PY" "$HERE/gen_input_ab.py"
"$PY" "$HERE/gen_input_f64.py"

TMP="$(mktemp -d)"
build() { local n="$1"; shift; $CXX $STD -O0 $FPFLAGS "$@" \
    -I"$VITIS_INC" -I"$BLAS_INC" -I"$BLAS_INC/xf_blas" -o "$TMP/$n" "$HERE/$n.cpp"; }

build dump_gemv; build dump_gemv_int; build dump_gemv_f64; build dump_gemv_ab
build dump_gemv_fixed
# The SAME source built twice.  The plain build is the shipped library, defect and all; the
# -DWF_PATCHED build puts cpp/vendor_patched/dotHelper_patched.hpp ahead of it, whose include
# guard suppresses the shipped header.  ONE line differs.  Nothing in the vendor tree is edited.
$CXX $STD -O0 $FPFLAGS -DWF_PATCHED -I"$HERE/vendor_patched" \
    -I"$VITIS_INC" -I"$BLAS_INC" -I"$BLAS_INC/xf_blas" \
    -o "$TMP/dump_gemv_fixed_patched" "$HERE/dump_gemv_fixed.cpp"

run() { "$@" | grep -v "HLS SIM" || true; }

run "$TMP/dump_gemv_int" "$ROOT/data/input_int_M3_N32.txt" "$ROOT/golden/gemv_int_M3_N32.txt"
echo "wrote golden/gemv_int_M3_N32.txt"

for IN in "$ROOT"/data/input_f64_M*_N*.txt; do B="$(basename "$IN" .txt)"; B="${B#input_f64_}"
    run "$TMP/dump_gemv_f64" "$IN" "$ROOT/golden/gemv_f64_${B}.txt"; echo "wrote golden/gemv_f64_${B}.txt"; done

for IN in "$ROOT"/data/input_fixed_*.txt; do B="$(basename "$IN" .txt)"; B="${B#input_fixed_}"
    run "$TMP/dump_gemv_fixed"         "$IN" "$ROOT/golden/gemv_fixed_${B}_shipped.txt"
    run "$TMP/dump_gemv_fixed_patched" "$IN" "$ROOT/golden/gemv_fixed_${B}_patched.txt"
    echo "wrote golden/gemv_fixed_${B}_{shipped,patched}.txt"; done

for IN in "$ROOT"/data/input_ab_M*_N*.txt; do B="$(basename "$IN" .txt)"; B="${B#input_ab_}"
    run "$TMP/dump_gemv_ab" "$IN" "$ROOT/golden/gemv_ab_${B}.txt"; echo "wrote golden/gemv_ab_${B}.txt"; done

for IN in "$ROOT"/data/input_M*_N*.txt; do B="$(basename "$IN" .txt)"; B="${B#input_}"
    run "$TMP/dump_gemv" "$IN" "$ROOT/golden/gemv_f32_${B}_sweepP.txt"; echo "wrote golden/gemv_f32_${B}_sweepP.txt"; done
