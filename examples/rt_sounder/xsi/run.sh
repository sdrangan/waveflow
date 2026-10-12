#!/usr/bin/env bash
# run.sh <top> <tb_basename> [trace] [all|build|rtl|tb|run] [vectors_dir] — XSI flow
# (xvlog -> xelab -dll -> g++ BFM -> run) for a generated free-running mem-stream kernel.  Linux
# sibling of run.bat; same arguments, same artifacts, same stdout markers, so
# waveflow.build.trace_steps and the -m xsi tests can drive either one.
#   ./run.sh mem_r_stream mem_r_bfm_tb
#   ./run.sh mem_w_stream mem_w_bfm_tb
#
# Pass `trace` to also elaborate vcd_dumper_<top>.v as a SECOND top, whose $dumpvars writes
# <top>_trace.vcd.  The XSI top -- and so every BFM port number -- is untouched, so the cycle counts
# are identical either way; only the dump is added.  The dumper is per-top because one xsi/
# directory can serve several (examples/interleaver/xsi builds three), and a dumper naming a scope
# that is not part of THIS elaboration is a hard error.  The traced design is its own snapshot,
# xsim.dir/<top>_trace, so a traced and an untraced build coexist; the run phase points the
# testbench at it through WF_XSI_DESIGN (xsi_bfm.h).
#   ./run.sh mem_copy mem_copy_bfm_tb trace
#
# The verb picks the phases (plans/incremental_xsi.md); the default, `all`, is every phase:
#   rtl    compile the RTL and elaborate the snapshot
#   tb     compile and link the testbench
#   build  rtl + tb
#   run    run the testbench that is already built, against the snapshot already elaborated
#   all    build + run
# Any other argument is the VECTORS DIRECTORY of a run: the testbench's "vectors/..." bundles are
# read from and written to it instead (WF_VECTORS_DIR, xsi_bundle.h), so two runs of one snapshot
# need not overwrite each other's outputs.
#   ./run.sh mem_copy mem_copy_bfm_tb run runs/p3
#
# Nothing here decides whether a phase is needed: waveflow.build.xsi_snapshot.XsiSnapshot does, from
# content stamps it writes after a successful phase.  Each phase deletes its own outputs -- and the
# stamp that vouches for them -- before it rebuilds, so a failed build cannot leave an old artifact
# that a stamp still describes.
#
# Differences from run.bat, all forced by the platform:
#   - the design library is xsimk.so, not xsimk.dll (xelab -dll emits the native form);
#   - the simulation kernel is libxv_simulator_kernel.so, found via LD_LIBRARY_PATH rather
#     than PATH, and the BFM links -ldl for it;
#   - the compiler is the system g++, not the mingw g++ bundled with Vivado.

set -o pipefail
cd "$(dirname "$0")" || exit 1

TOP="$1"
TB="$2"

if [ -z "$TOP" ] || [ -z "$TB" ]; then
    echo "usage: run.sh <top> <tb_basename> [trace] [all|build|rtl|tb|run] [vectors_dir]" >&2
    exit 2
fi
shift 2

TRACE=""
VERB="all"
VEC=""
for a in "$@"; do
    case "${a,,}" in
        trace) TRACE="trace" ;;
        all|build|rtl|tb|run) VERB="${a,,}" ;;
        *) VEC="$a" ;;
    esac
done
SNAP="$TOP"
[ -n "$TRACE" ] && SNAP="${TOP}_trace"
DO_RTL=""; DO_TB=""; DO_RUN=""
case "$VERB" in
    all) DO_RTL=1; DO_TB=1; DO_RUN=1 ;;
    build) DO_RTL=1; DO_TB=1 ;;
    rtl) DO_RTL=1 ;;
    tb) DO_TB=1 ;;
    run) DO_RUN=1 ;;
esac

# Vivado root: an explicit VIV wins, then the standard XILINX_VIVADO, then whatever `vivado`
# resolves to on PATH (what an environment module provides).  run.bat hardcodes a path; here the
# install is discovered so the script survives a toolchain upgrade.
if [ -z "$VIV" ]; then
    if [ -n "$XILINX_VIVADO" ]; then
        VIV="$XILINX_VIVADO"
    elif command -v vivado >/dev/null 2>&1; then
        VIV="$(cd "$(dirname "$(command -v vivado)")/.." && pwd)"
    fi
fi
if [ -z "$VIV" ] || [ ! -x "$VIV/bin/xelab" ]; then
    echo "run.sh: cannot locate Vivado. Set VIV=/path/to/Vivado, or put vivado on PATH." >&2
    exit 2
fi

export LD_LIBRARY_PATH="$PWD/xsim.dir/$SNAP:$VIV/lib/lnx64.o:$LD_LIBRARY_PATH"
export PATH="$VIV/bin:$PATH"

# WF_PHASE lines time each phase (waveflow.build.trace_steps.run_xsi).
if [ -n "$DO_RTL" ]; then
    echo "WF_PHASE compile_rtl $(date +%s.%N)"
    echo "--- xvlog RTL ($TOP) ---"
    "$VIV/bin/xvlog" -f "rtl_$TOP.f"
    echo "xvlog errorlevel=$?"
    echo "WF_PHASE elaborate $(date +%s.%N)"
    rm -rf "xsim.dir/$SNAP"
    if [ -n "$TRACE" ]; then
        echo "--- xvlog vcd_dumper_$TOP ---"
        "$VIV/bin/xvlog" "vcd_dumper_$TOP.v"
        echo "--- xelab -dll [+ vcd_dumper_$TOP] ---"
        "$VIV/bin/xelab" "work.$TOP" "work.vcd_dumper_$TOP" -dll -s "$SNAP" -debug typical
    else
        echo "--- xelab -dll ---"
        "$VIV/bin/xelab" "work.$TOP" -dll -s "$SNAP" -debug typical
    fi
    echo "xelab errorlevel=$?"
fi

if [ -n "$DO_TB" ]; then
    echo "WF_PHASE compile_tb $(date +%s.%N)"
    echo "--- g++ BFM tb ($TB) ---"
    rm -f "$TB.bin" "$TB.o" "$TB.wf_stamp.json"
    g++ -I"$VIV/data/xsim/include" -O3 -c -o xsi_loader.o xsi_loader.cpp
    # WF_TB_CXXFLAGS: extra flags for the testbench only (see run.bat); unset, nothing.
    g++ -I"$VIV/data/xsim/include" ${WF_TB_CXXFLAGS:-} -O3 -c -o "$TB.o" "$TB.cpp"
    g++ -o "$TB.bin" "$TB.o" xsi_loader.o -ldl
    echo "gpp errorlevel=$?"
fi

if [ -n "$DO_RUN" ]; then
    unset WF_VECTORS_DIR WF_XSI_DESIGN
    [ -n "$VEC" ] && export WF_VECTORS_DIR="$VEC"
    [ -n "$TRACE" ] && export WF_XSI_DESIGN="xsim.dir/$SNAP/xsimk.so"
    echo "WF_PHASE simulate $(date +%s.%N)"
    echo "--- run ---"
    "./$TB.bin"
    echo "XSI_EXITCODE=$?"
fi
echo "WF_PHASE end $(date +%s.%N)"
