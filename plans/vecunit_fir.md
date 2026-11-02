# Plan: single-channel FIR on the general vector unit

**Status:** Proposal; no implementation or performance result is implied.
**Complexity:** L2 after `plans/vecunit_new.md`: extend the general ISA and queue contract only if
the Stage 0 decisions and Stage 1 witnesses support it, then add a standalone FIR program and its
models. The vector unit remains application-agnostic.
**Plan file:** `plans/vecunit_fir.md` · **Parent plan:** `plans/vecunit_new.md`

> **For whoever executes this plan:** read *Rules for the doer* first. Paths are repo-relative. This
> plan separates known behavior from hypotheses. The bank equations below are derived from the
> proposed mapping; throughput, II, timing closure, and resource cost are not measured until the
> named gates produce evidence.

## Rules for the doer

1. Follow the doer rules in `plans/vecunit_new.md`; work one stage at a time and pause for review at
   every gate and on any surprise.
2. **The vector unit knows nothing about FIR.** Do not add FIR-named state, opcodes, parameters,
   branches, formats, or special cases to `waveflow/vecunit/`. The example and its program builder
   own tap count, coefficients, boundary policy, and output length. Any unit extension must describe
   a general vector or queue operation and have a non-FIR test.
3. Keep the existing CG and matched-filter programs bit-exact and unchanged in behavior. Extend the
   generic ISA/queue model, rather than changing application semantics to accommodate this example.
4. HLS bodies follow `docs/guide/vectorization/hls/loop_optimization.md`: use the generated lane
   utilities, never pack or unpack a word by hand, and do not decide, compute, and commit in one
   iteration.
5. A measured number names the tool and version (for example, Vitis HLS 2024.1); do not combine
   numbers from different versions as if they were comparable. Label analytical estimates and
   unverified hypotheses as such.
6. **Never merge or push unless asked.** Keep this work in one PR; do not alter `plans/vecunit_new.md`
   or other files as part of this plan-only change.

## Goal

Specify how the general member-per-lane vector unit from `plans/vecunit_new.md` can execute a
single-channel FIR/Conv1D efficiently. Map adjacent output samples to adjacent lanes, and retain a
contiguous input tile in cyclically banked on-chip storage so each tap reads a shifted window without
bank conflicts. Propose only general ISA and queue semantics; `examples/vecunit_fir/` expresses FIR
as a program, not as a unit mode.

For a causal FIR with `T` taps, the mathematical reference is

\[
y[m] = \sum_{t=0}^{T-1} h[t]\,x[m-t].
\]

The output domain and boundary rule (for example, zero-padded “same” output or valid-only output)
must be explicitly selected in Stage 0. The formula alone does not select a padding policy.

## Success criteria

1. **Generality:** no FIR-specific names or behavior appear in `waveflow/vecunit/`. Any window
   operation is defined on a queue and vector group, has checked bounds/halo semantics, and is tested
   independently of FIR.
2. **Bank access:** the Stage 1 HLS witness demonstrates that each read of an `L`-sample window
   addresses each of the `L` banks exactly once for every tap offset. Record the exact mapping and
   test both aligned and non-aligned window origins.
3. **Throughput, as a measured target:** for the chosen `L`, a one-read-per-bank window witness
   achieves II=1 at the declared clock target without replicated storage. This is a gate, not a
   known result. Report timing, memory inference, routing, and resource use for the named tool/device.
4. **Program correctness:** the interpreter, pysim, and RTL match an independent integer golden for
   signed inputs, coefficient values, tap counts, tail VLs, halo boundaries, and the specified
   quantization after each architecturally visible write.
5. **Useful work:** at the declared configuration, report cycles per output and input words per
   output against (a) the one-sample-per-cycle input floor and (b) a simple program-only
   copy/permute implementation. Do not call the design efficient based only on a conflict-free
   bank equation.
6. **Preservation:** the existing vector-unit tests and CG/matched-filter goldens pass unchanged.

## What is known and what is suspected

**Known from the parent plan:** a register group has `L` members; member `l` runs on lane `l`.
`wf.setvl k` selects `VL` elements, and programs strip-mine work into chunks no larger than
`VLMAX`. Queue blocks are stream-ordered, double-buffered objects, not globally addressed SRAM.
Chained instructions execute as one pass over `i < VL` at II=1 when the implementation supports
that slot combination; two instructions in one chained group may not write the same register.
Register writes quantize to the destination format.

**Derived, not a hardware measurement:** let `L` be the bank count, and map non-negative
queue-local sample index `j` to `bank(j) = j mod L`, `row(j) = floor(j/L)`. For a tile of `B`
outputs, let local index `T-1` be the first output sample; the `T-1` preceding halo samples occupy
indices `0..T-2`. Lane `l` at element `i` needs local sample `T-1 + iL + l - t` for tap `t`. For
fixed `i,t`, the bank index is `(T-1 + iL + l - t) mod L`; as `l` ranges over `0..L-1`, this is a
permutation of every bank. Therefore that access pattern is conflict-free under the proposed
mapping. It does **not** establish that the read network meets timing or that it is cheap: row
addresses differ at bank-wrap points, and bank outputs must be routed/rotated to the requesting
lanes.

**Suspected, to test:** a banked window read with one read port per bank can feed one window per
cycle at II=1; a cyclic lane rotation is smaller or faster than a full crossbar; queue-side halo
handling will not dominate the FIR program; and the programmable unit's instruction/queue overhead
will remain small for useful tap counts. None is an established result.

## Vocabulary and mapping

| Term | FIR program meaning | Unit meaning |
|---|---|---|
| lane/member `l` | computes output `m = a + iL + l` | member `l` executes on lane `l` |
| element `i` | successive `L`-output batches | vector-register element index |
| `VL` | number of batches in this pass | `wf.setvl VL`, at most `VLMAX` |
| strip-mining | advance `a` by `L × VL` output positions | repeated program chunks |
| tap `t` | selects the input window shift | program scalar/immediate, not unit state |
| window | `W[l][i] = x[a + iL + l - t]` | general read into a vector group |
| halo | the neighboring input samples needed at tile edges | queue-block data/valid-range contract |
| chaining | window read feeds multiply-accumulate in a pass | existing statically chained group rule |
| quantization | follows the selected accumulator write policy | destination-format quantization |

The lane mapping is deliberately the same for all applications. The FIR program makes a lane denote
an adjacent output position rather than a channel or antenna. If `O` outputs are requested, a
strip-mined pass has `VL = min(VLMAX, ceil(remaining_outputs/L))`; the last pass needs either a
general tail/mask mechanism or a queue/program convention that prevents invalid lanes from becoming
observable. Do not silently assume masks exist: the parent plan lists them as out of scope.

## Proposed data and execution model

For each output tile of `B = L × VL` positions, retain the distinct input samples needed by that
tile plus its halo in a queue block. For the causal equation above, the logical span is `B + T - 1`
samples before any external padding policy is applied. The block's payload, halo, valid sample
count, and output origin must be represented in a way that is unambiguous to load, execute, and
store. A queue block remains a stream-ordered object; the execute task does not receive a DRAM
address.

An illustrative generalized operation is:

```
wf.vwin vd, q, base, offset
```

with conceptual semantics `vd[l][i] = q[base + offset + iL + l]`, for `i < VL`, subject to a
specified valid-range/halo check. Here `base` is a queue-local index. In the FIR mapping, the block
starts at input sample `a-(T-1)`, so `base = T-1` is the first output's index and `offset = -t`.
The exact encoding, signedness, base source, and bounds behavior are open decisions, not an
already-selected ISA.

For a fixed tap, the lanes fetch one shifted window, multiply by the coefficient `h[t]`, and
accumulate into the output group. Coefficients can use an existing general operand form if its
semantics fit (for example, a per-member scalar initialized identically across members); otherwise,
Stage 0 must choose a general scalar/vector load or broadcast mechanism. Do not add a
“FIR coefficient” register.

To respect the parent plan's chaining rules, the conservative schedule is one tap per pass: chain a
supported window-read slot into a multiply-accumulate slot, then write the accumulator once in that
pass. Repeat for `T` taps, consuming the queue block only after the final tap. This allows a
read/MAC pair to be chained without two slots writing the accumulator register. A single instruction
group containing multiple writes to that same accumulator is illegal under the current rule. Any
alternative schedule must identify a legal register/dataflow arrangement and slot support before
claiming multi-tap overlap.

### Fixed-point semantics

The parent plan's rule is `q_d` on every register write, with exact operation arithmetic between
writes. Therefore a sequence of ordinary `vmacc` writes to an output-format register rounds and
saturates after every tap; it is not equivalent in general to exact accumulation followed by one
final rounding. The plan must choose and test one of these contracts:

1. **Per-tap destination quantization:** use the existing accumulator destination format on every
   MAC write. The golden applies `fixputils.quantize` after each tap in the same order.
2. **Wide accumulator then narrow:** accumulate each tap into a sufficiently wide general vector
   register format, with the existing destination-write rule, then explicitly convert/narrow once
   to the output format. State how overflow is handled and prove the chosen width covers or
   intentionally saturates the declared range.

The example golden must use stored integer values and the selected `fixputils` rounding and
overflow modes. Compare both contracts in Stage 0/1; do not describe one-rounding behavior as
“standard FIR” or assume it matches the current vector-unit semantics.

## ISA alternatives (provisional; D1 fixes the contract)

The operation must be general-purpose; the following are alternatives to measure and review.

| Option | General semantics | Advantages | Costs / questions |
|---|---|---|---|
| **A. `wf.vwin` queue window read (recommended witness)** | Read `VL` adjacent lane groups beginning at a general queue offset, including a defined halo/valid-range contract. | Directly expresses the access pattern; permits banked implementation without an FIR opcode; may chain to arithmetic. | Needs queue metadata/address semantics, bank rotation, slot capability, and bounds definition. A new op alone does not prove II=1. |
| **B. General gather** | Each output element selects a queue/register element by a general index vector or scalar base/stride. | More general than windows; can express permutations and irregular access. | Index storage, address generation, and routing may cost more than the regular window pattern; gather's timing/throughput is unknown. |
| **C. General slide/permute across members and elements** | Shift a vector group by an offset across adjacent members and element positions, with an explicit boundary source. | May reuse existing vector-register storage and avoid a queue-side crossbar. | The parent plan's registers are partitioned by member; cross-lane moves and tile-edge halo values need routing/materialization. Ordinary per-member RVV slides do not by themselves define this cross-member behavior. |
| **D. Program-only copy/permute** | Load overlapping/shifted data into temporary vector groups, then use ordinary arithmetic. | No new unit opcode if existing loads and register moves suffice; establishes a software baseline. | Repeated loads/copies or `L`-wide shuffles may raise bandwidth, register pressure, and instruction count. Measure it; do not dismiss it by assumption. |

RVV's standard vector slide and gather instructions are useful precedents, but the proposed unit's
member-per-lane organization requires precise cross-member and queue-boundary semantics. Cite RVV
without implying that `wf.vwin` is part of RVV or that RVV specifies this physical banking.

## Decisions (D1..D6; record choice, alternatives, and evidence here)

### D1. Window ISA and semantics

Choose A, B, C, or D (or a staged combination). Specify the mnemonic/encoding, whether the origin
and offset are immediates or scalar operands, signed offset range, per-member/per-element indexing,
VL behavior, valid-range/halo checks, and whether the operation can chain. For any new unit op, add a
non-FIR use/test demonstrating its general semantics. Record why a general queue window is or is not
preferable to program-only movement.

### D2. Queue block, tile layout, and halo

Choose the unit of a queue block: contiguous logical samples with `L` cyclic banks, a padded
`k × L` matrix, or another explicit layout. Define whether `k` counts requested outputs, input rows,
or lane groups; where halo samples live; how tile origins and valid lengths are represented; and who
supplies left/right padding. Define behavior for a final partial tile (`VL < VLMAX`, and fewer than
`L` remaining outputs). Avoid a FIR-specific queue type: any metadata must have generic block
semantics and not break existing queue users.

### D3. Banking and routing

Start with `bank(j) = j mod L`, `row(j) = floor(j/L)` in the chosen queue-local coordinate system.
Confirm whether tile alignment makes this mapping stable; write down the lane-to-bank permutation
for positive and negative offsets. Compare (a) cyclic bank-output rotation plus per-bank row
selection, (b) a general crossbar, and (c) replicated/shifted storage as an experimental baseline.
Report read/write ports, bank conflicts, mux/routing cost, timing, BRAM inference, and resource
counts. A conflict-free bank proof is not a claim that the network is free or timing-safe.

### D4. Quantization and accumulation

Select per-tap quantization or wide accumulation followed by one narrowing conversion. Record
operand formats, accumulator width, rounding, saturation/overflow, order of operations, and the exact
golden procedure. Explain how this follows the destination-write rule in `plans/vecunit_new.md`.

### D5. Chaining and slot capabilities

State which slot reads a window, which slot multiplies/accumulates, which formats and operand forms
are supported, and whether this pair can sustain II=1. Respect the existing maximum chain length and
no-two-slots-write-the-same-register rule. Specify whether one tap is one pass (recommended starting
point) or propose a legal alternative. Include queue pop/push ordering and stalls when the input is
empty or output full. Do not assume extra MAC slots improve throughput without an accumulation
schedule and witness.

### D6. Example and comparison contract

Fix the FIR definition (causal equation, coefficient order), boundary policy, output length, tap and
tile configurations, fixed-point golden, and baseline implementation. Pre-register which
configurations are training versus held-out for any Stage 7 cost fit. Name tool/device versions for
every later measurement.

## Stages

### Stage 0: decisions and pre-registered measurements

Resolve D1..D6 before implementation. Pre-register the Stage 1 configurations and metrics:

- `L` values, tap/window offsets (including negative and bank-wrap offsets), origin alignments,
  `VL` values including a tail, and block/halo sizes.
- The clock target, Vitis HLS version, FPGA part, and whether the result is csynth-only or includes
  RTL timing/cosim.
- Compare the selected banked implementation against a simple program-only copy/permute baseline.
- Metrics: achieved II, latency, inferred memories/ports, DSP/LUT/FF/BRAM, critical path or timing
  estimate, and trace-level cycles/words per output where available.

**Gate:** decisions and experiment table are recorded and reviewed. **Pause.**

### Stage 1: HLS witness for banked window read and II=1

Build small standalone HLS witnesses, not the vector unit:

1. Implement cyclic `L`-bank storage and a generalized shifted-window read. Verify bank selection
   against the equation for every lane and tested offset, including non-aligned origins and halo
   boundaries.
2. Synthesize one read per bank per cycle and test whether the read/routing loop reaches II=1 at the
   pre-registered target. Record the bank-to-lane rotation and row-address generation.
3. Compare cyclic rotation with a crossbar and, if useful, a program-only copy/permute witness.
4. Check that generated lane utilities are used for packed input/output and that the chosen memory
   primitive/inference matches the intended on-chip storage.

Do not generalize an HLS result beyond its tool version, part, parameters, or witness. If II=1 fails,
root-cause at most two hypotheses as required by the parent plan, record the result, and return to D1
/D3 rather than proceeding as if the target passed.

**Gate:** correctness and synthesis reports recorded with versions and exact configuration; selected
option meets the pre-registered gate or the plan is revised. **Pause.**

### Stage 2: ISA, assembler, interpreter, and tests

Only after Stage 1 and decision review, update `waveflow/vecunit/` with generic operation semantics,
queue metadata if needed, assembler checks, and interpreter behavior. Add tests for general window
reads independent of FIR: every bank/offset class, signed offsets, bounds/halo cases, `VL` tails,
queue stalls/pop behavior, and illegal encodings or parameters. Test program-only operations as
applicable. Check that existing CG and matched-filter programs remain bit-exact.

**Gate:** targeted `pytest tests/vecunit` passes, including old tests and new instruction tests.
**Pause.**

### Stage 3: FIR example program and golden on the interpreter

Add `examples/vecunit_fir/` as a program builder and an independent integer golden. The builder owns
tap count, coefficients, output shape, boundary policy, tile strip-mining, and queue sequencing. It
must use only the generic unit interface. Cover tap counts below, equal to, and above `L`; offsets
that wrap each bank; multiple tiles; `VL < VLMAX`; a partial final lane group if the selected
contract allows it; short inputs; and each defined halo edge. Exercise signed coefficients and
input extremes for rounding/saturation.

Compare the interpreter bit-for-bit to the golden under the D4 accumulation contract. Include the
program-only baseline if selected in D1/D6, and report instruction count and estimated traffic as
counts, not measured cycles.

**Gate:** all registered golden cases pass and the generated program is legal under the assembler.
**Pause.**

### Stage 4: pysim integration and harness checks

Integrate the general queue/window behavior into the unit's pysim tasks and add the FIR example to a
stream-only harness. Compare outputs to both the interpreter and the independent golden. Exercise
empty/full queue stalls, block boundaries, and the last tile. No timing claim is made from functional
pysim unless its timing model has been calibrated.

**Gate:** harness checks pass for FIR and existing applications; queue occupancy and status behavior
are checked. **Pause.**

### Stage 5: RTL/HLS implementation and measurements

Implement only the selected general operation and storage/routing scheme in the vector-unit HLS
bodies. Run csynth, cosim/XSI as available, and compare bit-exact outputs on the registered cases.
Report tool/version, part, clock constraint, II, latency, DSP/LUT/FF/BRAM, and timing evidence.

Measure cycles per output and effective input traffic for several registered `(L, VLMAX, T, VL)`
configurations against the input floor and the program-only baseline. Separate computed estimates
from RTL or trace measurements; include fill/drain, queue, instruction issue, and tail overhead.
Report failures and skipped gates explicitly.

**Gate:** RTL matches the interpreter and golden; Stage 1 targets are rechecked in the integrated
design; comparison table is complete. **Pause.**

### Stage 6: optional optimizations

Only after Stage 5, measure candidates such as multi-tap scheduling, a smaller rotation network,
overlapping input load/compute, coefficient reuse, or a specialized program schedule. Any hardware
change remains general-purpose. Keep an optimization only if it improves the pre-registered metric
without violating bit-exactness, queue semantics, resource limits, or timing.

**Gate:** each optimization is compared with Stage 5 under the same tool/device conditions and
removed if it does not pay. **Pause.**

### Stage 7: calibration and documentation

Calibrate the generic window/read and pass cost in the existing harness, and fit a resource/cost
model over pre-registered configurations. Keep held-out configurations untouched until the fit is
frozen, then score once. Document the ISA semantics, queue/halo contract, bank mapping, measured
limitations, fixed-point behavior, and comparison methodology in the vector-unit guide and FIR
example page. Label tool-version-specific measurements.

**Gate:** held-out scores and documentation are reviewed; existing non-slow test suite and applicable
XSI gates pass. **Pause.**

## Out of scope

- An FIR-only mode, tap counter, coefficient register, or FIR-named opcode in `waveflow/vecunit/`.
- General-purpose DRAM gather or arbitrary global memory addressing.
- Assuming masks, branches, or cross-member slides exist in the parent ISA without a separate
  decision and implementation.
- Claiming GPU shared-memory performance from the modulo-bank analogy.
- Replacing or specializing the existing CG, matched-filter, systolic, or dedicated FIR designs.

## References

| Topic | Reference |
|---|---|
| Vector ISA and strip-mining | RISC-V “V” Standard Extension for Vector Operations, Version 1.0, official specification: <https://docs.riscv.org/reference/isa/unpriv/v-st-ext.html> |
| Vector chaining | R. M. Russell, “The CRAY-1 computer system,” *Communications of the ACM*, 1978. doi:10.1145/359327.359336 |
| Decoupled access/execute | J. E. Smith, “Decoupled access/execute computer architectures,” ISCA 1982. doi:10.1145/1067649.801719 |
| Decoupled vector architectures | R. Espasa and M. Valero, “Decoupled vector architectures,” HPCA 1996. doi:10.1109/hpca.1996.501193 |
| RVV implementation and banked vector register file | M. Cavalcante et al., “Ara: A 1 GHz+ scalable and energy-efficient RISC-V vector processor with multi-precision floating-point support in 22 nm FD-SOI,” *IEEE TVLSI*, 2020. doi:10.1109/tvlsi.2019.2950087 |
| GPU shared-memory bank conflicts (analogy only) | NVIDIA, *CUDA C++ Best Practices Guide*, “Shared Memory”: <https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/index.html#shared-memory> |
| Parent unit plan and its references | `plans/vecunit_new.md` |

## Log

| Date | Stage | Event | Where |
|---|---|---|---|
| 2026-10-24 | -- | Drafted as a standalone generic-vector-unit plan; implementation and performance remain unclaimed | this file |
