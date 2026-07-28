# Plan — RFSoC 4x2 bring-up: Waveflow for SDR / wireless experiments

**Status:** direction captured (2026-06-20), not started. Deferred until the VMAC timing
stages wrap. Drafted from a design discussion; treat as a starting frame, not a committed spec.

## Why
Make Waveflow useful to a wireless lab doing SDR experiments (channel sounding, array
processing) on **RFSoC 4x2** (Zynq UltraScale+ RFSoC gen3, 4 ADC / 2 DAC) and ZCU-series
boards. The applied motivation for the paper positioning ([[reference-paper-positioning]]).

## The key fit (already reasoned through)
The standard RFDC simulation methodology IS the Waveflow component model. You **cannot**
simulate the analog converter; "data from an ADC" in sim always means injecting digital samples
at the **AXIS boundary**. The pragmatic, widely-used approach (often a literal `SIM`/`SYNTH`
generate): in SIM, drive the ADC-side AXIS from a file/waveform/channel source and capture the
DAC-side AXIS; in SYNTH, the real RFDC IP. That "signal-source-replaces-converter in sim,
real-RFDC in hardware" pattern is exactly Waveflow's sim/synth duality — make it first-class.

## The block-LT simulation architecture (the user's design)
Same loosely-timed block pattern as VMAC ([[project-vmac-timing-example]]): **block = the
transaction, numpy = the function, block-duration = the timing.** Process samples in blocks
(`blksize` e.g. 1024), NOT per-IQ-sample — one SimPy event per block, timed at `blksize/fs`,
with the channel computed by numpy. Avoids a SimPy event per sample.

- **CDC fits naturally at LT:** each clock domain is a SimObj at its own block cadence; blocks
  cross between them. LT captures rate-match + crossing latency + backpressure; abstracts the
  FIFO internals/metastability (a verified-IP / RTL-sizing concern). Backpressure (fabric can't
  keep the converter rate) → the SAME queue/occupancy machinery as VMAC's mm-queue.
- **`blksize` is the sim-grain knob** (fast/coarse): make it compatible with the DSP's natural
  block (FFT size, AXIS samples-per-beat × int) to avoid re-blocking. Block→AXIS packing is the
  same duality as VMAC (one block event in sim, N-samples/beat in hardware).

## Components to model
- `RfdcAdc` — AXIS master; params = sample rate, decimation, NCO/mixer freq; `run_proc` emits
  sample blocks from a signal/channel model. DDC (NCO mix + decimate) modeled functionally.
- `RfdcDac` — AXIS slave sink (capture to file / compare). DUC functionally.
- `Channel` — sparse FIR (multipath taps) + Doppler (time-varying phase), applied to blocks.
- The fabric DSP between them = generated HLS kernels (the existing Waveflow path).

## The one real subtlety: inter-block state (overlap)
Channels and stateful DSP (filters, DDC) have memory spanning block boundaries → the channel /
filter SimObjs MUST carry state across blocks (overlap-save / keep last `L-1` samples; Doppler
phase accumulator carried across blocks). Discontinuities at block edges if state resets per
block — bake the overlap discipline in from day one.

## Fidelity boundary (name it, like the VMAC LT work)
Feedforward DSP (filters, FFT, channelizers, mixers, matched filters) is **block-perfect**.
**Sample-level feedback loops** (carrier recovery, timing recovery, AGC) have dynamics block
granularity can't resolve — model those functionally or at finer grain. Most SDR receivers have
at least one.

## (b) Vivado TCL autogen — bounded but real
RFDC is hardened IP instantiated via IPI TCL (`create_bd_cell` + a large pile of
`set_property CONFIG.*`: tile/converter map, sample rate, PLL/clocking, mixer, decimation). If
the RFDC config is the component's params, emitting that TCL is mechanical codegen. Intricate
and **device/board-specific** (4x2 gen3 vs ZCU111 gen1 differ) — template from a known-good
reference BD and parameterize (see *Saving projects* below for how to capture one). Clocking is
the fiddly part.

## (c) RTL sim path — hardest, not Waveflow-specific
The same "decouple at AXIS, drive with vectors" pattern; the ADC model's generated sample
vectors drive an RTL/cosim testbench at the AXIS boundary (exactly today's HLS cosim stimulus,
scaled to the converter boundary). System-level RTL sim through the whole chain (vs per-kernel
cosim) is new integration work.

## Saving projects — archival contract for a known-good reference design
A working board-level design (e.g. the student's `wave_player`: PS → SmartConnect → HLS
`wave_player` + `iq_bram_bridge` + blk_mem_gen → RFDC DAC) is worth committing as a *source*
design, not a tarball. It is both the inspection target and the template the TCL autogen in
**(b)** parameterizes.

Rule of thumb: **commit sources + the TCL that rebuilds the project.** Never commit the Vivado
project tree (`.xpr`, `.runs/`, `.cache/`, `.gen/`, `.hw/`, `.sim/`, `.ip_user_files/`) — all
regenerated, huge, and path-dependent.

**Where the block diagram lives.** Natively it is JSON at
`<proj>.srcs/sources_1/bd/<bd>/<bd>.bd` — committable in principle, but Vivado-version-fragile
and only meaningful beside its generated output products. The portable form is:

```tcl
open_project ./vivado/<proj>.xpr
open_bd_design [get_files <bd>.bd]
write_bd_tcl -force -include_layout ./bd/create_bd.tcl
```

That single file carries every `create_bd_cell`, every `set_property CONFIG.*` (the whole RFDC
tile/NCO/PLL pile, the clk_wiz 245.76 settings, blk_mem_gen sizing, SmartConnect port count),
every `connect_bd_net` / `connect_bd_intf_net`, and the address-map assignments;
`-include_layout` preserves the visual arrangement so it opens looking like the design instead of
an auto-layout. Two caveats: it hard-codes IP **VLNV version strings** (a different Vivado
version prompts an upgrade), and it references the HLS IP repo by **absolute path** — rewrite
that to repo-relative or a variable.

**Minimum file set**

- **HLS — one per kernel, and there is usually more than one.** The `.cpp`/`.h`, any C testbench,
  and a `run_hls.tcl` pinning part, clock period, top-function name, and the `export_design`
  VLNV/version. The TCL matters as much as the source: without it nobody reproduces the IP the BD
  binds to. Optionally commit the exported IP `.zip` too — redundant in principle, but HLS version
  drift silently changes generated RTL and interface names, and a reference design should be
  stable.
- **Vivado** — `bd/create_bd.tcl`; `constraints/*.xdc` (board pins: `fpga_refclk_in`,
  `dac0_clk`, `sysref_in`, `vout00`); the top-level wrapper *only if hand-edited* (regenerate it
  with `make_wrapper` otherwise); and a `scripts/build.tcl` that creates the project, sets
  part/board part + IP repo path, sources the BD TCL, wraps, and runs synth/impl/`write_bitstream`.
  Start it from `write_project_tcl -no_ip_version -force`, then de-absolutize the paths.
- **Environment manifest** (the part everyone forgets) — exact Vivado/Vitis version, the part
  (`xczu48dr-ffvg1517-2-e`), and the **RFSoC 4x2 board-files version + provenance** (RealDigital).
  The BD TCL names a `board_part` that does not exist on a clean machine; vendoring the board
  files into `boards/` is safest.
- **Runtime + known-good outputs** — the PYNQ notebook / Python driver, plus `.bit` and `.hwh`.
  Outputs rather than sources, but PYNQ *needs* the `.hwh`, and a known-good pair separates "my
  rebuild broke" from "the board is misconfigured".

```
rfsoc4x2-wave-player/
├── README.md              # versions, board files, build order
├── hls/wave_player/       # .cpp .h tb run_hls.tcl
├── hls/iq_bram_bridge/
├── bd/create_bd.tcl       # write_bd_tcl -include_layout
├── constraints/top.xdc
├── scripts/build.tcl      # end-to-end: HLS → IP repo → BD → bitstream
├── boards/                # vendored rfsoc4x2 board files
├── sw/                    # notebook + python driver
└── prebuilt/              # .bit + .hwh reference
```

**Acceptance test — do not skip.** Clone into an empty directory on a *different* machine (or at
minimum a fresh path) and run `vivado -mode batch -source scripts/build.tcl` to a bitstream.
Export scripts essentially always carry one absolute path or one missing board file, and that only
surfaces on a clean clone.

## Staging (first milestone first)
1. **Loopback** — DAC → modeled `Channel` → ADC with a trivial DSP (decimating FIR / DDC),
   modeled end-to-end with a file-driven ADC source; prove the sim/synth duality on the 4x2.
2. **Channel sounder** — transmit a known sequence (Zadoff-Chu / PN), pass through the
   sparse-FIR+Doppler channel, correlate at RX to estimate the CIR. Real experiment; the golden
   (the known CIR) is trivially checkable against the estimate. Exercises overlap/state.

## Ecosystem
Complementary to **RFSoC-PYNQ** (runtime: Python drives the deployed bitstream), not competing —
Waveflow is design-time (model + generate the overlay logic). A lab uses Waveflow to build, PYNQ
to run.
