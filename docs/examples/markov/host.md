---
title: The host
parent: Two kernels on a bus
nav_order: 3.2
summary: "MarkovHost, read as a recipe for writing a host program: what a host is (a module outside the synthesized cut that talks to the system only through endpoints, never addresses), the endpoints it owns and the ones the system hands it, its jobs, its two processes -- a writer that admits at most two jobs and a reader that takes each response and reads the job's results back from memory -- how both sleep on interrupts instead of polling, and how the run ends. A checklist for writing your own, and which parts only matter once the host has a C++ twin."
---

# The host

The **host** is the program that uses the accelerator: it sends the jobs, waits for the answers and
collects the results. In this example it is
[`MarkovHost`](../../../examples/markov/markov.py), and this page goes through it as a recipe: if you
write a host for your own system, these are the pieces you need.

A host is a module **outside the synthesized cut**. It is never turned into hardware; at RTL its C++
twin runs beside the top (see [XSI testbench](xsi.md)). In Python it is a
[`SwHost`](../../guide/build/sw_threads.md): software that runs as **threads** -- SimPy processes whose
code between waits takes no simulated time -- and owns what software physically has: a bus master and
the interrupt lines it waits on.

## The rule: endpoints, not addresses

The host never computes the address of a queue or a register. It is handed **endpoints** — the same
stream endpoints it would hold if it were wired straight to the kernels — and calls `write` and
`get_*` on them:

| endpoint | type | one call |
|---|---|---|
| `qcmd` | `StreamIFMaster` | `write(cmd_words)` sends one command to the generator |
| `qresp` | `StreamIFSlave` | `get_schema(MkvResp)` takes one response from the chain |

Whether those endpoints reach the kernels over a bus or directly is decided by the **system** that
wires them ([The system](system.md)), not by the host. That is what lets one host class run both
wirings. The only addresses the host knows are the ones it *hands out*: the memory region each job's
results are written to.

## Step 1: the class and the endpoints it owns

```python
@dataclass
class MarkovHost(SwHost):
    jobs: list = field(default_factory=list)
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    poll_cycles: int = 8
    #: Jobs outstanding at most -- a DynParam, so the C++ twin reads the same value.
    max_in_flight: DynParam[int] = MAX_IN_FLIGHT

    def __post_init__(self) -> None:
        super().__post_init__()
        self.add_bus_master("m", bitwidth=DW)            # creates and registers self.m
        self.qcmd: StreamIFMaster | None = None          # set by the system
        self.qresp: StreamIFSlave | None = None          # set by the system
        self.irq = {v: self.add_irq(f"irq_{v}") for v in ("qcmd", "qresp")}   # self.irq_qcmd, ...
        self.mem_reader = BusReader(self.m)
        self.mem: MemoryMod | None = None
        self.mem_bus_base: int | None = None             # None: read the memory directly
        self.done = self.env.event()
        self.results: dict[int, dict] = {}
        #: Jobs that may still be sent: the writer takes one, the reader gives it back.
        self.slots = SwSemaphore(self, int(self.max_in_flight), name="slots")
```

Two kinds of endpoint, and the difference matters:

- **What the host physically has** — its bus master `m` and the host ends of the interrupt lines it
  waits on (`IrqIFSink`s) — it **creates and registers** with `add_bus_master` / `add_irq`. These are
  its pins: when the host is replaced by its C++ twin at RTL, these are what that twin binds to.
- **What the host programs against** — `qcmd`, `qresp` — the system **gives** it, because only the
  system knows whether they are bus views or plain streams. They start as `None`.

`mem_reader` wraps the bus master for one more kind of access: reading each job's results straight out
of the shared memory (a plain bus read, not a queue). `mem` and `mem_bus_base` say where that memory
is; the system sets them.

## Step 2: the jobs

A job is a dict of `MkvCmd` fields (`default_jobs` makes four). The host gives each job a region of
the shared memory for its states `x`, and puts that address in the command — the chain writes there:

```python
    def _dst(self, j: int) -> int:
        return j * REGION_BYTES                       # job j's region, memory-local

    def scenario_bursts(self) -> list[np.ndarray]:
        out = []
        for j, job in enumerate(self.jobs):
            dst = self._dst(j) + (self.mem_bus_base or 0)
            cmd = MkvCmd(**job, dstaddr=dst).serialize(word_bw=DW)
            out.append(np.concatenate([_u64([dst, get_nwords(U8, word_bw=DW, shape=job["n"])]),
                                       _u64(cmd)]))
        return out

    def pre_sim(self) -> None:
        super().pre_sim()
        bursts = ...                                  # scenario_bursts() -- or a file, see below
        self.items = [(int(b[0]), int(b[1]), np.asarray(b[2:], dtype=np.uint64)) for b in bursts]
```

Each job becomes one item: **where its `x` lands, how many words `x` is, and the command as words**.
The program below walks `self.items`. Building them as words rather than keeping the `MkvCmd` objects
looks like an extra step, and for Python alone it is; it is what lets the host's C++ twin run the
*same* jobs from a file later (see [the scenario](xsi.md#the-host-in-c)). `_u64` builds every piece as
`uint64` — `np.concatenate` of a Python `int` list with a `uint64` array promotes to `float64` and
silently corrupts 64-bit words.

## Step 3: the program — two threads

```python
    def main(self):
        self.start(self._writer)              # a second thread
        yield from self._reader()             # this thread
```

`main()` is the host's first thread (`SwHost.run_proc` runs it); `start()` starts another. The host is
**two threads**, a writer and a reader, sharing one bus master — the shape of a real
host driver too. Each blocks on its own thing: the writer on a free slot (and on room in the command
queue), the reader on the next response. A single process would have to choose which to wait for
first; whatever it chose, it could not also be doing the other, so jobs would stop overlapping, and a
host whose queues can fill — a long burst of commands, a kernel waiting for its output to drain — can
deadlock outright. Two processes keep both directions moving and never wait for each other except
through what they share: the slots.

**The writer** sends each job's command, but never more than `max_in_flight = 2` jobs at a time -- the
slots are a `SwSemaphore`, a channel between the two threads:

```python
    def _writer(self):
        for _xaddr, _xwords, cmd in self.items:
            yield from self.slots.acquire()           # two jobs out: wait for the reader
            yield from self.qcmd.write(cmd)           # one command = one queue-in packet
```

**The reader** takes each response, reads that job's `x` back from memory, records the result, and
frees a slot:

```python
    def _reader(self):
        for _ in self.items:
            resp = yield from self.qresp.get_schema(MkvResp)
            j = resp.tx_id                       # which job this answers
            n = resp.n
            if self.mem_bus_base is None:             # direct wiring: read the memory itself
                x = self.mem.read_array(self._dst(j), U8, n).val
            else:                                     # over the bus
                xaddr, xwords, _cmd = self.items[j]
                words = yield from self.mem_reader.read(xwords, xaddr)
                x = read_array(words, U8, word_bw=DW, shape=n).val
            self.results[j] = dict(n=n, ones=resp.ones, x=x, t=self.env.now)
            self.slots.release()                      # a slot back to the writer
        self.done.succeed()
```

Three things in it are general:

- **The response says which job it answers** (`tx_id`), so the reader looks the job up rather than
  assuming order. (`default_jobs` numbers jobs `0, 1, ...`, so `tx_id` is also the index into
  `items`.)
- **The response means "done", not "seen".** The chain sends it only after the states are stored (its
  memory writer forwards it — see [Python model](python.md#the-chain)), so reading `x` right after the
  response is safe.
- **Every read names its size.** A queue read from the bus cannot see where a message ends, so
  `get_schema(MkvResp)` reads exactly one response's words, and the memory read asks for exactly the
  job's words.

## Step 4: waiting, not polling

Neither process ever reads a queue's count to see whether it can go on. The system hands the host
endpoints that **sleep on the queue's interrupt**: `qcmd.write` waits for the command queue's *room*
interrupt, `qresp.get_schema` for the response queue's *data* interrupt. The host's code does not
change for it — it is how the system builds the endpoints (`stream_master("qcmd", irq=...)`, see
[The system](system.md#step-8-interrupts-and-the-host-endpoints)). The gates check it: every bus read the host
issues is a response or a read of `x`; none is a count.

## Step 5: how the run ends

The reader fires `self.done` after the last job. The system runs the simulation until then:

```python
    def run(self) -> dict[int, dict]:                 # MarkovSystem.run
        self.sim.run_sim(until=self.host.done)
        return self.host.results
```

A free-running kernel never finishes — it waits for the next command forever — so the host's "I have
everything" is what ends the simulation.

## Writing your own host: the checklist

1. **Subclass `SwHost`.** Create what the host physically has with `add_bus_master` and `add_irq`
   (one per interrupt it waits on).
2. **Leave the program's endpoints to the system** — `StreamIFMaster` / `StreamIFSlave` /
   `LatestValueIFSlave` attributes set to `None`, which the system fills by view name.
3. **Name no address** except the data regions the host hands out (and tell the kernels about them in
   the commands).
4. **Encode the jobs as items in `pre_sim`**, so the program walks a list of word messages.
5. **One thread per direction** — `main()` plus `start()`ed threads, a writer and a reader — joined by
   a channel for what they share (here a `SwSemaphore` of in-flight slots), never one thread that
   waits for both.
6. **Match responses by an id the command carried**; read exactly the words you expect.
7. **Never poll**: wait on the endpoints, which wait on interrupts.
8. **Signal completion** with an event the system can run until, and keep the results on the object.

That is a complete host for pysim. For RTL it adds two class attributes, `cpp_model` and
`cpp_header`, naming its C++ twin; and `SwHost`'s `scenario` / `trace_dir` fields let the *same* host
be checked against that twin. They do nothing in a pure Python run and are explained in
[XSI testbench](xsi.md#the-host-in-c) and [Software threads](../../guide/build/sw_threads.md).

Next: [The system](system.md) — how `MarkovSystem` builds the kernels, the memory and the host, and
wires the host's endpoints either straight to the kernels or across the bus.
