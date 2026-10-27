---
title: A matched filter as a vector program
parent: Examples
nav_order: 24
has_children: true
example_dir: examples/vecunit_mf
summary: "A multi-antenna matched filter, Y[i, j] = conj(X[i]) · R[i, j], as a program for the programmable vector unit (VecUnit): antennas on lanes, samples as elements strip-mined in chunks of VLMAX, the reference broadcast from member 0 and conjugated in the multiply. The same RTL template as CG at another build configuration (VLMAX 64, few groups), bit-exact with a NumPy golden at RTL. What it shows: how close a streaming program comes to its input floor -- a frame every 1,408 cycles against 1,152 input words (1.22x, Vitis 2024.1), the execute task binding just above the loader at VLMAX 64 -- and how to count the instructions a chunk so issue stays small."
---

# A matched filter as a vector program

The second application of [the vector unit](../../guide/dsp/vecunit/index.md), next to
[CG](../vecunit_cg/index.md): a multi-antenna matched filter in the frequency domain,

```
Y[i, j] = conj(X[i]) · R[i, j]      i over the samples of a frame, j < nrx over the receive antennas
```

CG is compute with short vectors (`VL` = 8 rows); this is a stream with long ones. It exercises what CG
does not: the conjugate and broadcast operand forms, `VLMAX` 64 (the same RTL template with other
build parameters), and a program whose rate is set by its input.

## Learning objectives

1. Map a streaming kernel onto a vector unit: **antennas on lanes, samples as elements**, a frame
   strip-mined in chunks of `VLMAX`.
2. Use the **broadcast and conjugate** operand forms to reuse one reference vector across every
   lane.
3. Count a program's **instructions a chunk** and choose `VLMAX` so instruction issue never binds.
4. Measure a frame against its **input floor**, and find which task binds.

## The program

| Matched filter | Unit |
|---|---|
| antenna `j` | a group member, one lane an antenna; `nrx > L` takes `ceil(nrx / L)` column groups, and `L` need not divide `nrx` |
| sample `i` | an element; a chunk of `VL ≤ VLMAX` samples at a time |
| `R` (samples × `nrx`) | an input block per chunk, `k` = the chunk, `n` = `nrx` |
| `X` (samples) | a single-column input block (`k × 1`), read into member 0 |
| `Y` | an output block per chunk, the layout of `R` |

Per chunk the execute program reads `X` once into `v0` -- member 0 holds it, the other lanes are
the block's zero padding -- then runs one chained pass per column group: read `R`'s group into `v1`,
`v2 = conj(v0[member 0]) · v1`, and write `v2` straight to the output block:

```
wf.setvl k
wf.vlse v0, qi0, 0, 1 ; wf.qpop qi0
for g:  wf.vlse.c v1, qi1, g, G ; wf.vmulc.vb.c v2, v0, v1 ; wf.vsse qo0, v2, g, G
wf.qpop qi1 ; wf.qpush qo0
```

That is `5 + 3·ceil(nrx / L)` instructions a chunk (`mf_program.instructions_per_chunk`), 11 at eight
antennas on four lanes, against `VL · ceil(nrx / L)` = 128 elements: at `VLMAX` 64 the instruction
words are a few percent of the chunk. The load program alternates `wf.ld` of `X` and of `R` per chunk;
the store program is one `wf.st` per chunk, tagged with the chunk's index.

The formats are the example's: `X` 16 bits with 2 integer, `R` 16 with 3, both rounding; `Y` 16 with
3, truncating -- so the unit's union datapath carries both rounding modes. The golden
(`mf_golden.py`) is NumPy on stored integers: the exact complex product, one quantization to `Y`.

## Running it

```bash
python -m examples.vecunit_mf.harness --root /tmp/vu_mf --through compare --workload steady
python -m waveflow.vecunit.report /tmp/vu_mf
pytest tests/examples/test_vecunit_mf.py              # the program against the golden, bit for bit
pytest -m xsi tests/vecunit/test_unit_xsi.py          # the RTL gates
python -m examples.vecunit_mf.mf_runtime --rtl --root /tmp/vu_mf_rt   # untimed vs pysim vs RTL, by size
```

What a frame costs to simulate, against the untimed models and the RTL, from 256 to 16,384 samples,
is measured in [Evaluation: the matched filter on the vector unit](../../evaluation/sim_speed/vecunit_mf.md);
the RTL across antenna counts, chunk lengths, word widths and lanes, against each frame's bounds, in
[Timing at RTL](timing.md).

| Workload | Frames |
|---|---|
| `test` | every antenna-count shape -- below, at and above `L` -- with whole and short last chunks |
| `steady` | four frames of four 64-sample chunks at eight antennas |

## The input floor

A frame of 256 samples at eight antennas moves, per chunk, `X` (64 values, 32 words at two 32-bit
complex values a 64-bit word) and `R` (512 values, 256 words): **1,152 input words a frame**, the
fewest cycles a frame can take at one word a cycle. Vitis HLS / Vivado xsim 2024.1, `xczu48dr` at
4 ns, the `steady` workload:

| | cycles a frame | against the floor |
|---|---|---|
| fused runs (Stage 6 of the plan) | **1,408** | 1.22x |
| one pass at a time (Stage 5) | 1,448 | 1.26x |

The gap is the cost of programmability for a streaming kernel: each chunk pays the execute task's
decode and the two runs' pipeline fill, which a dedicated kernel would not. The execute task binds, at
about 352 cycles a chunk; the loader needs about 320 -- 288 words, but `X`'s single-column block
lands a value a cycle (`L` does not divide `n` = 1), 64 cycles for its 32 words.

pysim gives this frame 1,513 cycles, 7.5 % over the RTL: its timing was calibrated on the CG
configuration, whose decode loop is deeper (depth 23 against 14 in this build) -- see
[the guide](../../guide/dsp/vecunit/timing.md#the-calibrated-models).
