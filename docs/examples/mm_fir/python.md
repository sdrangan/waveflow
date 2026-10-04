---
title: Python model
parent: A memory-mapped FIR
nav_order: 1
has_children: false
summary: "The Python side of mm_fir: four DataList messages (the packet header, the response, the config and the status) that also generate the C++ structs, typed int16 samples and int64 results packed by the serializer, a vectorized exact-integer golden, the kernel as a FreeRunMod whose run_iter is one packet -- header, the config it names, samples, response -- and the host program: a writer that commits configs and sends tagged packets, and a reader that takes the results and checks each response."
---

# Python model

All of it is in [`examples/mm_fir/mm_fir.py`](../../../examples/mm_fir/mm_fir.py).

## The messages

Everything that crosses a stream is a declared type, so the same declaration is what pysim
serializes and what generates the C++ the HLS body uses ([Code generation](codegen.md)). Nothing on
either side packs or unpacks a word by hand.

```python
S16 = IntField.specialize(bitwidth=16, signed=True, include_dir="include")
S64 = IntField.specialize(bitwidth=64, signed=True, include_dir="include")
U16 = IntField.specialize(bitwidth=16, signed=False)
U32 = IntField.specialize(bitwidth=32, signed=False)
Taps = DataArray.specialize(S16, max_shape=(NTAP_MAX,))


class FirCmdHdr(DataList):
    """The in-band header in front of every sample packet on ``s_in`` -- one 64-bit word."""
    elements = {
        "nsamp": {"schema": U32, "description": "samples in this packet"},
        "tx_id": {"schema": U16, "description": "the host's packet id, echoed in the response"},
        "cfg_seq": {"schema": U16,
                    "description": "the config this packet needs: config k is the k-th COMMIT"},
    }


class FirRespHdr(DataList):
    """The kernel's response to one packet, on the response FIFO -- one 64-bit word."""
    elements = {
        "nsamp": {"schema": U32, "description": "samples filtered in this packet"},
        "tx_id": {"schema": U16, "description": "echo of the packet's tx_id"},
        "cfg_seq": {"schema": U16, "description": "the config the packet was filtered with"},
    }


class FirCfg(DataList):
    """One configuration: ``ntaps`` taps from ``coeffs``."""
    elements = {
        "coeffs": {"schema": Taps, "description": "tap k multiplies x[n-k]"},
        "ntaps": {"schema": U32, "description": "active taps (<= NTAP_MAX)"},
    }


class FirStatus(DataList):
    """What the kernel publishes after every packet (latest value wins)."""
    elements = {
        "nsamp": {"schema": U32, "description": "samples filtered so far"},
        "ncfg": {"schema": U32, "description": "configs taken so far"},
    }
```

| message | stream | words at 64 bits |
|---|---|---|
| `FirCmdHdr` | `s_in`, in front of each packet's samples | 1 |
| samples | `s_in`, an array of `S16` | `ceil(nsamp / 4)` — the serializer packs four int16 to a word |
| results | `m_out`, an array of `S64` | `nsamp` — one int64 per word |
| `FirRespHdr` | `m_resp`, the response FIFO | 1 |
| `FirCfg` | `s_cfg`, from the register bank | 5 — the bank's `NCFG` |
| `FirStatus` | `m_status`, to the register bank | 1 — the bank's `NSTAT` |

**Why `coeffs` comes first in `FirCfg`.** At 64 bits, Python packs a `DataList` densely, while the
generated C++ starts an array on a fresh word — a known disagreement
(`plans/stream_array_alignment.md`, not fixed yet). With `ntaps` first, the RTL read the taps 32 bits
late; the RTL gate caught it on its first run. Sixteen int16 taps are exactly four 64-bit words, so
with the array first both layouts agree.

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

The filter **history is continuous across a switch** (only the taps change — the samples already seen
stay in the delay line), and samples before the first config see all-zero taps. `cfgs` is the plan,
`[(apply_at, taps), ...]`: the sample each config is meant to start at.

## The kernel

`MmFir` is an ordinary `FreeRunMod` leaf with five stream endpoints and no memory-mapped anything —
except a declaration, on the class, of which of those endpoints a bus master reaches and as what
(its address layout; see [the slave guide](../../guide/interface/axi_mm/slave.md#building-an-adaptor)):

```python
    mm_views: ClassVar[tuple] = (
        RegBank("regs", cfg_port="s_cfg", status_port="m_status",
                cfg_type=FirCfg, status_type=FirStatus),
        QueueIn("qin", port="s_in", depth=QDEPTH),
        QueueOut("qout", port="m_out", depth=QDEPTH),
        QueueOut("qresp", port="m_resp", depth=RDEPTH),
    )
```

The view addresses the rest of the example uses are this layout placed at `MM_BASE`:

```python
MM_LAYOUT = MemSlaveLayout.of(MmFir, mem_dwidth=DW)
REGS, QIN, QOUT, QRESP = (MM_BASE + MM_LAYOUT[n].base for n in ("regs", "qin", "qout", "qresp"))
```

The endpoints:

| endpoint | direction | carries |
|---|---|---|
| `s_cfg` | in | `FirCfg` messages, one per commit, from the register bank |
| `s_in` | in | per packet: a `FirCmdHdr`, then its samples, from queue in |
| `m_out` | out | results, one per sample, to queue out |
| `m_resp` | out | one `FirRespHdr` per packet, to the response FIFO |
| `m_status` | out | `FirStatus` messages, to the register bank |

One firing of `run_iter` is one packet — the in-band header pattern of
[stream_inband](../../../examples/stream_inband/poly.py):

```python
    def run_iter(self):
        hdr = yield from self.s_in.get_schema(FirCmdHdr)
        # The order the two streams cannot give, carried in the header: wait for this packet's
        # config.  A config committed for a LATER packet stays in s_cfg until one asks for it.
        while self.ncfg < int(hdr.cfg_seq):
            cfg = yield from self.s_cfg.get_schema(FirCfg)
            self.taps = np.asarray(cfg.coeffs, dtype=np.int64)[:int(cfg.ntaps)]
            self.ncfg += 1
        n = int(hdr.nsamp)
        if n:
            x, tstart = yield from self.s_in.get_pipelined(S16, n)      # the serializer unpacks
            y = self._filter(np.asarray(x.val, dtype=np.int64))
            # Timing, as the HLS body: the first result leaves proc_latency cycles after the first
            # sample arrived, and one result follows every proc_ii cycles.
            t_out_start = tstart + self.proc_latency * self.clk.period
            proc_time = max(0.0, n * self.proc_ii * self.clk.period + (t_out_start - self.env.now))
            yield self.timeout(proc_time)
            yield from self.m_out.write_pipelined(array(S64, y), t_out_start)
        # The status first, then the response -- so a host holding a packet's response knows the
        # status already counts it, and reads the final status once instead of waiting for it.
        yield from self._publish()
        # The response: which packet, and which config it was ACTUALLY filtered with.
        yield from self.m_resp.write(FirRespHdr(nsamp=n, tx_id=int(hdr.tx_id), cfg_seq=self.ncfg))
```

`_filter` is the golden itself, run over the filter's history and the packet:

```python
    def _filter(self, x: np.ndarray) -> np.ndarray:
        h, n = len(self.hist), len(x)
        xs = np.concatenate([self.hist, x])
        y = fir_golden(xs, [(0, self.taps)])[h:]
        self.hist = xs[n:]
        self.nsamp += n
        return y
```

What to read in it:

- **The config wait.** The header's `cfg_seq` names the config the packet needs; the kernel takes
  configs until it has that many, waiting if the config has not arrived. A packet can use neither an
  older config nor one nobody has asked for yet. The [index](index.md#the-one-subtle-part-switching-taps-mid-stream)
  explains why this is the whole ordering protocol. The wait is on `s_cfg`, the kernel's own stream —
  it costs nothing on the bus.
- **One packet, one config.** The host cuts packets at every switch point, so `_filter` filters a
  whole packet with one set of taps — one vectorized convolution. `hist` (the last 15 samples) keeps
  the delay line continuous from one packet to the next.
- **Typed reads and writes.** `get_pipelined(S16, n)` reads `n` int16 samples and the serializer
  unpacks them from their words; `array(S64, y)` is serialized one result per word. The kernel never
  sees a word.
- **The timing is the HLS body's.** `proc_ii = 1` and `proc_latency = 10` are the csynth report's
  interval and latency: the first result leaves 10 cycles after the first sample arrived, one per
  cycle after that — the same pattern as `PolyAccel`. `write_pipelined` anchors the output at
  `t_out_start`, so a packet's input and output overlap rather than adding.
- **The status, then the response, after the results.** A host that has a packet's response has its
  results, and a status that already counts the packet — so it never has to ask again.

## The host program

`FirHost` holds five endpoints and never an address:

| endpoint | type | one call |
|---|---|---|
| `cfg` | `StreamIFMaster` | `write(cfg)` sends one config to the kernel |
| `qin` | `StreamIFMaster` | `write(...)` sends one queue-in packet — a header, or a packet's samples |
| `qout` | `StreamIFSlave`, unframed | `get_array(S64, n)` takes *n* results |
| `qresp` | `StreamIFSlave`, unframed | `get_schema(FirRespHdr)` takes one response |
| `status` | `LatestValueIFSlave` | `read()` returns the latest status |

The system gives it those endpoints, either through the adaptor or joined straight to the kernel
([Python simulation](pysim.md) shows both), so the same class runs over the bus and without it.

It runs as two processes, laid out by `host_schedule`. The **writer** commits each config and sends
each packet — its header, then its samples:

```python
    def _writer(self):
        for item in self.schedule:
            if item[0] == "cfg":
                yield from self.cfg.write(make_cfg(item[1]))
            else:
                _, n0, n1, tag, _want = item
                yield from self.qin.write(FirCmdHdr(nsamp=n1 - n0, tx_id=self._tx_id(n0),
                                                    cfg_seq=tag))
                yield from self.qin.write(array(S16, np.asarray(self.x[n0:n1], dtype=np.int64)))
```

It never asks whether a config has arrived: the header's `cfg_seq` makes the kernel wait. Packets are
cut at every switch point, so each sees one config, and each is tagged with the config it needs. The
header and the samples are two queue-in writes, so each reaches the kernel as one burst and the
kernel's exact-count reads take each whole.

The **reader** takes one output packet per input packet, then its response, and checks it:

```python
    def _reader(self):
        for item in self.schedule:
            if item[0] == "pkt":
                _, n0, n1, _tag, want = item
                y = yield from self.qout.get_array(S64, n1 - n0)
                self.y += [int(v) for v in y.val]
                resp = yield from self.qresp.get_schema(FirRespHdr)
                got = (int(resp.tx_id), int(resp.cfg_seq))
                self.responses.append(got)
                for name, exp, val in (("tx_id", self._tx_id(n0), got[0]), ("cfg_seq", want, got[1])):
                    if exp != val:
                        self.mismatches.append((got[0], name, exp, val))
```

Queue out is unframed — the bus cannot see where the kernel's packets end — so the reader names how
many results it wants, and it knows because it reads the same schedule.

**Nothing polls.** Over the bus, the queue endpoints sleep on the views'
[interrupts](../../guide/interface/axi_mm/slave.md#interrupts): queue in's for room, queue out's and
the response FIFO's for data. They never read a count, and never issue a transfer a queue would stall
— which matters here, because the writer and the reader share one bus master and, behind one front,
one adaptor: a write stalled on a full queue would hold the front, the reader's pops could not get
through, and the host would deadlock.

After the last response the reader reads the status once:

```python
        self.final_status = yield from self._read_status()
```

One read is enough because the kernel publishes its status *before* each response, so a host holding
the last response knows the status already counts every packet. (Publishing after it would leave the
host a choice between reading a stale status and asking again until it changed — a poll.)

Two knobs exercise the protocol:

- **`lag`** commits each config (after the first) that many samples after the packets that need it
  went out. Those packets wait in queue in until it arrives, and the output is still exact.
- **`stale_tag`** is the negative control: every packet is tagged with config 1. The kernel never
  takes config 2, and every response after the switch shows `cfg_seq = 1` where the host meant 2.

Next: [Python simulation](pysim.md).
