---
title: Python Simulation
parent: Shared Memory (histogram)
nav_order: 3
has_children: false
---

# Python simulation

Python simulation is fast and easy to debug, so we run the histogram in SimPy
before ever invoking Vitis. This page shows the simulation harness — the host
controller that drives one transaction, the `MemoryMod` that stands in for
shared DRAM, and how the kernel-produced counts are checked against the numpy
golden. The harness is `run_sim()` in
[`examples/shared_mem/hist.py`](../../../examples/shared_mem/hist.py).

## What a SimPy run needs

Unlike the [regmap example](../regmap/pysim.md), where the host talks to the
kernel over a single AXI-Lite link, the histogram has **two** participants on
**three** interfaces: a host/controller that issues the command and stages the
data, the accelerator, and a shared memory both reach. The harness instantiates
all three and wires them together:

```python
# examples/shared_mem/hist.py — run_sim
def run_sim(data, bin_edges, nbins, *, clk_freq=1e9, tx_id=7, addr_misalign=0):
    sim = Simulation()
    clk = Clock(freq=clk_freq)
    mem   = MemoryMod(name="mem", sim=sim, inline=False, clk=clk)
    accel = HistAccel(name="hist_accel", sim=sim, clk=clk)
    ctrl  = HistController(name="hist_ctrl", sim=sim, mem=mem,
                           data=data, bin_edges=bin_edges, nbins=nbins, tx_id=tx_id,
                           addr_misalign=addr_misalign)
    connect(sim, ctrl, accel, mem, clk)
    sim.run_sim()

    expected = golden_counts(data, bin_edges, nbins)
    return HistResult(counts=ctrl.counts, expected=expected, status=ctrl.resp.status)
```

Three SimObjs share one `Simulation` and one `Clock`:

- **`MemoryMod`** is the shared memory — a SimPy model of a byte-addressed
  word memory behind an AXI-MM **slave** endpoint. It is where the `data`,
  `bin_edges`, and `counts` buffers live; both the controller (to stage inputs
  and read results) and the accelerator (to do the work) reach it over `m_axi`.
  `inline=False` makes it its own scheduled process rather than a passive object,
  so its accesses take simulated time on the clock.
- **`HistAccel`** is the [accelerator](python.md) under test.
- **`HistController`** is the host stand-in — the SimObj that plays the CPU
  driver.

## The controller — one transaction, end to end

`HistController.run_proc` is the host-side sequence the [concept page](aximm.md)
laid out: allocate the three regions, stage the inputs, **launch the kernel**, issue the command, await
the response, read the counts back.

Note the order: the controller raises `ap_start` **before** sending the command. The kernel's
[`on_start`](./python.md) then blocks on `s_in.get(...)` until the in-band command arrives. One master
does both — which is the point of the [control-only regmap](./python.md#why-a-control-only-regmap): the
same host that programs the memory base address also launches the kernel.

```python
# examples/shared_mem/hist.py — HistController.run_proc
# Allocate the three regions, in order (data, edges, counts).
data_nwords  = get_nwords(Float32, word_bw=self.mem.word_size, shape=ndata)
edge_nwords  = get_nwords(Float32, word_bw=self.mem.word_size, shape=max(nedges, 1))
count_nwords = get_nwords(Uint32Field, word_bw=self.mem.word_size, shape=nbins)
self.data_addr  = self.mem.alloc(data_nwords)
self.edge_addr  = self.mem.alloc(edge_nwords)
self.count_addr = self.mem.alloc(count_nwords)

# Populate the input buffers (TB-side memory access).
yield from self.mem.m_mm.write_array(self.data, Float32, self.data_addr, word_bw=bw)
if nedges > 0:
    yield from self.mem.m_mm.write_array(self.bin_edges, Float32, self.edge_addr, word_bw=bw)

# Launch the kernel (ap_start at 0x00 of the AXI-Lite slave).  on_start then
# blocks on s_in.get until the in-band command below arrives.
yield from self._regmap().bind_master(self.m_lite).start()

# Issue the command and await the response.
cmd = HistCmd(tx_id=self.tx_id,
              data_addr=self.data_addr + self.addr_misalign,
              bin_edges_addr=self.edge_addr, ndata=ndata, nbins=nbins,
              cnt_addr=self.count_addr)
yield from self.m_cmd.write(cmd)

resp_words = yield from self.s_resp.get()
self.resp = HistResp().deserialize(resp_words, word_bw=bw)

# Read the kernel-produced counts back.
out = yield from self.mem.m_mm.read_array(Uint32Field, nbins, self.count_addr, word_bw=bw)
self.counts = np.asarray(out, dtype=np.uint32)
```

The details that matter:

1. **Allocation order is the contract.** The controller allocs `data`, then
   `edges`, then `counts` — the same order the [testbench](codegen.md) will, so
   the three regions land at the same byte addresses on both sides. `MemoryMod`
   hands out non-overlapping regions; the controller stamps the returned addresses
   into the command.
2. **The edges alloc is clamped to `max(nedges, 1)`.** When `nbins == 1` there are
   zero edges, but a memory region still needs a valid base address — so the
   controller reserves one word it never writes. (The generated testbench does the
   same clamp; see [`CODEGEN_NOTES.md`](../../../examples/shared_mem/CODEGEN_NOTES.md).)
3. **`addr_misalign` is a test hook.** Adding a byte offset to `data_addr` lets a
   test drive the kernel's `ADDRESS_ERROR` path without any other change — the
   command now points one byte off a word boundary.

## Wiring it together

`connect()` builds the three links — two `StreamIF`s for the command/response
control and one `DirectMMIF` for the shared memory — and binds each port to its
peer:

```python
# examples/shared_mem/hist.py — connect
def connect(sim, ctrl, accel, mem, clk):
    in_stream  = StreamIF(sim=sim, clk=clk)
    out_stream = StreamIF(sim=sim, clk=clk)
    mem_link   = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)
    in_stream.bind( "master", ctrl.m_cmd);   in_stream.bind( "slave", accel.s_in)
    out_stream.bind("master", accel.m_out);  out_stream.bind("slave", ctrl.s_resp)
    mem_link.bind(  "master", accel.m_mem);  mem_link.bind(  "slave", mem.s_mm)
```

`DirectMMIF(byte_addressable=True)` is the in-process AXI-MM link: it carries the
accelerator's `read_array` / `write_array` bursts to the memory's slave endpoint,
converting the command's **byte** addresses to word indices exactly as real
hardware does. The controller reaches the same memory through its own `mem.m_mm`
master — so in the model, host and kernel genuinely share one memory, addressed
identically.

## Parity against the golden

The point of the SimPy run is to confirm the accelerator's logic before
synthesis. `run_sim` returns a `HistResult` carrying both the observed counts and
the numpy golden, and `passed` is the conjunction of *status is `NO_ERROR`* and
*counts match*:

```python
# examples/shared_mem/hist.py
@dataclass
class HistResult:
    counts: npt.NDArray[np.uint32]
    expected: npt.NDArray[np.uint32]
    status: HistError

    @property
    def passed(self) -> bool:
        return (self.status == HistError.NO_ERROR
                and np.array_equal(self.counts, self.expected))
```

The golden is `golden_counts` — `bin = #{edges <= sample}`, then count per bin —
the same routine the `compute` hook uses, so the model is checked against a
second, independent numpy implementation of the binning rule.

The parity tests in
[`tests/examples/test_hist_sim.py`](../../../tests/examples/test_hist_sim.py)
sweep several `(ndata, nbins)` vectors and the three failure modes:

```python
res = run_sim(ht.data, ht.bin_edges, nbins=nbins, tx_id=seed)
assert res.status == HistError.NO_ERROR
np.testing.assert_array_equal(res.counts, ht.counts)   # vs the HistogramAccel golden
```

Plus one test each for `INVALID_NDATA` (`ndata` out of range), `INVALID_NBINS`
(`nbins` out of range), and `ADDRESS_ERROR` (driven by `addr_misalign=1`) — so the
validation→status path is exercised in simulation, not just asserted in prose.

## Timing in the SimPy run

The SimPy simulation is **discrete-event on the clock**: every stream transfer and
every memory burst advances simulated time, so a run is not just functional — it
produces a transaction *timeline* (command in → reads → compute → write → response
out). That timeline is a first, cheap estimate of how long a transaction takes.

The simulation is cycle-*approximate*, though, not cycle-exact: the real burst
latencies, the `m_axi` handshake overhead, and the pipeline fill only show up once
the design is synthesized to RTL. The next page hands the generated kernel to
Vitis to get those measured numbers, and the [timing page](timing.md) renders the
RTL waveform and the multi-buffer burst layout that the SimPy model is
approximating.

## Next

- [Code generation](codegen.md) — lowering `HistAccel` (and the testbench) to
  Vitis HLS C++.
- [C and RTL simulation](rtlsim.md) — running the Vitis flows against the
  generated artifacts.
