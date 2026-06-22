# Vitis HLS driver for the rowwise_fir sandbox (plans/dataflow_composition.md
# Phase 1).  Builds TWO csynth results from the SAME hand-written sources via a
# compile flag, to prove single-bundle II = #streams vs multi-bundle II = 1:
#   * waveflow_rowwise_fir_proj / solution_singleport  (X+Y share bundle=gmem)
#   * waveflow_rowwise_fir_split_proj / solution_split (-D WF_FIR_SPLIT_BUNDLE)
# Per-solution -D requires separate projects (Vitis cflags are project-scoped).
#
# csim runs once (solution-independent) and is bit-exact vs Y_golden.bin.
# Cosim is gated behind WAVEFLOW_ROWWISE_FIR_COSIM (off by default, like
# block_scale) and runs against the single-port solution only.
#
# Conventions mirror examples/block_scale/run.tcl: WAVEFLOW_SUCCESS: /
# WAVEFLOW_ERROR: sentinel lines and exit 0/1.
set script_dir [file dirname [file normalize [info script]]]
set part {xc7z020clg484-1}

# Data dir: optional env override (used by the cosim sweep for per-point
# fixtures), else the committed data/ fixture.  (vitis-run injects its own argv,
# so we use an env var rather than a tclarg.)
set data_dir [file join $script_dir "data"]
if {[info exists ::env(WAVEFLOW_ROWWISE_FIR_DATA_DIR)]} {
    set data_dir $::env(WAVEFLOW_ROWWISE_FIR_DATA_DIR)
}

# Exact m_axi depths for the active fixture (emitted by gen_data.py), injected
# via -D so cosim's memory model matches the testbench arrays.
set depthflags ""
set dims_tcl [file join $data_dir "dims.tcl"]
if {[file exists $dims_tcl]} {
    source $dims_tcl
    set depthflags " -DWF_FIR_X_DEPTH=$WF_FIR_X_DEPTH -DWF_FIR_Y_DEPTH=$WF_FIR_Y_DEPTH"
}

set do_cosim 0
set trace_level "none"
if {[info exists ::env(WAVEFLOW_ROWWISE_FIR_COSIM)]} {
    set do_cosim [expr {$::env(WAVEFLOW_ROWWISE_FIR_COSIM) in {1 true TRUE yes YES}}]
}
if {[info exists ::env(WAVEFLOW_ROWWISE_FIR_TRACE_LEVEL)]} {
    set trace_level $::env(WAVEFLOW_ROWWISE_FIR_TRACE_LEVEL)
}
# Sweep mode: single-port cosim only, skip the split-bundle csynth.
set singleport_only 0
if {[info exists ::env(WAVEFLOW_ROWWISE_FIR_SINGLEPORT_ONLY)]} {
    set singleport_only [expr {$::env(WAVEFLOW_ROWWISE_FIR_SINGLEPORT_ONLY) in {1 true TRUE yes YES}}]
}

# ---------------------------------------------------------------------------
# Solution 1: single shared m_axi bundle (X, Y, h on bundle=gmem).
# ---------------------------------------------------------------------------
open_project -reset waveflow_rowwise_fir_proj
set_top fir_accel
add_files [file join $script_dir fir_sandbox.cpp] -cflags "-I$script_dir$depthflags"
add_files -tb [file join $script_dir fir_tb.cpp] -cflags "-I$script_dir"
open_solution -reset "solution_singleport"
set_part $part
create_clock -period 10

if {[catch {csim_design -argv "$data_dir"} res]} {
    puts "WAVEFLOW_ERROR: rowwise_fir C-simulation failed."
    puts $res
    exit 1
}
if {[catch {csynth_design} res]} {
    puts "WAVEFLOW_ERROR: rowwise_fir single-port C-synthesis failed."
    puts $res
    exit 1
}
if {$do_cosim} {
    if {[catch {cosim_design -argv "$data_dir" -trace_level $trace_level} res]} {
        puts "WAVEFLOW_ERROR: rowwise_fir RTL co-simulation failed."
        puts $res
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Solution 2: split m_axi bundles (separate gmemX / gmemY).  Separate project so
# -D WF_FIR_SPLIT_BUNDLE applies.  Cosim shows this is IDENTICAL to single-port
# (a single bundle is already full-duplex); kept as the controlled comparison.
# ---------------------------------------------------------------------------
if {!$singleport_only} {
    open_project -reset waveflow_rowwise_fir_split_proj
    set_top fir_accel
    add_files [file join $script_dir fir_sandbox.cpp] -cflags "-I$script_dir -DWF_FIR_SPLIT_BUNDLE$depthflags"
    add_files -tb [file join $script_dir fir_tb.cpp] -cflags "-I$script_dir -DWF_FIR_SPLIT_BUNDLE"
    open_solution -reset "solution_split"
    set_part $part
    create_clock -period 10
    if {[catch {csynth_design} res]} {
        puts "WAVEFLOW_ERROR: rowwise_fir split-bundle C-synthesis failed."
        puts $res
        exit 1
    }
    # Cosim the split solution too: whether the single-bundle vs split topology
    # changes timing is an RTL effect (static csynth is blind to it).  cosim shows
    # they are identical -- the full-duplex finding -- which is the point.
    if {$do_cosim} {
        if {[catch {cosim_design -argv "$data_dir" -trace_level $trace_level} res]} {
            puts "WAVEFLOW_ERROR: rowwise_fir split-bundle RTL co-simulation failed."
            puts $res
            exit 1
        }
    }
}

if {$do_cosim} {
    puts "WAVEFLOW_SUCCESS: rowwise_fir C-sim, C-synth, and RTL co-sim passed."
} else {
    puts "WAVEFLOW_SUCCESS: rowwise_fir C-sim and C-synth passed."
}
exit 0
