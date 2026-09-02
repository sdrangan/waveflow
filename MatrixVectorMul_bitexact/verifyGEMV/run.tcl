# run.tcl -- C-simulation, C-synthesis and C/RTL co-simulation of all nine Vitis BLAS gemv DUTs.
#
# Driven by vitis-run:
#   vitis-run --mode hls --tcl run.tcl
#
# The Vitis BLAS L1 headers are found via the WF_BLAS_LIBS environment variable; it must point at
#   .../blas/L1/include/hw          (see README.md)
#
# One project per DUT, because Vitis synthesizes one top per solution.  Each DUT exists to close
# a specific "not verified" item -- see ARCHITECTURE.md for what each one is and why.
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
set base_cf "-I$here/src -I$blas -I$blas/xf_blas -std=c++14 -ffp-contract=off"

# name  top  tb-source  -DWF_DUT  input-file
set duts {
    {f32       gemv_f32_top       gemv_float_tb.cpp    0  input_f32.txt}
    {f32_wide  gemv_f32_wide_top  gemv_float_tb.cpp    1  input_f32_wide.txt}
    {f32_pad   gemv_f32_pad_top   gemv_float_tb.cpp    2  input_f32_pad.txt}
    {f64       gemv_f64_top       gemv_float_tb.cpp    3  input_f64.txt}
    {ab        gemv_ab_top        gemv_ab_tb.cpp      -1  input_ab.txt}
    {i32       gemv_i32_top       gemv_intlike_tb.cpp  0  input_int.txt}
    {u32       gemv_u32_top       gemv_intlike_tb.cpp  1  input_int.txt}
    {fixed     gemv_fixed_top     gemv_intlike_tb.cpp  2  input_fixed.txt}
    {fix24     gemv_fix24_top     gemv_intlike_tb.cpp  3  input_fix24.txt}
}

# WF_DUTS, if set, is a space-separated list of DUT names to run -- so fixing one does not cost a
# full sweep.  Unset means all of them.
set only {}
if {[info exists ::env(WF_DUTS)]} { set only $::env(WF_DUTS) }

set ran 0
foreach dut $duts {
    lassign $dut stem top tb sel data
    if {[llength $only] && [lsearch -exact $only $stem] < 0} { continue }
    incr ran
    set cf $base_cf
    if {$sel >= 0} { set cf "$base_cf -DWF_DUT=$sel" }

    puts "WAVEFLOW_DUT_BEGIN: $stem ($top)"
    open_project -reset ${stem}_proj
    set_top $top
    add_files    $here/src/gemv_top.cpp -cflags $cf
    add_files -tb $here/src/$tb -cflags $cf

    open_solution -reset "solution1"
    set_part {xc7z020clg484-1}
    create_clock -period 10

    set argv_csim  "$here/data/$data $here/results/output_${stem}_csim.txt"
    set argv_cosim "$here/data/$data $here/results/output_${stem}_cosim.txt"

    # ---- 1. C-simulation: the library's C++ running natively ----
    if {[catch {csim_design -argv $argv_csim} res]} {
        puts "WAVEFLOW_ERROR: csim failed for $stem"; puts $res; exit 1
    }
    puts "WAVEFLOW_CSIM_OK: $stem"

    # ---- 2. C-synthesis: C++ -> RTL ----
    if {[catch {csynth_design} res]} {
        puts "WAVEFLOW_ERROR: csynth failed for $stem"; puts $res; exit 1
    }
    puts "WAVEFLOW_CSYNTH_OK: $stem"

    # ---- 3. C/RTL co-simulation: the SAME testbench driving the synthesized RTL ----
    if {[catch {cosim_design -argv $argv_cosim -trace_level none} res]} {
        puts "WAVEFLOW_ERROR: cosim failed for $stem"; puts $res; exit 1
    }
    puts "WAVEFLOW_COSIM_OK: $stem"
    close_project
}

if {$ran == 0} {
    puts "WAVEFLOW_ERROR: WF_DUTS matched no DUT"
    exit 1
}
puts "WAVEFLOW_SUCCESS: csim + csynth + cosim passed for $ran DUT(s)"
exit 0
