---
title: The polynomial's host
parent: The polynomial on a bus
nav_order: 3
---

# The host

`PolyHost` in [`stream_kernel_mm.py`](../../../examples/stream_kernel/stream_kernel_mm.py) is the
program on the other side of the bus: a [`SwHost`](../../guide/build/sw_threads.md) of two threads that
sends every command and collects every result and status. It has a C++ twin,
[`poly_kernel_host.h`](../../../examples/stream_kernel/poly_kernel_host.h), that runs the same scenario
at RTL.

## What the host has

```python
@dataclass
class PolyHost(SwHost):
    cmds: list = field(default_factory=good_cmds)
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    poll_cycles: int = 8

    cpp_model: ClassVar[str | None] = "PolyHostModel"
    cpp_header: ClassVar[str | None] = "poly_kernel_host.h"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.add_bus_master("m", bitwidth=W, max_outstanding=HOST_MAX_OUTSTANDING,
                            issue_cycles=HOST_ISSUE_CYCLES)
        self.q_in = self.q_out = self.q_status = None        # the system wiring sets these
        self.irq = {v: self.add_irq(f"irq_{v}") for v in HOST_ENDPOINTS}
        self.done = self.env.event()
```

What the software physically has: one bus master (`m`), an interrupt input per view, and three endpoints
left for [the system](system.md#the-hosts-endpoints-and-the-interrupt-lines) to wire. `cpp_model` and
`cpp_header` name the C++ twin.

## The scenario

`scenario_bursts()` is what both realizations send, one burst per command:
`[nhdr, <header words>, <sample words>]`, made from the stream example's `stimulus` -- what to send,
nothing about what comes back. The `mm_scenario` step writes it once
([The scenario](../../guide/build/xsi_system.md#scenario)); `pre_sim` reads it back (or makes it, in a
plain pysim run) into `(header, samples)` items.

## Two threads

```python
def _writer(self):
    for hdr, samples in self.items:
        yield from self.q_in.write(hdr)
        yield from self.q_in.write(samples)

def _reader(self):
    for _ in self.items:
        h = yield from self.q_out.get_schema(Resp)
        y = yield from self.q_out.get(nwords_max=h.nsamp)
        st = yield from self.q_status.get_schema(Status)
        self.out += [_u64(h.serialize(word_bw=W)), _u64(y)]
        self.status.append(_u64(st.serialize(word_bw=W)))
    self.done.succeed()

def main(self):
    self.start(self._writer)
    yield from self._reader()
```

- **The writer** sends each command's header and then its samples into `q_in`, each `write` one packet,
  as the kernel's framed `s_in` reads them.
- **The reader** takes, per command, the `Resp` header from `q_out`, then the results it announces, then
  the command's `Status` from `q_status` -- **in the order the kernel writes them**, so `q_out` never fills
  behind an unread status ([The protocol](../stream_kernel/protocol.md#why-the-status-has-its-own-stream)).
- `main` starts the writer as a second thread and is the reader itself; the reader's `done` ends the run.

Two threads, because the two directions are independent: the writer can run a command ahead while the
reader waits for the last one's results.

## Results sized from their header

A queue out drops TLAST ([The device](device.md#the-kernel-is-unchanged)), so a read names its size:
`get(nwords_max=n)` returns exactly `n` words. The reader takes `n` **from the `Resp` header**,
`h.nsamp`, never from what the writer sent. The count is part of the protocol, so any reader can size the
results -- one that never saw the command included. The header and status are read as schemas
(`get_schema(Resp)`, `get_schema(Status)`), never unpacked by hand.

## Interrupts, and no polling

Every endpoint was given its view's interrupt line, so every call sleeps on it:

| call | sleeps until | then |
|---|---|---|
| `q_in.write(words)` | the queue in's room interrupt | the bus writes the packet |
| `q_out.get_schema(Resp)`, `q_out.get(nwords_max=n)` | the queue out holds the words wanted | the bus reads them |
| `q_status.get_schema(Status)` | the status is there | the bus reads it |

The endpoint writes the interrupt's threshold itself -- a read of `n` words sets it to `n` -- and never
reads a queue's count ([Interrupts](../../guide/interface/axi_mm/slave.md#interrupts)). So the host
**never polls**, and two gates check it:

- **in pysim**, `test_the_host_never_polls` spies on the bus master's reads and asserts that none is of
  a queue's status half ([`test_stream_kernel_mm.py`](../../../tests/examples/test_stream_kernel_mm.py));
- **at RTL**, the C++ host reports its polls in the run's `DONE` line: zero
  ([With a host](../../guide/systems/verification.md#with-a-host)).

## The C++ twin

```cpp
class PolyHostModel : public PolyHost_endpoints {
public:
    using PolyHost_endpoints::PolyHost_endpoints;
    void main() override {
        decode();                                   // ~ PolyHost.pre_sim: the scenario as items
        start("writer", [this] { writer(); });
        reader();
    }
private:
    void writer() {
        for (const Item& it : items_) {
            q_in.write(it.hdr);
            q_in.write(it.samples);
        }
    }
    static constexpr int W = 32;
    void reader() {
        for (size_t i = 0; i < items_.size(); ++i) {
            const Resp h = q_out.get<Resp, W>();
            q_out.get((uint32_t)h.nsamp);
            q_status.get<Status, W>();
        }
    }
    ...
};
```

The same two threads, line for line, on the generated `PolyHost_endpoints` base: the endpoints named as
in Python, each on its view at its bus address with its interrupt pin; the bus master, the scenario, the
scheduler, the cycle count and the traces come from the runtime ([Writing a host](../../guide/build/sw_threads.md#writing-a-host)).
No address, pin or threshold is written by hand. The C++ reader does not check values: the run checks
them from the traces.

## The gate: identical traces

Every endpoint records what crossed it, in pysim and at RTL, and `mm_compare` compares the two **per
endpoint, byte for byte** ([The gate](../../guide/build/sw_threads.md#the-gate-the-two-realizations-agree)).
`test_rtl_host_traces_match_pysim` requires traces for exactly `q_in`, `q_out` and `q_status`, no
mismatch, and the C++ host done. Before that, `test_pysim_matches_the_golden_through_the_bus` checks
pysim against the golden: every header, result burst and status, bit for bit, and as many of each as the
golden says -- a deadlock would look like success otherwise
([Every gate asserts counts](../../guide/systems/verification.md#every-gate-asserts-counts)).

Next, [Composition](composition.md): the device alone, and the timing check.
