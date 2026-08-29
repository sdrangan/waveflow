---
title: Reading the trace
parent: A memory reached three ways
nav_order: 5
has_children: false
---

# Reading the trace

[RTL simulation](rtlsim.md) produced `bram_access_top_trace.vcd`. This page reads it: what the design
did, whether it did anything it was not allowed to, how long the memory really takes to answer, and
how all of that compares with the Python model.

Unlike every other timing page in this tree, the lanes here are the **memory's own pins**. `buf_w`
and `buf_r` are wires of the *wrapper* — the join between the kernel's `mode=bram` ports and
`bram_t2p` — so a level-1 `$dumpvars` sees them, and the picture shows the memory being used rather
than inferring it from the streams at the boundary.

## One run, four lanes

![The whole run: the write port fills first, and nothing reads until the token arrives](./images/activity_full.svg)

Reading it left to right: the payload arrives, the write port is busy for 256 cycles, and **both read
lanes are empty the entire time**. That emptiness is the arming token doing its job — the reader is
blocked on `go` until the writer's first command completes. Then the answers start, and from roughly
cycle 320 the write and read lanes are busy *together*.

The event counts say the same thing more precisely, and one of them is a surprise:

```python
from pathlib import Path
from examples.bram_access.bram_access_build import hazard_manifest
from examples.bram_access.bram_access_figures import lanes

ll = lanes(Path("examples/bram_access/xsi/bram_access_top_trace.vcd"), hazard_manifest())
for label, ev, _colour in ll:
    print(f"{label:26s} {len(ev):4d} beats   first {ev[0]:3d}  last {ev[-1]:3d}")
```

```
data_w  (payload in)        356 beats   first  17  last 405
buf_w   (memory write)      388 beats   first  24  last 480
buf_r   (memory read)       242 beats   first 287  last 582
data_r  (payload out)       233 beats   first 289  last 583
```

**356 payload words in, 388 memory writes — and the memory is written *more* than it is fed.** The
32 extra writes are the `COMPUTE`: it takes no payload at all and rewrites 32 words it read from the
memory itself. That asymmetry is the transaction's whole signature, and it is legible here before
any timing argument is made.

**242 read enables for 233 returned words.** Nine of the ten read commands return data, and each
presents one address past the end of its range as the pipeline drains. That extra enable is why the
read-latency measurement below skips addresses whose contents are unknown: word 256 was never
written, and at RTL a never-written word is `X`.

### The cycle numbers on this page are the waveform's

The figures and the scan count **rising edges from the start of the dump**, which includes the 16
reset cycles the harness holds before the run loop begins. The sinks' `cycles.bin` counts from the
first post-reset cycle instead. The two differ by a constant, and it is worth knowing which is which
before comparing a figure against a gate:

```python
import numpy as np
from pathlib import Path
from examples.bram_access.bram_access_build import hazard_manifest
from examples.bram_access.bram_access_figures import lanes

xsi = Path("examples/bram_access/xsi")
vcd_beats = [ev for label, ev, _c in
             lanes(xsi / "bram_access_top_trace.vcd", hazard_manifest())
             if label.startswith("data_r")][0]
sink = np.fromfile(xsi / "vectors" / "data_r" / "cycles.bin", dtype="<u8")
print("words:", len(vcd_beats), len(sink),
      "| offset:", sorted({int(d) for d in vcd_beats - sink}))
```

```
words: 233 233 | offset: [15]
```

Exactly 15 on every one of the 233 words. The gate's `568` and the figure's `583` are the same
instant.

## The overlap, beat by beat

![The overlap window: both memory ports busy in the same cycles, on disjoint ranges](./images/activity_overlap.svg)

The zoom is picked from the run rather than written down — the window is derived as the cycles in
which the read port is active inside the write port's span — so it cannot drift away from the thing
it is meant to show.

Three things are legible here that the whole-run view flattens:

- Each single-word read is a *pair* of `buf_r` hairlines: the address it was asked for, and the one
  the pipeline presents behind it.
- `buf_w` and `buf_r` fire in the **same cycles**, continuously, through the phase-2 window. This is
  a true-dual-port memory doing the thing it exists for.
- Later in the run there is a stretch where `buf_w` beats are only half as dense. That is the
  `COMPUTE`, and it is measured below.

### Overlap is legal here, and that is the interesting part

Scenario zero runs the write side through three phases:

- **Phase 1 — no overlap.** The witness. Load 256 words, then read. Nothing else is live.
- **Phase 2 — deliberate overlap.** `write(64, 64)` runs *while* `read(0, 64)` is outstanding.
  Disjoint ranges, so it is legal.
- **Phase 3 — in place.** `write(512, 32)` seeds a region and `compute(512, 32)` rewrites it, with
  the read of that region deliberately last on the reader's stream.

Phase 2 is where "no hazard" stops being structural and becomes **conventional**. Compare
[`RfShotBuf`](../../guide/rf/), whose entire safety argument is that the reader and the writer are
*never* live at the same time; this design permits the overlap and hands the caller the obligation.
[Sequencing belongs in the design](../../guide/interface/bram.md#sequencing-belongs-in-the-design)
is where the one piece of ordering it *does* own — the arming token — is argued.

That overlap is asserted rather than admired, and in **cycles** — because the two ranges are
disjoint, so the returned words are identical whether the two overlapped or ran one after the other:

```python
import numpy as np
from pathlib import Path
from examples.bram_access.bram_access import WriteResp, resp_words, scenario_zero

sc = scenario_zero()
xsi = Path("examples/bram_access/xsi")
data = np.fromfile(xsi / "vectors" / "data_r" / "cycles.bin", dtype="<u8")
resp_w = np.fromfile(xsi / "vectors" / "resp_w" / "cycles.bin", dtype="<u8")
lo, hi = sc.overlap_read
when = int(resp_w[resp_words(WriteResp, sc.overlap_write_resp + 1) - 1])
print(f"read window [{int(data[lo])}, {int(data[hi - 1])}], phase-2 write done at {when}")
print("overlapped:", int(data[lo]) <= when <= int(data[hi - 1]))
```

```
read window [320, 383], phase-2 write done at 353
overlapped: True
```

A sink timestamps every **word**, and a response is two of them — so the index goes through
`resp_words`, which asks the schema, rather than through a `2`. The response grew from one word to
two the day it gained a `tid`; a literal would not have noticed, and the index would have quietly
pointed into the middle of a message.

## What it costs to read a word you are about to write

This is the measurement the example exists for, and it is a **controlled experiment**: the same task
writes 32 words at 512, and then rewrites *those same 32 words* in place, adjacent in time, through
the same port. Everything except the access shape is held constant.

```python
import numpy as np
from pathlib import Path
from examples.bram_access.bram_access_build import hazard_manifest
from waveflow.utils.bram_trace import port_samples

w = port_samples(Path("examples/bram_access/xsi/bram_access_top_trace.vcd"),
                 hazard_manifest(), "write")
we = np.nonzero(np.asarray(w.we) != 0)[0]
addr = np.asarray(w.addr)

groups, cur = [], [we[0]]                       # a gap longer than the II ends a command
for c in we[1:]:
    if c - cur[-1] <= 6:
        cur.append(c)
    else:
        groups.append(cur); cur = [c]
groups.append(cur)

for g in groups:
    n, span = len(g), g[-1] - g[0] + 1
    print(f"addr {int(addr[g[0]]):4d}..{int(addr[g[-1]]):4d}  {n:3d} writes over {span:3d} "
          f"cycles ({g[0]}..{g[-1]})  ->  {span / n:.2f} cycles/element")
```

```
addr    0.. 255  256 writes over 256 cycles (24..279)  ->  1.00 cycles/element
addr 1020..1023    4 writes over   4 cycles (289..292)  ->  1.00 cycles/element
addr   64.. 127   64 writes over  64 cycles (302..365)  ->  1.00 cycles/element
addr  512.. 543   32 writes over  32 cycles (375..406)  ->  1.00 cycles/element
addr  512.. 543   32 writes over  63 cycles (418..480)  ->  1.97 cycles/element
```

The last two rows are the experiment. **32 words written in 32 cycles; the same 32 recomputed in
place in 63.** csynth says the same thing in its own units — the achieved `PipelineII` is 1 for
`write_payload` and 2 for `compute_inplace` — and the two agreeing is worth more than either alone,
because the report is a schedule and the waveform is a run.

**The number is a consequence, not a property of in-place work.** "In place is II=2" is false in
general. What is true is a chain, and every link of it is checkable:

1. The wrapper wires **one physical memory port** per declared `bram` port.
2. So `buf_w`, being `access="readwrite"`, carries `storage_type=ram_1p` on its pragma, which
   declares no second port pair at all.
3. So the scheduler has one port to spend, and a read-modify-write needs two accesses per element.
4. So the loop runs at II=2, and `ii_for(2)` in the Python model says 2 for the same reason.

Under `ram_1wnr` the same loop reaches **II=1** — by reading on port B while writing on port A. That
is faster and it is *wrong*: the wrapper wires only the A halves, so those reads reach a dangling
port, and nothing says so until RTL. The
[`access` / `storage_type` derivation](../../guide/interface/bram.md#accessreadwrite-and-the-storage_type-that-follows)
is what makes the safe choice the one you get.

## The hazard that cannot be heard {#the-hazard-that-cannot-be-heard}

The design *permits* the overlap, so keeping the ranges disjoint is the caller's job. `bram_t2p.v`
carries the guard for a caller who gets it wrong — and **in this flow nothing can hear it**.

That is measured, not suspected. In the XSI shared-library flow (Vivado 2025.1, `xelab -dll` plus the
C++ loader), RTL text output is discarded: `$display` from an `always` block reaches neither stdout
nor a file, an `initial $display` at time zero does not either, and a non-null
`s_xsi_setup_info::logFileName` produces no log — although it *does* change the kernel's invocation
from `-nolog` to `-log <name>`, visible in `xsim.dir/<top>/xsimkernel.log`. Only an `$fwrite` to a
file the Verilog opens itself works, which is how the `$error`'s firings were counted in the first
place.

The cost was concrete: **five shipped XSI gates** asserted that the string
`"read-during-write collision"` was absent from a run's output — a string that could never appear —
and each read as positive evidence. All five have been removed.

So the **condition** is checked instead, from the waveform, using nets named by the emitter that made
them rather than matched by substring:

```python
from pathlib import Path
from examples.bram_access.bram_access_build import hazard_manifest
from waveflow.utils.bram_trace import describe, find_read_during_write

vcd = Path("examples/bram_access/xsi/bram_access_top_trace.vcd")
print(describe(find_read_during_write(vcd, hazard_manifest())))
```

```
no read-during-write collisions
```

### An empty scan is not a passing gate

No collisions is what a correct design looks like. It is *also* what a renamed net, a dump that never
ran, and a scan bound to the wrong scope look like. So the gate is a **pair**: scenario zero must come
back clean, and a scenario built to collide must come back dirty. `collision_scenario()` is the dirty
half, and on the run recorded here it produces **24** collisions on words 128–135.

Building that scenario turned out to be the interesting part, because **address overlap alone is not
a collision**. The memory's condition is `a_addr == b_addr` *in the same cycle*, and both tasks sweep
their range at one word per cycle — so two commands over the identical range are parallel lines in
(cycle, address) and never meet unless they happen to start in the same cycle. What makes them meet
is a relative phase that **moves**, so the scenario gives the writer and the reader command lengths
that differ by one word:

```python
from examples.bram_access.bram_access import collision_scenario

sc = collision_scenario()
w = [(int(c.waddr), int(c.nsamp)) for c in sc.cmd_w[1:]]
r = [(int(c.raddr), int(c.nsamp)) for c in sc.cmd_r]
print("writes:", w[0], "x", len(w), " reads:", r[0], "x", len(r))
print("ranges overlap:", max(w[0][0], r[0][0]) < min(w[0][0] + w[0][1], r[0][0] + r[0][1]))
print("lengths differ:", w[0][1] != r[0][1])
```

```
writes: (128, 8) x 48  reads: (128, 9) x 48
ranges overlap: True
lengths differ: True
```

Each round shifts the two by one cycle relative to each other, and within a few dozen rounds every
offset in the window has been visited. Both halves are gated in
`tests/examples/test_bram_access_xsi.py`.

*The durable fix is neither a print nor a trace scan but a sticky `collision` output on the memory,
carried through the wrapper and readable in both backends by construction. That is a `BramIF`
interface change and is not done.*

## The read latency, measured off the memory's own pins

`bram_t2p.v` publishes `localparam READ_LATENCY = 1`, and Waveflow emits both the kernel's
`latency=1` pragma and the pysim model's delay from that one line. But that is two files agreeing,
not evidence about the hardware. The waveform can be asked directly: **at what distance from the
address does the answer appear?**

```python
from pathlib import Path
from examples.bram_access.bram_access import BASE, DEPTH, SENTINEL_BASE
from examples.bram_access.bram_access_build import hazard_manifest
from waveflow.utils.bram_trace import measured_read_latency, port_samples

def expected(addr):
    if 64 <= addr < 128:
        return None                  # phase 2 rewrote these mid-run
    if addr < 256:
        return BASE + addr           # the witness's ramp
    if DEPTH - 4 <= addr < DEPTH:
        return SENTINEL_BASE + (addr - (DEPTH - 4))
    return None                      # computed in place, or never written (X at RTL)

port = port_samples(Path("examples/bram_access/xsi/bram_access_top_trace.vcd"),
                    hazard_manifest(), "read")
print("offsets that explain every read:", sorted(measured_read_latency(port, expected)))
```

```
offsets that explain every read: [1]
```

A **set**, on purpose. One element is the number; more than one means the scenario cannot tell those
offsets apart; none means the answer never appears where the data says it should — a real defect
rather than an off-by-one in the measurement. And a single element is only *decidable* because the
payload is a ramp: with a constant payload every offset would fit, which is the same failure the ramp
prevents in the value check.

## And the pysim does **not** match — for free

A reader arriving from [`mem_copy`](../memcpy/timing.md) will expect a section called *"And the pysim
matches — for free."* That page can say it; this one cannot, and the difference is the whole of
objective 4.

**The throughputs match for free.** Both backends deliver the 64-word read at one word per cycle.
Nothing in the Python model was tuned to make that true — a `StreamIF` with a clock and an RTL loop at
II=1 simply agree.

**The first word does not.** It is off by exactly `READ_LATENCY` unless the model pays it:

```python
import numpy as np
from pathlib import Path
from examples.bram_access.bram_access import run_pysim, scenario_zero

sc = scenario_zero()
lo, hi = sc.cadence_read
rtl = np.fromfile(Path("examples/bram_access/xsi/vectors/data_r/cycles.bin"), dtype="<u8")
tb = run_pysim(sc=sc)

def gaps(c):
    return sorted(set(np.diff(np.asarray(c)[lo:hi]).tolist()))

port, n = tb.dut.rd.buf_r, hi - lo
env, t0 = port.env, port.env.now                      # the run is over; drive one more read
proc = env.process(port.read_pipelined(port.element_type, n, 0))
env.run(until=proc)
cost = round((env.now - t0) * float(port.interface.clk.freq))

print("cycles per word   RTL", gaps(rtl), " pysim", gaps(tb.data_r_snk.cycles))
print(f"a {n}-element read costs {cost} cycles = READ_LATENCY {int(port.read_latency)} + {n}")
```

```
cycles per word   RTL [1]  pysim [1]
a 64-element read costs 65 cycles = READ_LATENCY 1 + 64
```

The fill is measured as the **content of the model**, not as absolute agreement between the backends:
pysim is a discrete-event model of the streams around the memory, not a cycle-accurate model of the
kernel, so its absolute cycle numbers are its own. What has to be exact is the *term*.

This used to be a subtraction — run the design twice with a `model_read_latency` flag on and off, and
check the difference was `READ_LATENCY`. The flag existed only because the fill was hand-written in
the design body, `yield self.timeout(self.buf_r.read_latency / freq)`, with nowhere else to put it.
It is [`BramIFMaster.read_pipelined`](../../guide/interface/bram.md)'s term now, so there is no "off"
configuration to subtract from and the number is read where it lives.

**Why a memory is different from a bus.** `mem_copy`'s free match comes from calibrated models of an
`m_axi` bus and the mem-stream adaptors — components whose timing is a `(component, platform)`
property that ships with Waveflow. A BRAM has no such model and does not want one: its access is
deterministic, unarbitrated and one cycle, so a discrete-event model of it would add a SimPy timestep
and no fidelity. What that buys is a model with nothing to calibrate; what it costs is exactly one
cycle of pipeline fill, paid explicitly, once per command.

And it is a **fill**, not a per-word cost — which a first-word check alone would not catch. A model
paying the latency per *word* would match RTL on the first word and be 64 cycles late by the end of a
64-word read:

```python
import numpy as np
from examples.bram_access.bram_access import run_pysim, scenario_zero

sc = scenario_zero()
lo, hi = sc.cadence_read
c = np.asarray(run_pysim(sc=sc).data_r_snk.cycles)
print("word-to-word gaps through the 64-word read:", sorted(set(np.diff(c[lo:hi]).tolist())))
```

```
word-to-word gaps through the 64-word read: [1]
```

## How the figures are committed and refreshed

Both SVGs on this page are **committed assets**, not rendered at build time. The workflow is the one
`mem_copy` and `shared_mem` use:

```bash
cd examples/bram_access
python bram_access_build.py --through rtl_trace          # the waveform
python bram_access_build.py --through activity_figures   # -> results/ (gitignored)
python bram_access_build.py --through sync_docs_figures  # -> docs/.../images/ (committed)
```

`SyncDocsFiguresStep` copies each figure named in an explicit manifest, so a docs figure only
changes when someone runs that step — and the change arrives as a reviewable `git diff` of the SVG
rather than as a silently different picture.

It also writes `images/sync_status.json` with each figure's source path and content hash. **That file
is not tracked** — a blanket `*.json` rule ignores it, here and for every other example that writes
one — so it is a *local* staleness signal you can check by hand, not a reviewable record. The
committed artifact is the SVG.

The SVGs are **deterministic**: `matplotlib.rcParams["svg.hashsalt"]` is fixed and the date metadata
is suppressed, so re-rendering an unchanged figure produces a byte-identical file. Without that, every
refresh would diff.

One caveat worth knowing: `rtl_trace` is *not* re-run just because the waveform on disk changed under
it. If you have run the `-m xsi` gate since — which leaves the **collision** vectors and the collision
waveform behind — force the step, or the figures will be of the wrong run:

```bash
python bram_access_build.py --through sync_docs_figures --force-step rtl_trace
```

## See also

- [Python simulation](pysim.md) — the same measurements from the Python side.
- [BRAM — memory between modules](../../guide/interface/bram.md) — the interface, and the addressing
  convention the wrapper reconciles.
- [`mem_copy`'s timing page](../memcpy/timing.md) — the free match, and the calibration behind it.
- [Timing models](../../guide/timing_model/) — what a forward model is, and when a component needs
  one.
