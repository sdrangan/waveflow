# run.tcl -- C-simulation, C-synthesis and C/RTL co-simulation of the three Vitis BLAS gemv DUTs.
#
# Driven by vitis-run:
#   vitis-run --mode hls --tcl run.tcl
#
# The Vitis BLAS L1 headers are found via the WF_BLAS_LIBS environment variable; it must point at
#   .../blas/L1/include/hw          (see README.md)
#
# Three separate projects because Vitis synthesizes one top per solution, and each top answers a
# different question that native C-simulation cannot reach:
#
#   gemv_f32_top    does the dot_tree reduction survive to RTL unchanged?
#   gemv_ab_top     does HLS FUSE axpy's `alpha*x + y` into an FMA?   <- the headline question
#   gemv_fixed_top  does the dotHelper.hpp:98 ap_fixed defect survive to RTL, or is it a csim
#                   artifact?  This DUT is expected to be WRONG; see ARCHITECTURE.md.
#
# Co-simulation IS the RTL simulation here: Vitis has no separate RTL-sim step for an ap_ctrl_hs
# kernel, it runs the same testbench against the synthesized RTL in xsim.
set here [file dirname [file normalize [info script]]]

if {[info exists ::env(WF_BLAS_LIBS)]} {
    set blas $::env(WF_BLAS_LIBS)
} else {
    puts "WAVEFLOW_ERROR: set WF_BLAS_LIBS to .../blas/L1/include/hw"
    exit 1
}
if {![file exists $blas/xf_blas.hpp]} {
    puts "WAVEFLOW_ERROR: xf_blas.hpp not found under WF_BLAS_LIBS=$blas"
    exit 1
}

# -ffp-contract=off is NOT cosmetic.  axpy computes `alpha*x + y` as one expression; letting the
# C compiler contract that into an FMA changes 25 of 576 rows of the native golden.  Pinning it
# here means the C-simulation result is the unfused reading, so a csim-vs-cosim difference on the
# ab DUT isolates what the SYNTHESIS TOOL chose -- which is the whole point of that DUT.
set cf "-I$here/src -I$blas -I$blas/xf_blas -std=c++14 -ffp-contract=off"

proc run_dut {here cf name tb data} {
    puts "WAVEFLOW_DUT_BEGIN: $name"
    open_project -reset ${name}_proj
    set_top ${name}
    add_files    $here/src/gemv_top.cpp -cflags $cf
    add_files -tb $here/src/$tb -cflags $cf

    open_solution -reset "solution1"
    set_part {xc7z020clg484-1}
    create_clock -period 10

    set stem [string map {gemv_ "" _top ""} $name]

    # ---- 1. C-simulation: the library's C++ running natively ----
    if {[catch {csim_design -argv "$here/data/$data $here/results/output_${stem}_csim.txt"} res]} {
        puts "WAVEFLOW_ERROR: csim failed for $name"; puts $res; exit 1
    }
    puts "WAVEFLOW_CSIM_OK: $name"

    # ---- 2. C-synthesis: C++ -> RTL ----
    if {[catch {csynth_design} res]} {
        puts "WAVEFLOW_ERROR: csynth failed for $name"; puts $res; exit 1
    }
    puts "WAVEFLOW_CSYNTH_OK: $name"

    # ---- 3. C/RTL co-simulation: the SAME testbench driving the synthesized RTL ----
    if {[catch {cosim_design -argv "$here/data/$data $here/results/output_${stem}_cosim.txt" \
                             -trace_level none} res]} {
        puts "WAVEFLOW_ERROR: cosim failed for $name"; puts $res; exit 1
    }
    puts "WAVEFLOW_COSIM_OK: $name"
    close_project
}

run_dut $here $cf gemv_f32_top   gemv_f32_tb.cpp   input_f32.txt
run_dut $here $cf gemv_ab_top    gemv_ab_tb.cpp    input_ab.txt
run_dut $here $cf gemv_fixed_top gemv_fixed_tb.cpp input_fixed.txt

puts "WAVEFLOW_SUCCESS: csim + csynth + cosim passed for all three DUTs"
exit 0
