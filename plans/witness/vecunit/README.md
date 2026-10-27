# Witness: the vector unit's execute task (Stage 1 of `plans/vecunit_new.md`)

Facts before design, all **Vitis HLS / Vivado xsim 2024.1**, `xczu48dr-ffvg1517-2-e`, 4 ns target,
csynth from a Linux build directory. Waveflow-independent: the Vitis and Vivado binaries directly.

```bash
./run.sh OUT_DIR                          # csynth every variant of witnesses 1-4, then the table
COSIM=1 ./run.sh OUT_DIR w2_cosim         # + C/RTL co-simulation with tb.cpp
./run.sh OUT_DIR w5 w5b_cosim w5c_cosim w5d_cosim && ./run_rtl5.sh OUT_DIR w5 w5b_cosim w5c_cosim w5d_cosim
```

## The prototype (`vexec.h`, `cfg.h`, `vexec_top.cpp`)

One call is one job: a job word, then instructions decoded from a stream. A vector instruction and
the ones chained to it run as one **pass**, a pipelined loop over `VL` elements in which every slot
computes per lane and a later slot reads an earlier slot's result after its quantization. A slot's
datapath per lane: **one complex multiplier** `P = A' * B` (A' a vector operand, conjugated or
broadcast from member 0, or a real per-member scalar -- the scalar forms and the reduction reuse the
multiplier), then `R = X +- Y`, quantized to the destination's format. Register file: `NB` banks per
lane, group `g` in bank `g % NB`. The centre configuration (`L 4, VLMAX 8, S 3`) with CG-like formats:
five 12-bit vector formats (union `(14, 5)`), four scalar formats (union `(21, 10)`), `g_div 6`.

`tb.cpp` runs a CG-like iteration plus the conjugate and broadcast forms through the RTL (`vexec_top`)
and through the same code as C (`vexec_ref`) and compares every output word.

## Results

| variant | what | PASS II | depth | est. ns | DSP | LUT | FF | BRAM | csynth GB |
|---|---|---|---|---|---|---|---|---|---|
| `w1_ng8_nb4` | 8 groups, 4 banks | 1 | 24 | 2.913 | 48 | 50,863 | 28,413 | 16 | 0.78 |
| `w1_ng16_nb4` | 16 groups, 4 banks | 1 | 24 | 2.913 | 48 | 50,863 | 28,422 | 16 | 0.78 |
| `w1_ng32_nb4` | 32 groups, 4 banks | 1 | 24 | 2.913 | 48 | 50,871 | 28,431 | 16 | 0.78 |
| `w1_ng8_nb8` | 8 groups, a memory a group | 1 | 25 | 2.913 | 48 | 57,267 | 30,750 | 32 | 0.85 |
| `w1_ng16_nb16` | 16 groups, a memory a group | 1 | 25 | 2.913 | 48 | 70,382 | 32,234 | 64 | 0.95 |
| `w1_ng32_nb32` | 32 groups, a memory a group | 1 | 25 | 2.913 | 48 | 105,550 | 45,570 | 128 | 1.69 |
| `w2_nodep` | 32 / 4, no `DEPENDENCE` pragma | **2** | 3 | **40.493** | 48 | 45,627 | 9,251 | 16 | 0.77 |
| `w4_f1` | 32 / 4, one vector format | 1 | 22 | 2.913 | 48 | 37,997 | 20,856 | 16 | 0.71 |
| `w4_qshared` | 32 / 4, five formats, one shared quantizer | 1 | 22 | 2.913 | 48 | 44,461 | 26,041 | 16 | 0.75 |

1. **Witness 1, the register file.** Banked, LUT is flat in `NG` (50.9k at 8, 16 and 32 groups: the
   bank memories deepen, the muxes do not grow); one memory a group grows with `NG` (57k, 70k, 106k
   LUT; 32, 64, 128 BRAM; 0.85 to 1.69 GB). **D2: banked.**
2. **Witness 2, `DEPENDENCE`.** Needed: without `#pragma HLS DEPENDENCE variable=rf type=inter
   dependent=false` the pass loop runs at II 2 (and the scheduler's estimate breaks, 40.5 ns). With
   it, II 1, and **C/RTL co-simulation passes** (`w2_cosim`: the shared-quantizer variant, the CG-like
   program, every output word equal, checksum `deb318487b30f644`). Co-simulation's dependence monitor
   warns of an access at loop distance 11; a pass has at most `VLMAX = 8` iterations, so the distance
   spans two pass calls, which run one after the other (the pass is its own non-inlined function).
3. **Witness 3, chaining.** `S = 3` chained slots, forwarding after quantization: **II 1 at 2.913 ns**
   (4 ns target), pass depth **24 cycles** (22 with the shared quantizer). A pass therefore costs about `VL + 24` cycles plus its
   decode: at `VL = 8` the fill and drain are three times the elements.
4. **Witness 4, the quantizers.** Five per-format quantizers per slot datapath (D4 as written) cost
   about 12.9k LUT over one format (50.9k against 38.0k). One quantizer whose shift, round bit,
   saturation bounds and wrap width are muxed on the format id computes the same bits (checked: the
   same output checksum) for 44.5k: **D4 amended to the shared quantizer**, half the cost.
5. **Witness 5, queue locks under run-time control** (`w5.cpp`, `tb5.cpp`, `tb5.v`). Three
   free-running `hls::task`s; the middle one acquires and releases stream-of-blocks buffers through
   `hls::read_buf` / `hls::write_buf` (`#define EXPLICIT_ACQUIRE_RELEASE`, present in both the
   simulation and synthesis headers of 2024.1 but undocumented), called when decoded instructions say
   so. csynth: COPY at II 1, est. 2.697 ns. **At RTL it is correct**: `tb5.v` under xsim drives the
   ports directly, 256 of 256 lane groups and 2 of 2 statuses, last output at cycle 537. So are the
   variants: unconditional explicit locks (`w5b`, 562 cycles), conditional without a reference
   (`w5c`, 537), and the documented scoped `read_lock` / `write_lock` (`w5d`, 562).
   **Vitis co-simulation cannot judge it**: its C test-bench phase runs the synthesized model and
   fails all four, the documented scoped locks included (215-233 of 256 words wrong), while native
   C (g++, threads) is right. **D5 stands**: whole-matrix blocks, acquired at first use and released by
   `wf.qpop` / `wf.qpush`. Risk: an undocumented API; re-check it with every tool version.
6. **D6.** csynth's peak memory is 0.71-1.69 GB in every variant: an instruction loop over a local
   register file does not hit the 6.7 / 7.0 GB failures of the systolic engine's state carried across
   firings.
