---
title: The Python model
parent: Rowwise FIR
nav_order: 3
has_children: false
audience: python
api: [HwComponent, transaction_queue, MMIFMaster.region]
summary: "FIRAccel as a HwComponent: three persistent SimPy stage processes (load / compute / store) handing off through fictitious inter-stage messages, a host-facing run_proc that pulls commands off the queue and kicks the pipeline without blocking, one shared golden (execute), and element-coordinate Region slices for memory access."
---
# The Python model

`FIRAccel` ([`fir.py`](../../../examples/rowwise_fir/fir.py)) is a single
[`HwComponent`](../../guide/flows/components.md) whose internal structure is three **concurrent** stage
processes. The shape is the [double-buffered timing model](../../guide/timing_model/double_buffered.md):
you cannot model load ∥ compute ∥ store inside one coroutine, so each stage is its own process and they
hand off through queues.

## Three stage processes, kicked by `run_proc`

`pre_sim` spawns the three persistent processes; `run_proc` is the host-facing entry that pulls each
command off the `s_in` control stream and hands it to the load stage **without blocking** — so
successive commands (and the three stages) overlap:

```python
def pre_sim(self):
    self.load_q    = self.transaction_queue()   # run_proc -> load
    self.compute_q = self.transaction_queue()   # load    -> compute
    self.store_q   = self.transaction_queue()   # compute -> store
    self.process(self.load()); self.process(self.compute()); self.process(self.store())

def run_proc(self):
    while True:
        cmd = yield from self.s_in.get(self.Cmd)
        self.load_q.put(cmd)          # kick the pipeline; do not wait for completion
```

## The fictitious inter-stage messages

The load→compute and compute→store hand-offs are plain dataclasses (`FIRCompMsg`, `FIRStoreMsg`). They
are **never synthesized** — in hardware the data moves through the BRAM ping-pong and the FIFO — so
they need not be `DataSchema` types. They carry the data plus the timing tag (`tstart`) the stages
thread through:

```python
@dataclass
class FIRCompMsg:    # load -> compute
    tstart: float    # ABS sim-time the first input row is available
    X: ndarray; h: ndarray; cmd: FIRCmd
```

## One golden, element-coordinate memory

Each stage does real numpy work and reads/writes shared memory through a
[`Region`](../../guide/memory/) — an **element-coordinate** view of the `m_axi` port (indices, not
bytes; the region resolves element→byte). Compute calls the **one** shared golden, `execute`
(= [`fir_golden`](./fir.md#the-golden)), so the simulation's `Y` is bit-exact with the kernel's:

```python
def load(self):    # ... read the row resident
    Xf, _ = yield from self._data.read_slice_pipelined(x0, x1, t_out_start=t_begin, num_trans=n_rows)
def compute(self): # ... the ONE golden
    Y = self.execute(msg.X, msg.h)
def store(self):   # ... write the output row
    yield from self._data.write_slice_pipelined(y0, Y.ravel(), t_out_start=anchor, num_trans=n_rows, ...)
```

The `read_slice_pipelined` / `write_slice_pipelined` calls are where the *timing* lives — the per-stage
spans, the channel contention, and the store-hides-under-compute overlap. That is the
[next page](./timing_model.md).

## Sim vs. hardware

Only the synthesizable kernel — the `@synthesizable` `dataflow` hook — becomes C++; the three stage
processes are a **sim-only** device whose job is to produce the overlapped timeline. The functional
truth (`execute`) is shared by both. So the same component is bit-exact-checked in Python and
synthesized from the [hand-written hook](./kernel_hook.md).
