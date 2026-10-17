# Reference plan: 02 Hamming (8,4) soft decoder

What a good plan for [02_hamming_decode.md](../02_hamming_decode.md) looks like (tree of 2026-10-17: the
`stream_kernel` frame, `docs/guide/systems/`, `docs/guide/streams/`).  The answer key, never shown to an
agent.  The rung tests **one** thing: turning a stated two-stage design into a composite of free-running
stages -- the algorithm and the table are given, so nothing here should need coding theory.

## Expected

- **References:** `stream_kernel` (one free-running module on framed streams, the protocol, the harness,
  the fatal stop); a composite of free-running stages (`docs/guide/comp_codegen/freerunning_composite.md`,
  `interleaver` or `memcpy` for `add_comp` / `add_if`); the pattern page's pipeline form (the command
  travels with the data, the last stage answers).
- **Modules:** a composite `FreeRunMod` `HammingDecode` with two leaf `FreeRunMod`s, `Correlate` and
  `Select`, joined by an internal framed `StreamIF`; boundary ports framed, 64 bits.
- **Types:** the LLR `FixedField(8, 6)`, a codeword of eight (a `DataArray` or a `DataList`, 64 bits);
  `c[k]` `FixedField(12, 10)`; `bits` a 4-bit `IntField`; the output entry a `DataList` (`bits`, `cmax`).
- **The link:** per command, a header (`tx_id`, `ncw`) then one message per codeword carrying the 16
  correlations (16 x 12 = 192 bits -- a wide internal word, or several words a codeword with the rate
  argued), and how an early TLAST from `correlate` reaches `select` (a count in an end message, or the
  link burst closed short with TLAST -- framed internal edges carry TLAST).
- **`correlate`:** one word per iteration at II=1, the 16 sums hand-coded from the table (or a `static
  const` table under full unroll -- either; the plan should say the sums are fixed adder trees, and how
  the table is checked against G: a test in the golden).
- **`select`:** a 16-way compare at II=1, ties to the lowest index -- a comparison tree whose tie rule is
  right (`>` against the running best scanning upward, or `>=` scanning downward), and the timing of a
  16-way tree at 250 MHz considered (it may need its own register stage -- *do not decide, compute and
  commit in one iteration*).
- **The harness:** `harness` on each stage alone, then on the composite; the stages' bodies RTL-harnessed
  alone through `KernelTask(framed=...)` (the better answer) or in pysim only.
- **The build:** codegen (schemas, array utils for the LLR and the entry), `add_system_steps` on the
  composite's harness, `compare`; the fatal-error scenario checked by recordings.
- **Throughput:** one codeword a cycle on scenario 8, from the RTL cycle count or calibrated phases.

## Must have

1. A composite of two leaf `FreeRunMod`s with an internal framed stream -- not one kernel.
2. Schemas for every message, the link included; the boundary exact; layouts given.
3. An independent golden with the worked examples and the tie rule; the table checked against G.
4. `harness` for the testbench; the build through `add_system_steps` to `compare`.
5. Hand-written HLS bodies behind `kernel_task()`, II=1 each, citing the loop optimization page.
6. The fatal error carried from `correlate` to `select`, and nothing after it.

## Counts against a plan

One kernel doing both stages; hand packing; a tie rule that picks the highest index; `SeqTB` or cosim; a
hand-written testbench; an error answered and then carried on from.

## Recorded, not scored

Frame chosen; how the link is laid out and why; whether `select`'s tree gets its own register stage;
whether `KernelTask(framed=...)` was found; pages read; tokens.
