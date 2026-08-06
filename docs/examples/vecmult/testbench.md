---
title: Testbench
parent: Vector multiply resource modeling
nav_order: 3
audience: python
summary: "Two bodies, one behaviour, and the rung that makes them agree. The pysim testbench drives one job through the SimPy model; the csim rung replays that exact job through the hand-written C++ and compares element for element. csynth consumes the verdict, so a design whose twin check failed cannot contribute a resource measurement."
---

# Testbench

`VecMult` has **two implementations of one behaviour**: `run_iter` in Python and `vec_mult_task.h` in
C++. That is a liability until something checks them against each other — and the check is easy to
believe you have when you do not.

- `pysim` proves the **Python** body is right.
- `csynth` proves the **C++** body compiles and schedules.
- Neither proves they compute **the same thing**.

Every rung can be green while the two disagree. Closing that is what this page is about.

## The pysim harness

An in-memory graph: a source that issues one job, the DUT, and a capture.

```python
@dataclass
class VecMultTB(FreeRunMod):
    """source -> DUT -> capture, over one stream in and one out."""
```

Both ends use the endpoint's own schema methods (`write` / `get`), never a hand-rolled word split, so
the packing under test is the *same* generated serializer on both sides of the comparison.

```python
got, exp, echoed = run_one(dwid=64, vlen=4096, n=4093, seed=0, tx_id=0x5A5A)
```

`n = vlen - 3` by default, which is deliberately **ragged**: not a multiple of `LW`, so the partial
final beat is exercised. A full-length job never reaches it.

{: .note }
> This example writes its vectors **in memory**, not as burst bundles. `state_toy` and `mem_copy`
> write bundles because the same bytes have to drive an RTL testbench too; `vecmult` has no RTL rung,
> so a file round-trip would buy nothing and cost the reader a level of indirection.

## The csim twin check

The `csim` rung replays that exact job through the C++ and compares:

```text
source -> pysim -> codegen_dut -> csim -> csynth -> resources
```

`pysim` writes the stimulus and the expectation to `data/`; `vec_mult_tb.cpp` reads them back, packs
them onto an `hls::stream`, calls the task, and checks every sample plus the `tx_id` echo.

```cpp
vec_mult_task<VM_DWID, VM_VLEN>(s_in, z_out);
```

Three details that matter:

**It calls the task function, not the generated top.** `vec_mult` is `ap_ctrl_none` with an
`hls::task` — it never returns, so csim of it would spin forever. The task body is the artifact under
test anyway; the top is generated and carries no arithmetic.

**The expectation comes from Python.** A testbench that computed its own expected output would be
checking the C++ against itself. `data/` holds `x`, `y` and `z_expected`, all written by the pysim
rung, so there is exactly one definition of the job under test.

**`csynth` consumes the verdict.** A design whose C++ disagrees with its golden cannot reach synthesis
and therefore cannot contribute a resource measurement:

```python
consumes = ["vec_mult_cpp", "run_tcl", "csim_verdict"]
```

## The gate can actually fail

A green gate that cannot go red is worse than no gate, because it buys false confidence. This one was
verified by corrupting a single expected sample:

```text
TB MISMATCH at 7: got 26649 expected 26650
WAVEFLOW_CSIM_FAIL: 1 mismatch(es) over n=4093
```

Worth doing once for any check you intend to rely on.

## What is not checked

**Cycle behaviour.** csim runs the C++ as software: it proves the arithmetic and the framing, not the
schedule, the back-pressure, or the handshakes in RTL. The resource counters this example exists for
are settled at C-synthesis, so nothing here needs RTL — but the gap is real and worth naming rather
than leaving implied.

For a design that closes it, see [`fir_block`](../firblock/) and its
[XSI gate](../firblock/rtlsim.md), which drives the elaborated RTL cycle by cycle through a BFM.

## The toolchain-free gates

`tests/examples/test_vecmult.py` runs with **no Vitis installed**, and covers what would otherwise rot
silently:

| test | what it pins |
|---|---|
| `test_pysim_matches_golden` | every lane width, LW=2 … 16 |
| `test_ragged_lengths` | `n ∈ {1, 7, 63, 64, 65, 253}` — the partial-beat off-by-ones |
| `test_response_echoes_the_transaction_id` | the transaction closes |
| `test_runtime_length_does_not_change_the_hardware` | `n` is runtime, `vlen` is structure |
| `test_golden_wraps_rather_than_saturates` | the golden truncates like the C++, not saturates |
| `test_dsp_prior_is_exact_on_every_point` | the DSP rule against the committed corpus |
| `test_bram_prior_is_exact_on_every_point` | the BRAM rule, all 16 |
| `test_crossbar_basis_predicts_lut_and_ff_held_out` | the fit generalizes, leave-one-out |
| `test_naive_linear_basis_is_not_good_enough` | *why* the basis has quadratic terms |

The last one is the unusual entry: it asserts that the obvious feature set is **>30% wrong**. Without
it the quadratic terms would read as unexplained curve-fitting, and a later "simplification" back to
linear-in-width would look harmless.

## Next

- [DUT codegen](./codegen_dut.md) — the `ap_ctrl_none` top this rung synthesizes, and the headers
  the body includes.
- [The sweep](./sweep.md) — 16 design points, and the grid that separates the two BRAM regimes.
