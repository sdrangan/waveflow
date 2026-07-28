---
title: Stream-of-Blocks Timing Analysis
parent: Timing Analysis Tools
nav_order: 7
has_children: false
---

# Stream-of-Blocks Analysis

A [stream of blocks](./sob.md) (SOB) is how a free-running component gets **random
access** to a buffer: instead of consuming a stream in order, it acquires a whole block, reads or writes it
at arbitrary indices, and releases it — a ping-pong pair of block RAMs behind a lock handshake. Because an
SOB lowers to a **block-RAM port** rather than an AXI stream, its activity lives on a different set of nets
than [AXI4-Stream](./axistream.md) or [AXI4-MM](./aximm.md). The `VcdParser` has `add_sob_signals` /
`extract_sob_span` for them.

## The example component

We use `il_compute`, the gather stage of the [interleaver](../../examples/interleaver/) (it is small). It
computes `Y[i] = X[P[i]]`, so it holds **three** SOB buffers:

- `p_blk`, `x_blk` — two **input** (read) blocks: the indices `P` and the source `X`.
- `y_blk` — one **output** (write) block: the result `Y`.

Each firing it acquires the two read blocks and the write block, runs the gather loop, and releases them.

## Loading the SOB signals

After creating a [`VcdParser`](./vcd.md) and its clock, load a block's port nets by naming the owning
component and the buffer:

```python
vp = VcdParser(vcd)
clk_name = vp.add_clock_signal()

# the output (write) block of the gather
y_sigs = vp.add_sob_signals('il_compute', 'y_blk')
print(y_sigs)
# {'we':   '...il_compute_..._y_blk_we0',
#  'ce':   '...il_compute_..._y_blk_ce0',
#  'addr': '...il_compute_..._y_blk_address0[7:0]',
#  'data': '...il_compute_..._y_blk_d0[31:0]',
#  'lock': '...il_compute_..._y_blk_write'}

# an input (read) block — the side is auto-detected
p_sigs = vp.add_sob_signals('il_compute', 'p_blk')
print(p_sigs)   # {'ce', 'addr', 'lock'}  — a read port has no write-enable
```

The first argument is a **substring** of the (mangled) component instance name — `'il_compute'` finds
`il_compute_inband_task_64_256_U0_...` — and the second is the buffer's base name. You can read the exact
net names off the signal printout; see [parsing the VCD outputs](./parsing.md). Each matched net is added
with a short name (`y_blk_we`, `y_blk_addr`, …) so it renders cleanly on a timing diagram.

## The signals

An SOB buffer lowers to a Vitis **block-RAM port**, whose nets Vitis lifts into the top scope. The roles
`add_sob_signals` picks up:

| role   | net              | what it is                                                             |
|--------|------------------|------------------------------------------------------------------------|
| `we`   | `<blk>_we0`      | **write-enable** — high on each cycle a word is written (write block only) |
| `ce`   | `<blk>_ce0`      | chip-enable — high on each access (read or write)                      |
| `addr` | `<blk>_address0` | the element **index** being accessed                                   |
| `data` | `<blk>_d0` / `_q0` | the word written (`d0`) or read (`q0`)                               |
| `lock` | `<blk>_write` / `_read` | the SOB **lock handshake** — asserted while the component holds the block |

`add_sob_signals` **auto-detects the side**: a `we0` net means a **write** block (the component *produces*
into it); no `we0` means a **read** block (it *consumes* from it). Pass `side='write'` / `'read'` to force
it.

## When is the block "active"?

There are two different answers, and the distinction matters:

- **Held** — the **lock** (`<blk>_write` / `_read`) is asserted for the *entire* firing the component holds
  the block, from acquire to release. That envelope **includes** any time the component was stalled — e.g.
  the gather still holding the `y_blk` write-lock while it waits, mid-loop, for a full output ping-pong to
  drain. So the lock tells you *when the block is reserved*, not when work is happening.
- **Working** — the **write-enable** `we0` (or, for a read block, the chip-enable `ce0`) pulses on *exactly*
  the cycles a word actually moves. For a fully-pipelined write loop (II = 1) it is high for a **contiguous
  run of `N` cycles**, where `N` is the number of elements written. That run is the block's real work
  window — the loop's own time.

For measuring a kernel's cost you almost always want the second one. `extract_sob_span` returns those
contiguous write-enable windows:

```python
windows, clk_period = vp.extract_sob_span(clk_name, y_sigs)
for w in windows:
    print(f"firing: cycle {w['start']}..{w['end']}   span {w['span']}")
# firing: cycle 329..585   span 256
# firing: cycle 631..887   span 256
# firing: cycle 933..1189  span 256
# firing: cycle 1235..1491 span 256
```

Each window is **one firing's write burst**; `span` is the number of active cycles = elements written =
the gather loop's own cycle count. If a window **splits** — a gap appears in `we0` mid-burst — the loop
**stalled** (typically the output block filled because the downstream stage drained slowly). So a *single
contiguous window per firing* is the "ran clean, no backpressure" signal, and the count of windows against
the number of firings is a stall check.

> Why the write-enable and not the lock: the lock is held across the whole firing *including waits*, so it
> over-counts the block's real work. `we0` marks only the cycles a word moves. This is the write-enable /
> occupancy distinction from [trace pitfalls](./trace_pitfalls.md) — but note the caution there is about a
> **posted `m_axi` store**; a block-RAM write is *not* posted, so here the write-enable window genuinely
> *is* the work window.

## Plotting the timing diagram

Because `add_sob_signals` added the block's nets, its activity is visible on a diagram just like any other
interface:

```python
sig_list = vp.get_td_signals()
td = TimingDiagram()
td.add_signals(sig_list)
ax = td.plot_signals(add_clk_grid=True)
_ = ax.set_xlabel('Time [ns]')
```

You will see the `we` pulses over the burst, the `addr` ramp beneath them, the `data` being written, and
the `lock` envelope spanning the whole firing around them — the "held" vs "working" distinction, drawn.

## See also

- [Stream of blocks](./sob.md) — the SOB mechanism itself (ping-pong, lock handshake).
- [Extracting VCD Files](./vcd.md) / [Parsing VCD Files](./parsing.md) — the `VcdParser` basics and finding
  net names.
- [AXI4-Stream](./axistream.md) / [AXI4-MM](./aximm.md) — the sibling interface extractors.
- [Trace pitfalls](./trace_pitfalls.md) — the write-enable-vs-occupancy caution (and why it is fine here).
- [Fitting the timing model](../../examples/interleaver/timing_fit.md) — where this is used to measure a
  custom kernel's per-firing cost.
