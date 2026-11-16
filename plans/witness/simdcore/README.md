# Witness: the SIMD core (Stage 1 of `plans/simdcore.md`)

Facts before design, all **Vitis HLS 2025.1**, `xczu48dr-ffvg1517-2-e`, 4 ns target, built on the
Windows laptop under `C:\wfsc\sw` (MAX_PATH). Waveflow-independent except witness 7. csynth numbers are
estimates; cosim is Vivado xsim 2025.1 (Verilog).

```bash
python run.py                       # every variant (two at a time), then the table
python run.py w6_l2 w6_l2_short     # some
python run.py --report-only w6_l2   # re-read the reports
python w7_vecunit.py C:/wfsc/w7 s1one s1 s3    # witness 7 (the venv, PYTHONPATH = the repo)
```

## The prototype

- `lane.h`: one `cplx4` lane (records D1, D3, D4). A 32-bit lane word -- a complex 16-bit element
  (`re` low), two `e16` reals, or one `e32` real -- and **four shared 17x17 products**
  (`m0 = a.lo·b.lo, m1 = a.hi·b.hi, m2 = a.lo·b.hi, m3 = a.hi·b.lo`) that serve every multiplying op:
  `wf.vcmul`, `wf.vcmulc`, `wf.vcmacc`, `wf.vcdotr`, `vsmul` (e16 pair) and, with `SC_E32MUL`, the e32
  `vmul` / `vmulh` (low halves zero-extended). Rounding is RVV `vxrm` 0 (round half up), saturating.
  Shifts `csh` / `rsh` are run-time (the `wfsh` CSR); `SC_FIXSH` fixes them at build.
- `w6.cpp` (witnesses 6 and 2): **the flattened instruction x element loop**. One pipelined loop over
  (instruction, element group `t`), `SC_L` lanes unrolled, element `e = t·L + lane`. The register
  file is per lane, 32 registers x `T_MAX` words (`VLEN` 2048: 64 `e32` elements), in **three copies**
  (one per read operand `vs2`, `vs1`, `vd`), each a simple dual-port BRAM written alike: three reads and
  a write a lane-cycle. `#pragma HLS dependence variable=<rf> inter false` on the copies and the mask
  file. `tb6.cpp` runs a 12-instruction **dependent chain** (each reads the previous destination:
  `vcmul, vcmulc, vcmacc, vcdotr, vadd16, vmax16, vmslt16, vmerge16, vsmul16, vssra16, vsub32, vcmul`)
  and compares all 32 x 64 register words with a sequential C reference.
- `w1.cpp`: the lane alone, an op per element (the op changes every cycle).
- `w3.cpp`: the reduction -- each lane folds its own elements, then a `log2 L` tree.
- `w5.cpp`, `tb5.cpp`, `w5_prog.S`: the scalar unit and a toy vector unit as **two `hls::task`s**: an
  RV32IM subset on the real encodings forwarding OP-V words with `rs1` and `vl` on a stream, `vmv.x.s`
  blocking on a return stream. The program (`w5_prog.S`, assembled by the Vitis 2025.1 GNU as,
  `-march=rv32im_zicsr_zve32x`) does `vsetvli`, `vmv.v.x`, `vadd.vv`, `vredsum.vs`, `vmv.x.s`, then a
  scalar loop on the result; two jobs.
- `w4.cpp`: an input window's two ping-pong copies, loader and program in one loop (resources only).
- `w7_vecunit.py`: the generic `VecUnit` (`waveflow.vecunit.calib.resource_config`) at the CG layout,
  `S` 1 against 3, and with one vector and one scalar format.

## Results

| variant | what | II | depth | est. ns | LUT | FF | DSP | BRAM18 | RTL |
|---|---|---|---|---|---|---|---|---|---|
| `w6_l2` | flattened loop, L 2, dependence false | **1** | **9** | 2.898 | 8,744 | 4,040 | 8 | 14 | cosim **PASS**, 4,495 cycles |
| `w6_l2_short` | same, `VL/L` = 2 < D | 1 | 9 | 2.898 | 8,744 | 4,040 | 8 | 14 | csim pass, cosim **FAIL** (hazard) |
| `w6_l2_nodep` | no dependence pragma (HLS default) | **2** | 4 | **6.532** | 8,723 | 2,399 | 8 | 14 | cosim PASS, 4,874 cycles |
| `w6_l2_e32mul` | L 2 + e32 multiply | 1 | 9 | 2.898 | 9,442 | 4,498 | 8 | 14 | csim PASS |
| `w2_l4` | L 4 | 1 | 9 | 2.898 | 15,804 | 6,931 | 16 | 14 | |
| `w2_l8` | L 8 | 1 | 9 | 2.898 | 29,548 | 12,297 | 32 | 26 | |
| `w2_l16` | L 16 | 1 | 9 | 2.898 | 59,861 | 26,671 | 64 | 50 | |
| `w1_lane` | one lane | 1 | 7 | 2.835 | 3,889 | 1,530 | 4 | 0 | |
| `w1_lane_fixsh` | one lane, `wf.` shifts fixed | 1 | 7 | 2.792 | 2,960 | 1,671 | 4 | 0 | |
| `w1_lane_e32mul` | one lane + e32 multiply | 1 | 7 | 2.870 | 4,235 | 1,788 | 4 | 0 | |
| `w3_l2` | reduction, L 2 | 1 | 3 | 2.341 | 713 | 205 | 0 | 0 | |
| `w3_l4` | reduction, L 4 | 1 | 3 | 2.481 | 1,221 | 461 | 0 | 0 | |
| `w3_l8` | reduction, L 8 | 1 | 3 | 2.481 | 2,256 | 815 | 0 | 0 | |
| `w3_l16` | reduction, L 16 | 1 | 3 | 2.481 | 4,308 | 1,618 | 0 | 0 | |
| `w5_scalar` | scalar + vector tasks, issue loop II 1 target | 1 | 3 | **6.815** | 7,313 | 4,361 | 3 | 1 | csim PASS (2 jobs) |
| `w5_scalar_ii2` | same, II 2 target | **3** | 4 | 4.051 | 6,034 | 3,331 | 3 | 3 | csim PASS (2 jobs) |
| `w4_dm` | window ping-pong, L 2, 8 registers | 1 | 15 | 2.556 | 1,728 | 2,118 | 0 | **24** | |

W7 (the `VecUnit`) is in its own table below.

## Findings

1. **Witness 6, the key risk: the flattened loop works.** With the register file's dependence declared
   false, the instruction x element loop runs at **II 1 with depth 9 across instruction boundaries**,
   and the RTL is bit-exact on a 12-instruction dependent chain at `VL` 64 (`T` = 32 groups). The
   co-simulated latency, 4,495 cycles, is `2048 (load) + 384 + 9 (exec) + 2048 + 3 (dump)` plus 3: the
   twelve instructions cost `12 · 32` cycles and **one** fill of 9, not twelve -- no flush between
   instructions. The cost model `S·N/L + D` holds at RTL.
2. **The hazard is real, and the pragma's precondition is `VL/L >= D`.** At `VL/L` = 2 the same RTL
   reads stale operands (`w6_l2_short`: C simulation passes, co-simulation fails at `v4[0]`). So the
   short-vector stall of D2 is required hardware, not an optimization: the issue logic must hold a
   dependent instruction until `D - VL/L` cycles have passed.
3. **Without the pragma HLS gives II 2 and misses timing** (`w6_l2_nodep`: II 2, 6.5 ns estimated
   against 4): the default is both slower and unbuildable at 250 MHz. The pragma is not optional.
4. **D = 9** (the EXEC loop's depth at L 2-16; the lane alone is 7, the register read and write add 2).
   With `VLEN` 2048, a full `e32` vector gives `T` = 32 (L 2), 16 (L 4), 8 (L 8) groups: L 8 is already
   below D for a full `e32` vector, and CG's `VL` 32 at L 4 (`T` = 8) stalls 1 cycle an instruction.
5. **LUT scales linearly in L (witness 2):** 8.7k, 15.8k, 29.5k, 59.9k at L 2, 4, 8, 16 -- about
   **3.7k a lane** plus ~1.3k, with no curvature (L 16 is 1.01x twice L 8). DSP exactly 4 a lane.
   BRAM: the three register-file copies, one BRAM18 each a lane from L 4 up (14, 14, 26, 50).
6. **A lane is 3.9k LUT -- heavier than hoped -- and a quarter of it is the run-time shifts**
   (`w1_lane` 3,889 against `w1_lane_fixsh` 2,960: the variable rounding shifters of `csh` / `rsh`
   cost ~930 LUT a lane, 24 %). The expression breakdown of `w1_lane`: selects 652, arithmetic shifts
   596 + logical 404, adders 585, compares 441. Options for D1: fix `csh` per build (or allow a few
   values: 15 and 0 cover Q15 and integer work), or keep one shifter shared by the complex parts.
7. **The e32 real multiply costs +8.9 % of a lane** (`w1_lane_e32mul` 4,235 against 3,889; +698 LUT,
   +8 % at L 2 in the full loop), under D3's 15 % threshold: **keep it**.
8. **The reduction tree is ~270 LUT a lane**, linear (713 to 4,308 from L 2 to 16), depth 3 for the
   per-lane fold, the tree adding 1 cycle a level.
9. **Witness 5: the two-task form works; the issue loop is II 3 at 250 MHz.** Scalar + vector
   `hls::task`s with a return stream for `vmv.x.s` run two jobs in C simulation with the right result
   and no deadlock. But the scalar decode/execute loop (register file read, ALU, write back, next PC,
   all loop-carried) estimates **6.8 ns at II 1**; asked for II 2 it achieves **II 3 at 4.05 ns**. A
   scalar instruction every 3 cycles is fine for a vector instruction of `T` >= 16 cycles with a few
   scalar instructions beside it (the matched filter), but CG's per-problem scalar work (two
   reductions read back, two divides, addresses) runs at a third of the rate the model assumed: count
   it in the cycle model (`SII = 3`), and keep scalar work off the vector unit's critical path (D2's
   decoupling is what makes that possible).
10. **Witness 4 is inconclusive.** Written with the loader's writes to the next copy and the program's
    reads of the current one in exclusive branches, HLS did not see them as exclusive and split each
    256-word window memory three ways: **24 BRAM18 for an 8-register window at L 2**, against the 4 a
    port-per-copy form needs. The window needs an explicit port-per-copy structure (or one memory per
    copy with the copy bit choosing which task owns it, as `stream_of_blocks` does); not pursued here.

## Against the D9 budgets (records, Stage 0)

- **Surprise (rule 2): the lane cost.** At the matched filter's L 2, the datapath and register file
  alone are **8.7k LUT**, before the scalar unit (~6k, witness 5), the loader / storer and windows, the
  reduction and control. The pre-registered target, <= 20k LUT (stretch 12k), is still within reach but
  the stretch is not, unless the run-time shifts go (finding 6: -1.9k at L 2). At CG's L 4 the
  datapath is 15.8k against a target of <= 30k (stretch 21.5k): within reach.
- **Surprise: the scalar issue loop is II 3**, not 1 (finding 9). It does not change the matched
  filter's prediction; it raises CG's scalar share.
- No surprise in scaling: linear in L, as the plan bets.

## Witness 7: the `VecUnit` at `S` = 1

The generic unit (`resource_config((4, 8, S, "all", "cg"))`: L 4, NG 8, every slot every capability,
the CG formats), csynth alone through `waveflow.vecunit.build.build_dag(...).run(..., through="csynth")`,
Vitis HLS 2025.1, unit totals (`waveflow.vecunit.report.resources`):

| variant | `S` | vector / scalar formats | LUT | FF | DSP | BRAM18 | est. ns |
|---|---|---|---|---|---|---|---|
| `s3` | 3 | 3 / 4 (CG) | **81,862** | 61,401 | 53 | 24 | 3.124 |
| `s1` | 1 | 3 / 4 (CG) | **42,522** | 36,512 | 21 | 24 | 3.124 |
| `s1one` | 1 | 1 / 1 | **37,386** | 25,579 | 21 | 24 | 3.124 |

(`s3` matches the CG build's 82,624 of `examples/vecunit_cg/measured_2025_1.json` within 1 %.)

11. **Chaining is half of `VecUnit`; the formats are a small part.** `S` 3 -> 1 saves **39.3k LUT
    (48 %)** and 32 DSP; one vector and one scalar format saves a further **5.1k (6 % of `S` 3)**. What
    remains at `S` 1 with one format, **37.4k LUT**, is the unit's base: decode and issue, the queues and
    their tasks, the per-lane scalar file and dividers, the one slot.
12. **What this means for the comparison (D9).** An un-chained `VecUnit` runs at the same `S·N/L` as
    this core (plus its pass overheads), so `s1one` is the fair like-for-like baseline at L 4, not the
    82k build. Against it, this core's datapath and register file at L 4 are 15.8k, the reduction 1.2k,
    the scalar unit ~6k (witness 5): ~23k before the loader, storer, windows and control. The saving
    over `s1one` is therefore likely **tens of percent, not the factor of three** the 82k figure
    suggests; the case for the core rests on that margin plus the familiar ISA. D9's rule compares
    against the 82k `VecUnit`; the record should add `s1one` (37.4k) as a second reference.

Raw rows: `w7_results.txt` (the JSON lines `w7_vecunit.py` printed).

Raw outputs: `results_batch0.txt` (`w6_l2`, `w1_lane`, `w5_scalar_ii2`), `results_batch1.txt` (the rest), `w7_results.txt`.
