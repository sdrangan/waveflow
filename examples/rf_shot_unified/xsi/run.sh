#!/usr/bin/env bash
# run.sh <top> <tb_basename> [trace] — XSI flow (xvlog -> xelab -dll -> g++ BFM -> run) for a
# generated free-running mem-stream kernel.  Linux sibling of run.bat; same arguments, same
# artifacts, same stdout markers, so waveflow.build.trace_steps and the -m xsi tests can drive
# either one.
#   ./run.sh mem_r_stream mem_r_bfm_tb
#   ./run.sh mem_w_stream mem_w_bfm_tb
#
# Pass a third argument `trace` to also elaborate vcd_dumper_<top>.v as a SECOND top, whose
# $dumpvars writes <top>_trace.vcd.  The XSI top -- and so every BFM port number -- is untouched,
# so the cycle counts are identical either way; only the dump is added.  The dumper is per-top
# because one xsi/ directory can serve several (examples/interleaver/xsi builds three), and a
# dumper naming a scope that is not part of THIS elaboration is a hard error.
#   ./run.sh mem_copy mem_copy_bfm_tb trace
#
# Note: re-running the built binary does NOT regenerate the VCD -- only this script does, because
# the dump comes from the elaborated snapshot.  See waveflow.build.trace_steps.RtlSimStep.
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
TRACE="$3"

if [ -z "$TOP" ] || [ -z "$TB" ]; then
    echo "usage: run.sh <top> <tb_basename> [trace]" >&2
    exit 2
fi

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

export LD_LIBRARY_PATH="$PWD/xsim.dir/$TOP:$VIV/lib/lnx64.o:$LD_LIBRARY_PATH"
export PATH="$VIV/bin:$PATH"

echo "--- xvlog RTL ($TOP) ---"
"$VIV/bin/xvlog" -f "rtl_$TOP.f"
echo "xvlog errorlevel=$?"

if [ "${TRACE,,}" = "trace" ]; then
    echo "--- xvlog vcd_dumper_$TOP ---"
    "$VIV/bin/xvlog" "vcd_dumper_$TOP.v"
    echo "--- xelab -dll [+ vcd_dumper_$TOP] ---"
    "$VIV/bin/xelab" "work.$TOP" "work.vcd_dumper_$TOP" -dll -s "$TOP" -debug typical
else
    echo "--- xelab -dll ---"
    "$VIV/bin/xelab" "work.$TOP" -dll -s "$TOP" -debug typical
fi
echo "xelab errorlevel=$?"

echo "--- g++ BFM tb ($TB) ---"
g++ -I"$VIV/data/xsim/include" -O3 -c -o xsi_loader.o xsi_loader.cpp
g++ -I"$VIV/data/xsim/include" -O3 -c -o "$TB.o" "$TB.cpp"
g++ -o "$TB.bin" "$TB.o" xsi_loader.o -ldl
echo "gpp errorlevel=$?"

echo "--- run ---"
"./$TB.bin"
echo "XSI_EXITCODE=$?"
