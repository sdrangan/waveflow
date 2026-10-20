# Witness: banked stream-of-blocks and the streaming systolic sweep

Two experiments behind `plans/systolic_characterization.md`, Part B (2026-10-19). Vitis HLS **2025.1**,
`xczu48dr-ffvg1517-2-e`, 4 ns. Run each from a short directory (csynth fails silently past Windows'
260-character paths): `vitis-run --mode hls --tcl <script>`.

## 1. A stream-of-blocks block can be banked (`sob_bank.cpp`, `run_sob_bank.tcl`)

Two free-running `hls::task`s joined by `hls_thread_local hls::stream_of_blocks<ap_uint<24>[4][8], 2> b`
with `#pragma HLS ARRAY_PARTITION variable=b dim=1 complete`. csynth: "Applying array_partition to
'b': Complete partitioning on dimension 1", then "Aggregating stream-of-blocks variable 'b_0'" ...
`'b_3'` -- four independent ping-pong buffers. The reader's loop takes all four rows a cycle:
`Final II = 1, Depth = 3, loop 'RD'`; the writer `Final II = 1, loop 'WR'`.

So operands can be loaded straight into the banked layout the array reads, with no copy in the
consumer, and the ping-pong still double-buffers.

## 2. The streaming systolic sweep (`sweep.h`, `sweep.cpp`, `tb.cpp`, `run_sweep.tcl`)

The core's sweep as one pipelined loop over all tiles of a job: each PE carries a `first` flag with its
`X` input and latches its accumulator into a shadow register when it arrives, starting the next tile at
once; tiles follow every `P = max(k, R*C/L, L)` steps (a shorter tile padded with zero steps);
shadows drain in diagonal order, group `(i, cg)` at slot `(q+1)P + L + n`. Centre configuration
(`Mmax 8, Kmax 8, Nmax 32, R 4, C 8, L 4`), 12-bit complex operands, form 4.

- **Bit-exact** against a plain triple loop on six shapes (`tb.cpp`, native g++ with the Vitis
  headers): `8x8x32`, `4x8x8`, `8x4x16`, `4x4x32`, `8x8x8`, `4x8x24` -- 0 wrong.
- **csynth:** `Final II = 1, Depth = 6, loop 'STEP'`; estimated 2.881 ns; 134 DSP, 11,639 FF,
  18,804 LUT, 0 BRAM (the operands are function arguments here).
- **Steps for `8x8x32`: 84** (8 tiles x 8 + the fill and the last drain) against the current core's
  298 for its 8 tiles (36 a tile).

The prototype assumes `R <= L` (distinct drain diagonals) and keeps the exact sums; the unit adds
the rounding, `MUL_AH`, short rows (`k < L`) and the panels.
