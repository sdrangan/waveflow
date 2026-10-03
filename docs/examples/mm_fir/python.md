---
title: Python model
parent: A memory-mapped FIR
nav_order: 1
has_children: false
summary: "The Python side of mm_fir: two DataList messages (the config and the status) that also generate the C++ structs, an exact-integer golden, the kernel as a FreeRunMod whose run_iter is one config, one packet of samples, or one idle cycle, and the host program -- a writer that commits configs, waits for them to be received and sends the samples, and a reader that takes the results."
---

# Python model

All of it is in [`examples/mm_fir/mm_fir.py`](../../../examples/mm_fir/mm_fir.py).

## The two messages

The kernel exchanges exactly two structured messages with the register bank, and both are
`DataList`s — so the same declaration is what pysim serializes and what generates the C++ structs the
HLS body reads ([Code generation](codegen.md)). Nothing on either side unpacks a word by hand.

```python
S16 = IntField.specialize(bitwidth=16, signed=True)
U32 = IntField.specialize(bitwidth=32, signed=False)
Taps = DataArray.specialize(S16, max_shape=(NTAP_MAX,))


class FirCfg(DataList):
    """One configuration: ``ntaps`` taps from ``coeffs``, in force from sample ``apply_at`` on."""

    elements = {
        "ntaps": {"schema": U32, "description": "active taps (<= NTAP_MAX)"},
        "apply_at": {"schema": U32, "description": "index of the first sample filtered with these taps"},
        "coeffs": {"schema": Taps, "description": "tap k multiplies x[n-k]"},
    }


class FirStatus(DataList):
    """What the kernel publishes after every event (latest value wins)."""

    elements = {
        "nsamp": {"schema": U32, "description": "samples filtered so far"},
        "ncfg": {"schema": U32, "description": "configs received so far"},
        "late": {"schema": U32, "description": "configs that arrived after their apply_at sample"},
    }
```

At the 64-bit bus width a `FirCfg` is 5 words (`FirCfg.nwords_per_inst(64)`) and a `FirStatus` 2.
Those two numbers are the register bank's `NCFG` / `NSTAT`: the shadow holds 5 words, and a commit
sends a 5-word packet.

## The golden

```python
def fir_golden(x, cfgs) -> np.ndarray:
    """Exact FIR over the whole stream: sample *n* uses the latest config with ``apply_at <= n``."""
    x = np.asarray(x, dtype=np.int64)
    y = np.zeros(len(x), dtype=np.int64)
    order = sorted(cfgs, key=lambda c: c[0])        # stable: of two configs at one sample, the later wins
    for i, (at, taps) in enumerate(order):
        end = min(order[i + 1][0] if i + 1 < len(order) else len(x), len(x))
        taps = np.asarray(taps, dtype=np.int64)
        if end > at and len(taps):
            y[at:end] = np.convolve(x, taps)[at:end]
    return y
```

Each config's taps are applied to the whole input with one `np.convolve`, and the config keeps the
samples it is in force for. The loop is over configs — a handful — not samples. `np.convolve` on
`int64` is integer arithmetic, so the result is exact with nothing to round: int16 samples times int16
taps, summed over at most 16 taps, need 37 bits.

Two properties are worth stating because the kernel must match them: the filter **history is
continuous across a switch** (only the taps change — the samples already seen stay in the delay
line), and samples before the first config see all-zero taps. `cfgs` is the plan,
`[(apply_at, taps), ...]`.

## The kernel

`MmFir` is an ordinary `FreeRunMod` leaf with four stream endpoints and no memory-mapped anything:

| endpoint | direction | carries |
|---|---|---|
| `s_cfg` | in | one `FirCfg` per commit, from the register bank |
| `s_in` | in | samples, one per word, from queue in |
| `m_out` | out | results, one per sample, to queue out |
| `m_status` | out | `FirStatus` messages, to the register bank |

One firing of `run_iter` is exactly one of three things — a config, a packet of samples, or an idle
cycle — and the config is checked **first**. A packet is filtered by `_filter` and timed as the HLS body
runs:

```python
    def run_iter(self):
        cfg = yield from self.s_cfg.get_schema_nb(FirCfg)
        if cfg is not None:
            self.ncfg += 1
            taps = np.asarray(cfg.coeffs, dtype=np.int64)[:int(cfg.ntaps)]
            at = int(cfg.apply_at)
            if at < self.nsamp:
                self.late += 1
                at = self.nsamp                     # too late for its sample: in force from now
            if self.pending is not None:            # superseded: in force now, as the RTL does
                self.taps = self.pending[1]
            self.pending = (at, taps)
            yield from self._publish()
            return
        if self.s_in.data_buffer.items:
            pkt = yield from self.s_in.get()
            n = len(pkt)
            # The packet's first word arrived (n - 1) cycles before its last, at II=1.
            tstart = self.env.now - (n - 1) * self.clk.period
            y = self._filter(samples_of(pkt))
            # Timing, as the HLS body: the first result leaves proc_latency cycles after the first
            # sample arrived, and one result follows every proc_ii cycles.
            t_out_start = tstart + self.proc_latency * self.clk.period
            proc_time = max(0.0, n * self.proc_ii * self.clk.period + (t_out_start - self.env.now))
            yield self.timeout(proc_time)
            yield from self.m_out.write_pipelined(y.view(np.uint64), t_out_start)
            yield from self._publish()
            return
        yield self.timeout(self.clk.period)         # idle: poll again next cycle
```

`_filter` is the golden itself, run over the filter's history and the packet:

```python
    def _filter(self, x: np.ndarray) -> np.ndarray:
        h, n = len(self.hist), len(x)
        plan = [(0, self.taps)]
        if self.pending is not None:
            k = max(self.pending[0] - self.nsamp, 0)   # the sample in this packet it applies at
            if k < n:
                plan.append((h + k, self.pending[1]))
                self.taps, self.pending = self.pending[1], None
        y = fir_golden(np.concatenate([self.hist, x]), plan)[h:]
        self.hist = np.concatenate([self.hist, x])[n:]
        self.nsamp += n
        return y
```

A pending config's `apply_at` lands on at most one sample of a packet, so a packet is at most two
segments — the taps in force, then the new ones — and `fir_golden` filters each with one convolution.
`hist` (the last 15 samples) is what keeps the delay line continuous from one packet to the next.

What to read in it:

- **Config first, and non-blocking.** `get_schema_nb` returns `None` when nothing is waiting. Because
  the config is checked even when no samples are arriving, a host that is *waiting for the config to
  be received* gets its answer — a kernel that blocked on `s_in` would deadlock that host.
- **One pending slot.** A received config is parked as `(apply_at, taps)` and put in force at the
  first sample whose index reaches `apply_at`. A second config arriving while one is pending first
  puts the pending one in force. The HLS body has exactly one slot too, which is why the Python does.
- **`late`.** A config whose `apply_at` has already passed is in force from the next sample and
  counted. The status makes the miss visible; nothing pretends it was on time.
- **The timing is the HLS body's.** `proc_ii = 1` and `proc_latency = 10` are the csynth report's
  interval and latency: the first result leaves 10 cycles after the first sample arrived, one per
  cycle after that — the same pattern as `PolyAccel` in
  [stream_inband](../../../examples/stream_inband/poly.py). `write_pipelined` anchors the output at
  `t_out_start`, so a packet's input and output overlap rather than adding.
- **The idle branch** waits one clock and returns. It is the pysim model of an HLS loop that checks
  both streams with `read_nb` every cycle — and because such a loop never runs out of events, the
  testbench ends the simulation explicitly (`Simulation.run_sim(until=...)`).

## The host program

`FirHost` is the protocol from the [index](index.md#the-one-subtle-part-switching-taps-mid-stream),
written as stream code. It holds four endpoints and never an address:

| endpoint | type | one call |
|---|---|---|
| `cfg` | `StreamIFMaster` | `write(cfg)` sends one config to the kernel |
| `qin` | `StreamIFMaster` | `write(samples)` sends one packet |
| `qout` | `StreamIFSlave`, unframed | `get(nwords_max=n)` takes *n* outputs |
| `status` | `LatestValueIFSlave` | `read()` returns the latest status |

The system gives it those endpoints, either through the adaptor or joined straight to the kernel
([Python simulation](pysim.md) shows both), so the same class runs over the bus and without it.

It runs as two processes. The **writer** sends the configs and the sample packets in the order
`host_schedule` lays out:

```python
    def _writer(self):
        for item in self.schedule:
            if item[0] == "cfg":
                _, i, apply_at, taps = item
                yield from self.cfg.write(make_cfg(taps, apply_at))
                while int((yield from self._read_status()).ncfg) < i + 1:
                    yield self.timeout(self.poll_cycles * self.clk.period)
            else:
                _, n0, n1 = item
                chunk = np.asarray([int(v) & 0xFFFF for v in self.x[n0:n1]], dtype=np.uint64)
                yield from self.qin.write(chunk)
```

After each config it reads the status until `ncfg` shows the config *received*; only then does it
send the sample the config applies at. A packet never straddles a commit point, so the packet
containing `apply_at` is cut there.

The **reader** takes one output packet per input packet, the same size. Queue out is unframed — the
bus cannot see where the kernel's packets end — so the reader names the size, and it knows the size
because it reads the same schedule:

```python
    def _reader(self):
        for item in self.schedule:
            if item[0] == "pkt":
                words = yield from self.qout.get(nwords_max=item[2] - item[1])
                self.y += [int(np.int64(np.uint64(w))) for w in np.asarray(words)]
```

Two processes are what make this simple. With one, the host has to empty queue out before every push:
queue out is 64 deep, a full output queue stops the kernel, and a kernel that has stopped takes no
input, so a host waiting for room in queue in would wait forever. The earlier single-process host did
exactly that bookkeeping, with the addresses written out.

Every call blocks, and over the bus it blocks by **polling** — reading the free space or the count
and asking again — never by stalling the bus. That matters here: the writer waiting for room and the
reader popping outputs share one bus master, and one adaptor front. A write stalled on a full queue
would hold the front, the reader's pops could not get through, and the host would deadlock.

`lag` is the negative-control knob. With `lag > 0` the host commits each config after the first
`lag` samples *after* its `apply_at` — deliberately late — and the kernel must report it.

Next: [Python simulation](pysim.md).
