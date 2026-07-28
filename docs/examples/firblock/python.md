---
title: Python
parent: Block FIR (state + fixed point)
nav_order: 4
---
# Python Model

This page builds the design in code: the two commands, the two custom leaves, and the composite that
wires them to the framework mem-streams. The whole design is
[`examples/fir_block/fir_block.py`](https://github.com/sdrangan/pysilicon/blob/main/examples/fir_block/fir_block.py).

## Running it

The build is a [`BuildDag`](../../guide/build/) behind the standard CLI, so every stage below is a
`--through` target. From the repo root:

```bash
# The first checkpoint — no toolchain, seconds. Runs the golden and fails on any mismatch.
python -m examples.fir_block.fir_block_build --through pysim

python -m examples.fir_block.fir_block_build --list-steps
python -m examples.fir_block.fir_block_build --status
```

The rest of the flow, in order:

| `--through` | what it does | needs |
|---|---|---|
| `pysim` *(default)* | run the `FirBlockTB` graph, check every block against the golden | — |
| `codegen_dut` | the `ap_ctrl_none` composite top, its TCL, the port map, the headers | — |
| `codegen_tb` | the XSI BFM harness, its main, and the scenario bundles | — |
| `csynth` | Vitis HLS C-synthesis; also re-emits `rtl_fir_block.f` | Vitis |

The parameters are CLI flags, which is also the handle a parameter sweep drives:

```bash
python -m examples.fir_block.fir_block_build --through csynth --samp-w 8 --ntap 32
python -m examples.fir_block.fir_block_build --through csynth --unroll-lane
```

The RTL rung is a test rather than a build step, because it needs Vivado `xsim` and a prior `csynth`:

```bash
pytest tests/examples/test_fir_block.py          # the pysim gates, incl. the falsification pair
pytest -m xsi tests/examples/test_fir_block_xsi.py
```

## Two message types

The host speaks one command; the pipeline forwards a different one internally.

```python
class FirCmd(DataList):
    """One host command on the boundary s_cmd (a plain word stream)."""
    elements = {
        "op":         {"schema": FirOpField, "description": "LOAD_TAPS or FILTER"},
        "src_off":    {"schema": Word32, ...},
        "n":          {"schema": Word32, "description": "sample count (tap count, or block length)"},
        "dst_off":    {"schema": Word32, ...},
        "zero_state": {"schema": Word32, "description": "FILTER: 1 = start from zeros, not the carry"},
        "tx_id":      {"schema": Word32, ...},
    }
```

`FirDesc` is the framed internal descriptor and carries the subset the *downstream* stages need — the
opcode, `n`, `dst_off`, `zero_state`, `tx_id`. It drops `src_off`, because by the time the descriptor
is travelling the read has already been issued.

Two details are load-bearing:

- **`op` is an `EnumField`**, so it reaches C++ as a real `enum class FirOp` and the kernel dispatches
  on `FirOp::LOAD_TAPS` rather than on an unchecked integer. That is also why `FirOpField` appears in
  `SCHEMA_CLASSES` in its own right — it needs its own generated header.
- **`n` is a *sample* count**, everywhere. The mem-streams speak words. See
  [samples and words](#samples-and-words) below.

## The framer

`FirCmdRx` is the only stage that sees a `FirCmd`. It reads one and frames the reader's command stream:

```python
def run_iter(self) -> ProcessGen[None]:
    w = int(self.mem_dwidth)
    cmd = yield from self.s_cmd.get(FirCmd)
    self._mark_start()
    desc = FirDesc(op=int(cmd.op), n=int(cmd.n), dst_off=int(cmd.dst_off),
                   zero_state=int(cmd.zero_state), tx_id=int(cmd.tx_id))
    memr = MemRCmd(addr=int(cmd.src_off), len=nwords(int(cmd.n), self.lw), fwd_bursts=1)
    yield from self.cmd_out.write(np.asarray(memr.serialize(word_bw=w), dtype=np.uint64))
    yield from self.cmd_out.write(np.asarray(desc.serialize(word_bw=w), dtype=np.uint64))
    self._log_firing()
```

`fwd_bursts=1` is what welds the descriptor to its data: the reader relays the next burst — the
`FirDesc` — as a *header* ahead of the data it fetches, so a descriptor can never be paired with the
wrong burst. **One read per job, for both opcodes**, which is what keeps the no-output opcode off a
special path.

{: .note }
> `_mark_start` and `_log_firing` are `@sim_only`. Instrumentation has no hardware meaning, and the
> extractor drops such calls wholesale — including their arguments. Without the marker a bare
> `self.now` read would trip the implicit-capture rule.

## The compute

`FirCompute` holds the state and does the work. Its `run_iter` reads the descriptor, reads the data,
dispatches, and frames the writer's stream:

```python
def run_iter(self) -> ProcessGen[None]:
    w = int(self.mem_dwidth)
    desc = yield from self.s_in.get(FirDesc)
    n = int(desc.n)
    nw = nwords(n, self.lw)          # the stream speaks WORDS; the descriptor carries SAMPLES
    data = yield from self.s_in.get(nwords_max=nw)
    self._mark_start()

    if int(desc.op) == FirOp.LOAD_TAPS:
        self.load_taps(np.asarray(data), n, self.taps)
        memw = MemWCmd(addr=int(desc.dst_off), len=0, fwd_bursts=1)
        ...
    else:
        y = self.filter_block(np.asarray(data), n, self.taps, self.carry, int(desc.zero_state))
        yield self.timeout(self._compute_delay(n))
        memw = MemWCmd(addr=int(desc.dst_off), len=nw, fwd_bursts=1)
        ...
```

The two branches are deliberately the *same shape* — descriptor, then command, then (for `FILTER`)
data. The only difference is `len`, which is `0` for a load. See
[the firing that writes nothing](./firblock.md#the-firing-that-writes-nothing).

### The arithmetic is a hook

`load_taps` and `filter_block` are `@synthesizable` — they are the **pysim twins** of the hand-written
C++ bodies, not their source. The generated kernel does not extract them; a human wrote the C++ and
these are what the golden runs. Keeping them side by side in one class is what makes the pair
reviewable.

```python
@synthesizable
def filter_block(self, x, n, taps: HwState, carry: HwState, zero_state: int):
    t = int(self.ntap)
    xs = unpack_samples(x, n, self.samp_cls, self.mem_dwidth)
    prev = np.zeros(t - 1, dtype=np.int64) if zero_state else np.asarray(carry.val, dtype=np.int64)

    # The window: buf[i : i+T] reversed is [x[i], x[i-1], ..., x[i-T+1]], aligned with h[0..T-1].
    buf = np.concatenate([prev, xs])
    win = np.lib.stride_tricks.sliding_window_view(buf, t)[:, ::-1]

    prod = mult(_as_fixed(win, self.samp_cls),
                _as_fixed(np.asarray(taps.val, dtype=np.int64), self.samp_cls))
    acc = fixed_sum(prod, axis=1)                 # +ceil(log2 T) integer bits, NOT +T
    y = quantize(acc, self.samp_cls)

    carry.val[:] = buf[len(buf) - (t - 1):]       # the next block's initial condition
    return pack_samples(np.asarray(y).reshape(-1), self.samp_cls, self.mem_dwidth)
```

Note that this is **one twin for both realizations**. `unroll_lane` changes the RTL's iteration
structure, not its arithmetic, so there is exactly one golden — see
[the two kernels](./kernels.md).

The `sliding_window_view` is the vectorized statement of the whole filter: `buf` is the previous tail
concatenated with this block, and every output is one row of the reversed window matrix dotted with
the taps.

## Samples and words

One conversion runs through the entire design, and getting it wrong is silent:

```python
def lane_width(mem_dwidth, samp_w):
    """LW — samples per transport word: max(1, MEM_DW // W)."""
    return max(1, int(mem_dwidth) // int(samp_w))

def nwords(n, lw):
    """Transport words for n samples at LW samples per word."""
    return (int(n) + int(lw) - 1) // int(lw)
```

Commands and descriptors count **samples**; `MemRStream` / `MemWStream` and the arena count **words**.
At the defaults (`MEM_DW = 32`, `W = 16`) `LW = 2`, so a 64-sample block is 32 words.

The packing itself is never hand-rolled. `pack_samples` / `unpack_samples` go through
`DataArray.serialize`, which is the same contract the generated
[`<stem>_array_utils`](../../guide/vectorization/hls/arrayutils.md) lane routines implement in C++:

```python
def pack_samples(stored, samp_cls, word_bw):
    arr = np.asarray(stored, dtype=np.int64).reshape(-1)
    cls = DataArray.specialize(samp_cls, max_shape=(max(len(arr), 1),))
    return np.asarray(cls(arr).serialize(word_bw=int(word_bw)), dtype=np.uint64)
```

{: .warning }
> An earlier version of this example hand-rolled the packing on both sides and asserted one sample per
> word. It was *correct*, because `LW` happened to be 1 — and it would have silently diverged from the
> RTL the moment a width changed. If a kernel here grows a `.range()` to pull elements out of a word,
> that is the bug and not the idiom.

## The composite

```python
self.rx = FirCmdRx(..., mem_dwidth=w, samp_w=int(self.samp_w), clk=self.clk)
self.rstream = MemRStream(..., mem_dwidth=w, inband=True, platform_dir=self.platform_dir)
self.compute = FirCompute(..., ntap=int(self.ntap), samp_w=int(self.samp_w),
                          samp_i=int(self.samp_i), unroll_lane=bool(self.unroll_lane), ...)
self.wstream = MemWStream(..., mem_dwidth=w, inband=True, emit_done=True, ...)
for c in (self.rx, self.rstream, self.compute, self.wstream):
    self.add_comp(c)

_sif("cmd_rd", self.rx.cmd_out, self.rstream.s_cmd)     # [MemRCmd | FirDesc]
_sif("rdata", self.rstream.m_out, self.compute.s_in)    # [FirDesc | taps-or-block]
_sif("wdata", self.compute.cmd_out, self.wstream.s_in)  # [MemWCmd | FirDesc | y]

self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
```

Four sub-components, three framed internal edges, four boundary ports. Every internal edge is framed
(`framed=True`); only the host boundary is a plain word stream. `boundary` names which ports become
top-level ports when the graph is lowered — everything else becomes an internal FIFO.

Two of the four stages are **framework** components with shipped, XSI-verified timing, so this design
owns exactly one custom thing worth calibrating: the compute.

## Where to next

- [Testbench](./testbench.md) — the graph that drives this, and the golden that judges it.
- [The two kernels](./kernels.md) — the C++ the two `@synthesizable` hooks above are twins of.
- [DUT codegen](./codegen_dut.md) — how this graph becomes an `hls::task` top.
