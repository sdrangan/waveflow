#!/usr/bin/env bash
# Stage 1 witnesses 1-4 (plans/vecunit_new.md): csynth each variant of the execute prototype, at most
# JOBS at a time, then print the table (report.py). Waveflow-independent: the Vitis binary directly.
#
#   ./run.sh OUT_DIR                 every variant
#   ./run.sh OUT_DIR w2_dep w2_nodep only these
#   COSIM=1 ./run.sh OUT_DIR w2_dep  csynth, then C/RTL co-simulation with tb.cpp
#
# Tool: set WITNESS_VITIS to the Vitis bin directory (default: 2024.1 at /tools/Xilinx).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: run.sh OUT_DIR [variant ...]}
shift
VITIS_RUN=${WITNESS_VITIS:-/tools/Xilinx/Vitis/2024.1/bin}/vitis-run
JOBS=${JOBS:-4}

declare -A CF=(
  [w1_ng8_nb4]="-DVX_NG=8 -DVX_NB=4 -DVX_DEP_FALSE"
  [w1_ng8_nb8]="-DVX_NG=8 -DVX_NB=8 -DVX_DEP_FALSE"
  [w1_ng16_nb4]="-DVX_NG=16 -DVX_NB=4 -DVX_DEP_FALSE"
  [w1_ng16_nb16]="-DVX_NG=16 -DVX_NB=16 -DVX_DEP_FALSE"
  [w1_ng32_nb4]="-DVX_NG=32 -DVX_NB=4 -DVX_DEP_FALSE"
  [w1_ng32_nb32]="-DVX_NG=32 -DVX_NB=32 -DVX_DEP_FALSE"
  [w2_nodep]="-DVX_NG=32 -DVX_NB=4"
  [w4_f1]="-DVX_NG=32 -DVX_NB=4 -DVX_NVF=1 -DVX_DEP_FALSE"
  [w4_qshared]="-DVX_NG=32 -DVX_NB=4 -DVX_QSHARED -DVX_DEP_FALSE"
  [w2_cosim]="-DVX_NG=32 -DVX_NB=4 -DVX_QSHARED -DVX_DEP_FALSE"
  [w5]=""
  [w5_cosim]=""
  [w5b_cosim]=""
  [w5c_cosim]=""
  [w5d_cosim]=""
)
ALL="w1_ng8_nb4 w1_ng8_nb8 w1_ng16_nb4 w1_ng16_nb16 w1_ng32_nb4 w1_ng32_nb32 w2_nodep w4_f1 w4_qshared w5"
VARIANTS=${*:-$ALL}

run_one() {
  local v=$1 d="$OUT/$1" top=vexec_top src=vexec_top.cpp tb=tb.cpp
  case "$v" in w5b*) top=w5_top; src=w5b.cpp; tb=tb5.cpp ;; w5c*) top=w5_top; src=w5c.cpp; tb=tb5.cpp ;; w5d*) top=w5_top; src=w5d.cpp; tb=tb5.cpp ;; w5*) top=w5_top; src=w5.cpp; tb=tb5.cpp ;; esac
  mkdir -p "$d"
  {
    echo "open_project -reset p"
    echo "set_top $top"
    echo "add_files $HERE/$src -cflags \"-I$HERE ${CF[$v]}\""
    echo "add_files -tb $HERE/$tb -cflags \"-I$HERE ${CF[$v]}\""
    echo "open_solution -reset s"
    echo "set_part xczu48dr-ffvg1517-2-e"
    echo "create_clock -period 4"
    echo "csynth_design"
    [ "${COSIM:-0}" = 1 ] && echo "cosim_design"
    echo "exit"
  } > "$d/run.tcl"
  ( cd "$d" && /usr/bin/time -v "$VITIS_RUN" --mode hls --tcl run.tcl > run.log 2> time.log )
  echo "done $v"
}

n=0
for v in $VARIANTS; do
  run_one "$v" &
  n=$((n + 1))
  if [ $n -ge "$JOBS" ]; then wait -n; n=$((n - 1)); fi
done
wait
python3 "$HERE/report.py" "$OUT" $VARIANTS
