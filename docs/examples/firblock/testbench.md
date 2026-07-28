---
title: Testbench (Python)
parent: Block FIR (state + fixed point)
nav_order: 5
---
# Testbench — how do you know stateful hardware is right?

Everything so far builds the filter. This page is about the harder half: writing a check that can
actually *catch* a state bug. It is the objective most easily skipped, and the one that earned its keep
here — the gate on this page is what found a bug that C-synthesis was perfectly happy with.

Run it:

```bash
python -m examples.fir_block.fir_block_build --through pysim   # the golden, as a build step
pytest tests/examples/test_fir_block.py                        # the full gate set, incl. falsification
```

## The problem with checking a stateful design

The obvious check is to compute the expected output the same way the DUT does — block by block,
carrying the tail — and compare. It is also nearly worthless: **a bug in how the carry is stored or
loaded is very likely to appear in both**, so the check agrees with the implementation and says
nothing. The one bug this kernel actually had (a delay line seeded one slot off) produces entirely
plausible numbers, so eyeballing would not have caught it either.

## The golden never mentions state

The fix is to make the reference **structurally unable** to share the bug. `FirBlockSim._golden`
filters the *whole signal* sample by sample, indexing history globally:

```python
for bi, blk in enumerate(blocks):
    h = _as_fixed(taps_by_set[tap_of_block[bi]], samp_cls)
    for i in range(n):
        g = base[bi] + i                       # index into the global signal
        win = np.array([sig[g - k] if g - k >= 0 else 0 for k in range(t)], dtype=np.int64)
        acc = fixed_sum(mult(_as_fixed(win, samp_cls), h))
        ys[i] = int(np.asarray(quantize(acc, samp_cls)).reshape(-1)[0])
```

There is no carry here, and no block boundary — just `x[i-k]`, zero before the start, with the
coefficient set switched at each reload. So the assertion

> block-wise output == whole-signal convolution

**is** the statement "the carry is correct". The DUT sees one block at a time and has to reconstruct
that history from `self.carry`; the golden gets it for free by indexing an array. If the carry is
wrong by so much as one sample, the two disagree.

This is the transferable idea: **write the reference in the frame the design cannot use.** The design
is incremental, so make the reference global. Then agreement is evidence rather than coincidence.

{: .note }
> The comparison is **bit-exact**, not a tolerance. Both sides run the same declared format and the
> same derived accumulator (see [Fixed point](./fixedpoint.md)), so there is no rounding difference to
> absorb — and a tolerance would have hidden the seeding bug, whose outputs were plausible.

## The scenario is chosen, not arbitrary

```python
DEFAULT_PROGRAM = ("load", "filter", "filter", "load", "filter")
```

Every element earns its place:

| element | what it catches |
|---|---|
| ≥ 3 `filter` firings | the carry itself. The **first** block starts from zeros (`zero_state`), so it never reads the carry — a one-block test proves nothing about state |
| a `load` *after* the first filter | that held state is genuinely replaceable; a design that latched the taps once and ignored reloads passes without it |
| `load` first **and** mid-stream | the no-output opcode in both positions — first, and between two data-carrying jobs |

That last one matters because a stage that mishandles a no-output firing often only wedges when the
firing is *between* two others.

## Guarding the guard

A scenario can be weakened until it silently proves nothing, so two tests check the *test*:

```python
def test_gate_program_reloads_mid_stream():
    steps = list(DEFAULT_PROGRAM)
    assert steps.count("filter") >= 3, f"program {steps} exercises the carry too little"
    first_filter = steps.index("filter")
    assert any(s == "load" for s in steps[first_filter:]), (
        f"program {steps} never reloads mid-stream — a stale tap set would pass")


def test_tap_sets_and_stimulus_discriminate():
    h0, h1 = _tap_set(0, 32, samp), _tap_set(1, 32, samp)
    assert np.all(h0 != h1), "the two tap sets overlap — a missed reload could pass"
    x = _stimulus(64, 0, samp)
    assert np.count_nonzero(x) > len(x) // 2, "stimulus is mostly zeros; a wrong carry would hide"
```

The two coefficient sets are deliberately *different shapes* (a decaying window versus a modulated
one), so a stale tap set gives a loudly wrong answer rather than one off by a few LSBs. And a
mostly-zero stimulus would hide a wrong carry, because multiplying stale history by zero looks fine.

## Proving the gate can fail

A gate that cannot fail is not a gate. Each flavour of state gets its own falsification test — a check
that only noticed one of them would let the other rot:

```python
def test_broken_carry_fails_the_gate(monkeypatch):
    """Ignore the carry (start every block from zeros) and the golden must reject it."""
    orig = FirCompute.filter_block
    monkeypatch.setattr(FirCompute, "filter_block",
                        lambda self, x, n, taps, carry, zero_state: orig(self, x, n, taps, carry, 1))
    with pytest.raises(AssertionError, match="fir_block block"):
        FirBlockSim().run()


def test_broken_tap_reload_fails_the_gate(monkeypatch):
    """Honour only the FIRST LOAD_TAPS; the golden must reject the block after the reload."""
    ...
```

Both fail exactly where they should: the broken carry at block 1 sample 0, the ignored reload at
block 2 sample 0. Running these is how you learn the gate is *load-bearing* rather than decorative.

## Completions, not just correctness

Correct output is not enough, because a wedged pipeline can still produce correct output for the jobs
that *did* complete. So the check counts completions and their order:

```python
bursts = tb.done_sink.words
assert len(bursts) == len(tb._steps), (
    f"fir_block: {len(bursts)} completions on s_done, expected {len(tb._steps)} "
    f"(one per command, LOAD_TAPS included)")
got_ids = [int(FirDesc().deserialize(np.asarray(b), word_bw=w).tx_id) for b in bursts]
assert got_ids == list(range(len(tb._steps))), ...
```

`tx_id` is echoed through the pipeline and back, so this asserts that **every** command — including
the two `LOAD_TAPS` jobs that write no data — produced exactly one completion, in issue order. Without
it, dropping the load's completion would look like a pass.

## The testbench is a graph

`FirBlockTB` is a `FreeRunMod` composite, not a script: a `StreamDriver` on `s_cmd`, a `StreamSink` on
`s_done`, one `MemoryMod` arena behind both `m_axi` bundles via a crossbar, and the DUT.

```python
xbar = AXIMMCrossBarIF(..., nports_master=2, nports_slave=1, bitwidth=w)
xbar.bind("master_0", self.dut.m_in)           # gmem0 read (taps, blocks)
xbar.bind("master_1", self.dut.m_out)          # gmem1 write (y)
xbar.bind("slave_0", self.mem.s_mm)
```

Being a walkable graph is what lets the *same structure* generate the XSI harness — see
[RTL simulation](./rtlsim.md). One statement, two backends: pysim builds the graph and runs it, and the
XSI generator builds the same graph and emits a C++ BFM harness from it, so the two cannot end up
describing different tests.

`write_scenario` is likewise the single scenario writer both backends read, materializing
`vectors/s_cmd`, `vectors/mem_in` and `vectors/golden` as burst bundles.

## The arena is laid out in words

```python
self.lw = lane_width(self.mem_dwidth, self.samp_w)
...
nw = nwords(n, self.lw)
src = cur; cur += nw
dst = cur; cur += nw
```

Region offsets and sizes are **word** coordinates while `n` on the command stays a **sample** count.
Conflating the two is silent: it produced a check that compared `n` words of a `ceil(n/LW)`-word
region and reported a mismatch in untouched arena, *after* the RTL was already correct.

## Where to next

- [The two kernels](./kernels.md) — the C++ this golden judges.
- [RTL simulation](./rtlsim.md) — the same graph, driving real RTL.
