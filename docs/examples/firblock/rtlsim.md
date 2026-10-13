---
title: RTL simulation
parent: Block FIR with state
nav_order: 8
summary: "The rung that answers what nothing earlier can: does the state actually survive between firings? C-synthesis can say the emitted static compiles and infers a memory, but a static swept by ap_rst would synthesize identically and quietly zero itself every job. A free-running composite also has no scheduler, so a token-count bug hangs or drops a job rather than producing a wrong answer."
---
# RTL simulation — does the state actually survive?

The system flow drives the synthesized RTL through real handshakes, one clock at a time, in Vivado
`xsim`. For this design it answers a question nothing earlier in the flow can.

C-synthesis can say the emitted `static` compiles and infers a memory. It **cannot** say the value
survives between firings: a static swept by `ap_rst` would csynth identically and then quietly zero
itself every job, and the design would look right and be wrong. Only a real RTL run can tell.

That is not the only thing this rung is for. A free-running `ap_ctrl_none` composite has no
scheduler, so a token-count bug does not produce a wrong answer: it **hangs**, or silently drops a
job. The [pysim](./testbench.md) can miss that class entirely; the RTL cannot.

## Running it

It needs Vitis HLS to C-synthesize the top, and Vivado `xsim` with a C++ compiler to run it:

```bash
python -m examples.fir_block.fir_block_build --through csynth   # produces the RTL
python -m examples.fir_block.fir_block_system_build             # the system flow, end to end
pytest -m xsi tests/examples/test_fir_block_xsi.py              # the gate
```

The gate runs the system flow with `synth="check"`: a missing or stale csynth fails the run and
names it, rather than passing on RTL the gate did not produce.

To check the other realization, rebuild and rerun:

```bash
python -m examples.fir_block.fir_block_build --through csynth --unroll-lane
pytest -m xsi tests/examples/test_fir_block_xsi.py
```

## The system: the testbench graph, cut at the kernel

The [`FirBlockTB`](./testbench.md#the-testbench-is-a-graph) graph is what pysim runs. For the RTL it
is wrapped in a system that states the **cut**: the `FirBlock` kernel is the design under test, and
everything else is testbench. That includes the driver, the sink, the memory, and the crossbar that
models the two `m_axi` bundles' contention in pysim.

```python
@dataclass
class FirBlockSystem(GraphSystem):                 # examples/fir_block/fir_block_sim.py
    program: tuple = DEFAULT_PROGRAM
    blk: int = DEFAULT_BLK

    def graph(self) -> FirBlockTB:                 # the testbench graph pysim runs
        self.runner = FirBlockSim(program=tuple(self.program), blk=int(self.blk), name="xsi_tb")
        return self.runner.tb

    def dut_modules(self, g) -> list:              # the cut: the kernel alone
        return [g.dut]

    def write_inputs(self, root) -> None:          # the scenario: the one writer pysim uses
        self.runner.write_scenario(root)

    def check(self) -> FirBlock:                   # the golden, on a pysim run
        return self.runner.check()
```

From that, the system flow derives the rest:

- **the system top**: the csynth'd kernel inside it, with `s_cmd` / `s_done` as stream ports and the
  two `m_axi` bundles as AXI4 master groups;
- **the harness**: each testbench participant's BFM model bound to those ports, so the stream driver
  and sink, and a flat memory behind both bundles;
- **the scenario**: `vectors/s_cmd`, `vectors/mem_in`, `vectors/golden`, and the cycle bound in
  `vectors/run.json`.

That is the point of building the testbench as a graph rather than a script: **one statement, two
backends.** The pysim run and the RTL run are walks of the same graph, so they cannot describe
different tests. The [mem_copy system page](../memcpy/system.md) walks the same derivation in detail.

Each run starts from a fresh copy of the scenario, so a broken build cannot pass on the last run's
output. A cached elaboration is reused only if its content stamp matches the RTL just written: a
stale file list plus a cached simulation library is how an XSI run goes green while proving nothing.
After the run, `compare` checks that the sink's completions and the memory's final arena hold the
same words at RTL as in pysim.

## What the gate asserts

Exit code zero proves nothing here, so `check_xsi_outputs` reads the RTL run's recordings (the
memory's arena in `vectors/out`, the sink's completions in `vectors/s_done`) and checks two things:

```python
for j, step in enumerate(s for s in tb._steps if s["op"] == FirOp.FILTER):
    # WORDS, not samples: at LW samples per word an n-sample block occupies ceil(n/LW) words.
    dst, nw = step["dst"], step["nw"]
    got, want = out[dst:dst + nw], golden[dst:dst + nw]
    if not np.array_equal(got, want):
        raise AssertionError(...)

s_done = read_burst_bundle(vdir / "s_done")[0]
assert len(s_done) == len(tb._steps) * dw, (
    f"... expected {len(tb._steps)}*{dw} (one FirDesc echo per command, LOAD_TAPS included)")
```

**Bit-exact against the golden**, block by block — and the golden is the
[stateless one](./testbench.md#the-golden-never-mentions-state), so agreement *is* the statement that
both flavours of state survived in real hardware, across a mid-stream tap reload.

**One completion per command**, `LOAD_TAPS` included. Without this a dropped no-output job would look
like a pass: the blocks that *did* run would still match.

## What it found

This gate earned its place on its first real run, and the story is worth keeping because it is the
argument for having it.

csynth was clean. The first output block matched. And the RTL was still wrong — the delay line had been
seeded with the [MAC-time invariant rather than the pre-shift one](./kernels.md#the-seeding-rule),
which drops the newest carry sample. It was invisible to csynth, and invisible in block 1 because
`zero_state` starts that block from zeros so it never reads the carry. Only block 2's first samples
were wrong.

Two things made it findable rather than merely detectable:

- the golden is **bit-exact**, not a tolerance. The wrong output was entirely plausible — a filter with
  a slightly-off history still looks like a filter. A tolerance would have swallowed it.
- the program runs **three filter firings**, not one. A single-block scenario never touches the carry.

Diagnosis did not proceed by guessing. Given the RTL's block-2 output, the candidate wrong-carry
hypotheses were enumerated and scored against it in numpy — a one-sample shift matched 64/64 — which
named the bug instead of suggesting one. It was then confirmed on a hand-checkable `T = 4`,
`blk = 8` build before the fix went in.

{: .note }
> The generalizable move: when RTL disagrees with a golden, **enumerate candidate corrupt states and
> score each against the observed output.** It converges far faster than re-reading the kernel, and it
> distinguishes "the state is stale" from "the state is shifted" from "the state is ignored", which
> look identical in a diff.

## Both realizations, one golden

Both kernels are verified through this same gate, against the same golden:

| realization | csynth | XSI |
|---|---|---|
| `serial` (default) | clean | bit-exact, 5/5 completions |
| `unroll` (`--unroll-lane`) | clean | bit-exact, 5/5 completions |

That is the property that makes the pair a QoR probe rather than two designs: they are
*numerically indistinguishable* and differ only in resources and rate. If they ever disagreed here,
comparing their DSP counts would be comparing two different filters.

## Where to next

The remaining rung is the **parameter sweep** — rebuilding across sample widths and both realizations
and collecting resources and throughput per point. That is where the two kernels stop being an
either/or and become a curve.
