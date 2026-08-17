#!/usr/bin/env bash
# One script: synthesize all eight variants, simulate the ones that built, print the tables.
#
# Waveflow-independent by construction — it calls the Vitis and Vivado binaries directly.  A witness
# that went through our toolchain wrapper could not settle a question about Vitis, because a wrapper
# bug and a synthesis result would look the same.
#
#   ./run.sh            everything
#   ./run.sh synth      csynth only
#   ./run.sh sim        RTL only (needs a previous synth)
#   ./run.sh report     re-print the tables from reports already on disk
#
# Tool discovery: set WITNESS_VITIS / WITNESS_VIVADO to the bin directories to override.
# Existence is tested with -f, not -x: under Git Bash the Windows .bat wrappers carry no
# execute bit, so -x reports "missing" for tools that are sitting right there.
set -u

cd "$(dirname "$0")"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) EXT=".bat"; DEFV="/c/Xilinx/2025.1" ;;
  *)                    EXT="";     DEFV="/tools/Xilinx/2025.1" ;;
esac
VITIS_BIN="${WITNESS_VITIS:-$DEFV/Vitis/bin}"
VIVADO_BIN="${WITNESS_VIVADO:-$DEFV/Vivado/bin}"
VITIS_RUN="$VITIS_BIN/vitis-run$EXT"
XVLOG="$VIVADO_BIN/xvlog$EXT"; XELAB="$VIVADO_BIN/xelab$EXT"; XSIM="$VIVADO_BIN/xsim$EXT"
PY="${WITNESS_PYTHON:-python}"

VARIANTS="ing_1 ing_n8 ing_n64 ing_w ply_1 ply_n8 ply_n64 ply_w"
STEP="${1:-all}"

# --- synth -----------------------------------------------------------------------------------
if [ "$STEP" = all ] || [ "$STEP" = synth ]; then
  [ -f "$VITIS_RUN" ] || { echo "no vitis-run at $VITIS_RUN (set WITNESS_VITIS)"; exit 1; }
  echo "=== csynth: 8 variants ==="
  "$VITIS_RUN" --mode hls --tcl run_hls.tcl 2>&1 | tee csynth.log | grep -E "^WITNESS-RESULT"
fi

# --- sim -------------------------------------------------------------------------------------
# Each variant is elaborated on its OWN, with its own xsim library, so a stale snapshot from
# another variant cannot be mistaken for this one's result.
if [ "$STEP" = all ] || [ "$STEP" = sim ]; then
  [ -f "$XVLOG" ] || { echo "no xvlog at $XVLOG (set WITNESS_VIVADO)"; exit 1; }
  rm -f sim.log; : > sim.log
  for v in $VARIANTS; do
    RTL="proj_$v/sol1/syn/verilog"
    if [ ! -d "$RTL" ]; then
      echo "=== $v: NO RTL (refused at csynth) ===" | tee -a sim.log
      continue
    fi
    case "$v" in ing_*) TB=tb_ingress.v ;; *) TB=tb_player.v ;; esac
    echo "=== $v ($TB) ===" | tee -a sim.log
    rm -rf "xsim_$v"; mkdir -p "xsim_$v"
    # The kernel is selected by a GENERATED define file, not by `xvlog -d KERNEL=<v>`.
    # On Windows the .bat wrapper splits arguments on `=`, so `-d KERNEL=ing_1` reaches xvlog as two
    # words and it goes looking for a file called `ing_1`.  Macros persist across the files of one
    # xvlog invocation, so compiling a one-line define file first does the same job portably.
    printf '`define KERNEL %s\n' "$v" > "xsim_$v/kdef.v"
    ( cd "xsim_$v" \
      && "$XVLOG" kdef.v "../$TB" ../$RTL/*.v > xvlog.log 2>&1 \
      && "$XELAB" tb -s tbsim --debug off > xelab.log 2>&1 \
      && "$XSIM" tbsim -runall > xsim.log 2>&1 ) \
      && grep -E "^(KERNEL|BEATS|THROUGHPUT|GAPS_OF_1|BOUNDARY_GAP|BRAM_WRITES|RAMP_ERRORS|READS_DURING_STALL|VERDICT_|RESUMED_BEATS|TB-)" \
              "xsim_$v/xsim.log" 2>/dev/null | tee -a sim.log \
      || { echo "  ELABORATION OR RUN FAILED — see xsim_$v/*.log" | tee -a sim.log
           tail -5 "xsim_$v/xvlog.log" "xsim_$v/xelab.log" 2>/dev/null | tee -a sim.log; }
  done
fi

# --- report ----------------------------------------------------------------------------------
if [ "$STEP" = all ] || [ "$STEP" = report ] || [ "$STEP" = sim ]; then
  echo
  "$PY" report.py
fi
