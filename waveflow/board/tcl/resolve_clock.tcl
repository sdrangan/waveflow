# Parameterized, configuration-only probe. No synthesis, timing closure, or board access.
# The caller owns an isolated run directory and checks the exact Vivado release.
if {[catch {
    if {$argc != 7} {
        error "expected <board_repo_dir> <board_part> <device_part> <source> <generator> <requested_hz> <input_hz>"
    }
    lassign $argv board_repo_dir board_part device_part source generator requested_hz input_hz
    if {$generator ni {ps_pl0 mmcm}} {
        error "unsupported clock generator: $generator"
    }
    if {![string is double -strict $requested_hz] || $requested_hz <= 0} {
        error "requested_hz must be positive"
    }
    set_param board.repoPaths [list [file normalize $board_repo_dir]]
    if {[llength [get_board_parts -quiet $board_part]] != 1} {
        error "board part is unavailable: $board_part"
    }
    create_project -in_memory clock_resolution -part $device_part
    set_property board_part $board_part [current_project]
    if {[get_property PART [current_project]] ne $device_part} {
        error "board preset selected a different device"
    }
    create_bd_design clock_resolution
    set requested_mhz [expr {double($requested_hz) / 1e6}]

    if {$generator eq "ps_pl0"} {
        set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e:3.5 ps]
        apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e -config {apply_board_preset "1"} $ps
        set_property CONFIG.PSU__FPGA_PL0_ENABLE 1 $ps
        set_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ $requested_mhz $ps
        set actual_hz [expr {double([get_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__ACT_FREQMHZ $ps]) * 1e6}]
        set pin_hz [get_property CONFIG.FREQ_HZ [get_bd_pins ps/pl_clk0]]
        set clock_source [get_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__SRCSEL $ps]
        set d0 [get_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__DIVISOR0 $ps]
        set d1 [get_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__DIVISOR1 $ps]
        set reference_hz [expr {double([get_property CONFIG.PSU__PSS_REF_CLK__FREQMHZ $ps]) * 1e6}]
        set parameters [format {{"clock_source":"%s","divisor0":%s,"divisor1":%s,"reference_hz":%.17g}} $clock_source $d0 $d1 $reference_hz]
    } else {
        if {![string is double -strict $input_hz] || $input_hz <= 0} {
            error "MMCM input_hz must be positive"
        }
        set clock [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 cw]
        set_property -dict [list \
            CONFIG.PRIMITIVE MMCM \
            CONFIG.PRIM_SOURCE Differential_clock_capable_pin \
            CONFIG.PRIM_IN_FREQ [expr {double($input_hz) / 1e6}] \
            CONFIG.CLKOUT1_REQUESTED_OUT_FREQ $requested_mhz \
            CONFIG.USE_RESET false \
        ] $clock
        set fin_hz [expr {double([get_property CONFIG.PRIM_IN_FREQ $clock]) * 1e6}]
        set m [get_property CONFIG.MMCM_CLKFBOUT_MULT_F $clock]
        set d [get_property CONFIG.MMCM_DIVCLK_DIVIDE $clock]
        set o [get_property CONFIG.MMCM_CLKOUT0_DIVIDE_F $clock]
        set actual_hz [expr {$fin_hz * $m / $d / $o}]
        set pin_hz [get_property CONFIG.FREQ_HZ [get_bd_pins cw/clk_out1]]
        set parameters [format {{"input_hz":%.17g,"multiplier":%s,"input_divider":%s,"output_divider":%s}} $fin_hz $m $d $o]
    }

    save_bd_design
    puts [format {WAVEFLOW_CLOCK_RESULT: {"source":"%s","generator":"%s","part":"%s","board_part":"%s","vivado_version":"%s","requested_hz":%.17g,"actual_hz":%.17g,"pin_hz":%s,"parameters":%s}} $source $generator [get_property PART [current_project]] [get_property BOARD_PART [current_project]] [version -short] $requested_hz $actual_hz $pin_hz $parameters]
    puts "WAVEFLOW_CLOCK_OK"
} message options]} {
    puts stderr "WAVEFLOW_ERROR: clock resolution: $message"
    puts stderr [dict get $options -errorinfo]
    exit 1
}
exit 0
