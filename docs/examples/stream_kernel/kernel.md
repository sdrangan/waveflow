---
title: The polynomial's kernel
parent: Free-running polynomial
nav_order: 3
---

# The kernel

`PolyKernel` is a [`FreeRunMod`](../../guide/flows/concurrent_python.md): a hardware module whose body,
`run_iter`, runs once per firing -- forever, unless an error stops it. It declares three framed stream
ports and its behavior; its HLS twin is a hand-written body; its top is generated.

## The ports

```python
@dataclass
class PolyKernel(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "poly_kernel"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=W)
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=W)
        self.m_status = FramedStreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=W)
        for ep in (self.s_in, self.m_out, self.m_status):
            self.add_endpoint(ep)
```

The ports are **framed**: each carries TLAST at RTL, because the protocol uses packet boundaries -- a
header is one packet, a sample run another, and an early TLAST is how a short burst announces itself.
`m_out` is framed the same way: its header one packet, the results another.

## One firing, one command

```python
def run_iter(self):
    hdr = yield from self.s_in.get_schema(CmdHdr)
    n = int(hdr.nsamp)
    coeffs = hdr.coeffs
    yield from self.m_out.write(Resp(tx_id=hdr.tx_id, nsamp=n))

    load = self.s_in.get_array(Float32, n)
    store = self.m_out.write_array(Float32, n)

    def compute(x):
        return array(Float32, poly_eval(coeffs, x.val))

    r = yield from self.samp_phase.pipeline(compute, load=load, store=store, nwords=load.nwords())
    code = StatusCode.OK if len(r.y.val) == n else StatusCode.TLAST_EARLY
    yield self.status_phase.timeout()
    yield from self.m_status.write(Status(tx_id=hdr.tx_id, status=code))
    if code != StatusCode.OK:
        yield self.event()          # fatal: never triggered, so the kernel stops taking s_in
```

Read the command and head the results, run the samples, report. The samples go through a **pipelined**
load, compute, store -- the HLS loop that reads a word, evaluates it, writes a word. The read and the
write are the same calls a sequential body makes (`get_array`, `write_array`), given to the phase
un-run, and `compute` is the pure function between them. A burst that ended early simply yields fewer
samples: `compute` gets what arrived, so many results go out, closed short, and the status says
`TLAST_EARLY`. Then the kernel waits on an event nothing triggers -- it stops, as the RTL does. The two
phases, `samp_phase` and `status_phase`, are the kernel's timing, on [Timing](timing.md).

## The HLS body

[`src/poly_kernel_task.h`](../../../examples/stream_kernel/src/poly_kernel_task.h) is the hand-written
twin of `run_iter`, written as the Python reads:

1. read the `CmdHdr` (a generated struct: `hdr.read_axi4_stream<W>(s_in)`), and write the `Resp`
   header on `m_out`;
2. the samples: a loop of **one word per iteration** at II=1 -- read a word, evaluate its lanes with the
   generated float32 lane routines, write the result word; the result completing `nsamp`, or answering
   an input word whose TLAST came early, carries TLAST, and an early TLAST ends the loop;
3. write the `Status`; after `TLAST_EARLY`, set `halted`.

`halted` is a `static` flag under `#pragma HLS reset`: once set, every later firing returns at once,
reading and writing nothing, until a reset clears it.

The body never packs or unpacks a word by hand: the lane routines are generated from the `Float32`
schema. See [Design patterns for loop optimization](../../guide/vectorization/hls/loop_optimization.md).

## The generated top

`kernel_task()` tells the generator which body to instantiate and in which argument order:

```python
def kernel_task(self):
    return KernelTask("poly_kernel_task", "poly_kernel_task.h", ("s_in", "m_out", "m_status"),
                      template_args=(W,))
```

From the module's ports the generator writes the free-running top -- three `axis` ports, `ap_ctrl_none`,
one `hls::task` running the body -- and its csynth script. Nothing about the top is hand-written.

Next, [The system flow](system_flow.md): the kernel run at RTL against pysim.
