#!/usr/bin/env bash
# Regenerate every file in ../golden/ from the Vitis DSP L1 SSR FFT library.
#
# The goldens are produced by instantiating the VENDOR's own templates -- never a
# reimplementation -- and dumping raw stored integers.  Everything compiles natively, so a
# regeneration takes milliseconds rather than a Vitis run.
#
# The library headers do NOT need a Vitis_Libraries checkout: Vitis ships a copy under
# `tps/xf_dsp/`, verified code-identical to the upstream `v2025.1_re` tag across all 45 L1
# headers (only the copyright-line glyphs differ).  Override VLIB to use a checkout instead.
#
#   VITIS_INC=... VLIB=... ./regen_golden.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"          # tests/vitis_l1/fft
OVERLAY="$HERE/overlay"

# `-std=gnu++14`, not `c++14`: the vendor's twiddle table uses M_PI, which strict ISO mode hides
# behind _USE_MATH_DEFINES on mingw.  Harmless on Linux, required on Windows.
STD="${STD:--std=gnu++14 -D_USE_MATH_DEFINES}"
CXX="${CXX:-g++}"

_first_dir() { for d in "$@"; do [ -d "$d" ] && { echo "$d"; return; }; done; }
_first_file_dir() { for d in "$@"; do [ -f "$d/ap_fixed.h" ] && { echo "$d"; return; }; done; }

VITIS_INC="${VITIS_INC:-$(_first_file_dir \
    /tools/Xilinx/2025.1/Vitis/include \
    /c/Xilinx/2025.1/Vitis/include \
    /opt/Xilinx/2025.1/Vitis/include)}"
VLIB="${VLIB:-$(_first_dir \
    /tools/Xilinx/2025.1/tps/xf_dsp/L1/include/hw/vitis_fft/fixed \
    /c/Xilinx/2025.1/tps/xf_dsp/L1/include/hw/vitis_fft/fixed \
    /opt/Xilinx/2025.1/tps/xf_dsp/L1/include/hw/vitis_fft/fixed)}"

[ -n "${VITIS_INC:-}" ] && [ -f "$VITIS_INC/ap_fixed.h" ] || { echo "set VITIS_INC (no ap_fixed.h found)" >&2; exit 1; }
[ -n "${VLIB:-}" ] && [ -d "$VLIB/vitis_fft" ]            || { echo "set VLIB (no vitis_fft/ found)" >&2; exit 1; }
echo "VITIS_INC=$VITIS_INC"; echo "VLIB=$VLIB"

TMP="$(mktemp -d)"
build() {  # build <name> [extra flags...]
    local n="$1"; shift
    $CXX $STD -O0 "$@" -I"$VITIS_INC" -I"$VLIB" -o "$TMP/$n" "$HERE/$n.cpp"
}

# ---- builds against the PRISTINE library ----------------------------------------------------
build dump_twiddle;  "$TMP/dump_twiddle" > "$ROOT/golden/twiddle_L16_R4_W18_I2.json"
echo "wrote golden/twiddle_L16_R4_W18_I2.json"
build dump_fft;      "$TMP/dump_fft"     "$ROOT/golden/fft_L16_R4_noscale_natural.json" >/dev/null
echo "wrote golden/fft_L16_R4_noscale_natural.json"
build dump_fft_l64;  "$TMP/dump_fft_l64" "$ROOT/golden/fft_L64_R4_noscale_natural.json" >/dev/null
echo "wrote golden/fft_L64_R4_noscale_natural.json"
build dump_cxops;    "$TMP/dump_cxops"   "$ROOT/golden/cxops_d16_2_t18_2.json" >/dev/null
echo "wrote golden/cxops_d16_2_t18_2.json"

# ---- builds needing the instrumented overlay ------------------------------------------------
# overlay/ carries only `wf_trace.hpp` plus the THREE vendor headers that take WF_TRACE calls;
# placed ahead of $VLIB on the include path it shadows just those three.  Without -DWF_FFT_TRACE
# every WF_TRACE expands to `do {} while (0)`, so the overlay is behaviourally the vendor tree.
build dump_modes -DWF_FFT_TRACE -I"$OVERLAY"
"$TMP/dump_modes" "$ROOT/golden/fft_L16_R4_modes.json" >/dev/null
echo "wrote golden/fft_L16_R4_modes.json"

# dump_stages is a debugging aid -- it writes no golden, it prints per-stage intermediates.
build dump_stages -DWF_FFT_TRACE -I"$OVERLAY"
echo "built dump_stages (diagnostic; writes no golden) -> $TMP/dump_stages"
