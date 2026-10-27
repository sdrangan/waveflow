#!/usr/bin/env bash
# Witness 5 at RTL: run tb5.v (xsim) against each synthesized variant's Verilog, each elaborated on its
# own, after ./run.sh has synthesized them:  ./run_rtl5.sh OUT_DIR w5 w5b_cosim w5c_cosim w5d_cosim
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: run_rtl5.sh OUT_DIR variant ...}
shift
BIN=${WITNESS_VIVADO:-/tools/Xilinx/Vivado/2024.1/bin}
for v in "$@"; do
  RTL="$OUT/$v/p/s/syn/verilog"
  [ -d "$RTL" ] || { echo "$v: NO RTL"; continue; }
  d="$OUT/$v/xsim5"; rm -rf "$d"; mkdir -p "$d"
  ( cd "$d" && "$BIN/xvlog" "$HERE/tb5.v" "$RTL"/*.v > xvlog.log 2>&1 \
      && "$BIN/xelab" tb -s tbsim --debug off > xelab.log 2>&1 \
      && "$BIN/xsim" tbsim -runall > xsim.log 2>&1 ) \
    && echo "$v: $(grep -E '^(WITNESS-RTL5|VERDICT_)' "$d/xsim.log" | tr '\n' ' ')" \
    || { echo "$v: ELABORATION OR RUN FAILED"; tail -5 "$d"/x*.log; }
done
