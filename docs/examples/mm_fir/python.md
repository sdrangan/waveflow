---
title: Python model
parent: A memory-mapped FIR
nav_order: 1
has_children: false
summary: "The Python side of mm_fir: two DataList messages (the config and the status) that also generate the C++ structs, an exact-integer golden, the kernel as a FreeRunMod whose run_iter is one config, one packet of samples, or one idle cycle, and the host program that commits configs, waits for them to be received, paces the samples and drains the results."
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
    order = sorted(cfgs, key=lambda c: c[0])
    for n in range(len(x)):
        taps = np.zeros(0, dtype=np.int64)
        for at, t in order:
            if at <= n:
                taps = np.asarray(t, dtype=np.int64)
        for k, c in enumerate(taps):
            if n - k >= 0:
                y[n] += int(c) * int(x[n - k])
    return y
```

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
cycle — and the config is checked **first**:

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
            out = np.zeros(len(pkt), dtype=np.uint64)
            for i, w in enumerate(np.asarray(pkt).tolist()):
                if self.pending is not None and self.pending[0] <= self.nsamp:
                    self.taps, self.pending = self.pending[1], None
                xn = _s16(int(w))
                window = np.concatenate([[xn], self.hist[:NTAP_MAX - 1]])
                acc = int(np.dot(self.taps, window[:len(self.taps)])) if len(self.taps) else 0
                out[i] = np.uint64(acc & 0xFFFF_FFFF_FFFF_FFFF)
                self.hist = window[:NTAP_MAX]
                self.nsamp += 1
            yield from self.m_out.write(out)
            yield from self._publish()
            return
        yield self.timeout(self.clk.period)         # idle: poll again next cycle
```

What to read in it:

- **Config first, and non-blocking.** `get_schema_nb` returns `None` when nothing is waiting. Because
  the config is checked even when no samples are arriving, a host that is *waiting for the config to
  be received* gets its answer — a kernel that blocked on `s_in` would deadlock that host.
- **One pending slot.** A received config is parked as `(apply_at, taps)` and put in force at the
  first sample whose index reaches `apply_at`. A second config arriving while one is pending first
  puts the pending one in force. The HLS body has exactly one slot too, which is why the Python does.
- **`late`.** A config whose `apply_at` has already passed is in force from the next sample and
  counted. The status makes the miss visible; nothing pretends it was on time.
- **The idle branch** waits one clock and returns. It is the pysim model of an HLS loop that checks
  both streams with `read_nb` every cycle — and because such a loop never runs out of events, the
  testbench ends the simulation explicitly (`Simulation.run_sim(until=...)`).

## The host program

`FirHost` is a `SimObj` holding an `MMIFMaster` — the one place a plain master endpoint belongs. It is
the protocol from the [index](index.md#the-one-subtle-part-switching-taps-mid-stream), written out:

```python
    def _commit(self, cfg: FirCfg, want_ncfg: int):
        yield from self.m.write(np.asarray(cfg.serialize(word_bw=DW), dtype=np.uint64), REGS)
        yield from self.m.write(np.asarray([1], dtype=np.uint64), REGS + 0x800)
        while True:                                   # received, so it cannot miss its sample
            st = yield from self._status()
            if int(st.ncfg) >= want_ncfg:
                return
            yield self.timeout(self.poll_cycles * self.clk.period)
```

The config goes into the shadow at `REGS` as one burst, the write to `REGS + 0x800` commits it, and
the host polls the status (`REGS + 0xC00`) until `ncfg` shows it arrived. Only then does it send the
sample the config applies at — and since a packet of samples never straddles a commit point, the
packet containing `apply_at` is cut there.

Between packets the host **drains** what is ready (read the occupancy at `QOUT + 0x800`, then pop that
many words) and **waits for room** (read queue in's vacancy until the next packet fits). Draining is
not optional: queue out is 64 deep, and a full output queue stops the kernel, which stops it taking
input, which would leave the host waiting for room forever.

```python
            chunk = [int(v) & 0xFFFF for v in self.x[n:end]]
            yield from self._drain()
            while int((yield from self.m.read(1, QIN))[0]) < len(chunk):
                yield self.timeout(self.poll_cycles * self.clk.period)
            yield from self.m.write(np.asarray([len(chunk)] + chunk, dtype=np.uint64), QIN)
```

The packet is `[len | samples]`: queue in frames in-band, so the length goes first.

`lag` is the negative-control knob. With `lag > 0` the host commits each config after the first
`lag` samples *after* its `apply_at` — deliberately late — and the kernel must report it.

Next: [Python simulation](pysim.md).
