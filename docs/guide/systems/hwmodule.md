---
title: Defining the kernel in Python and HLS
parent: Flow Overview
nav_order: 3.5
audience: python
api: [HwModule, FreeRunMod, run_iter, kernel_task, workload, TimedPhase, FramedStreamIFSlave, FramedStreamIFMaster]
summary: "Writing the kernel, twice: a Python model and an HLS body, which must agree -- the Python is the specification, the HLS is the hardware, and the RTL comparison is what checks them against each other. A hardware module (HwModule) is a SimObj with typed ports and a behavior -- one Python class that is both the model pysim runs and the source of the hardware. A free-running kernel is a FreeRunMod, and its class has a fixed structure: its C++ name, its build parameters as dataclass fields, its ports and timed phases in __post_init__, its behavior in run_iter (one firing, one job), its HLS body behind kernel_task(), and its scenarios in workload(kind). Shown on the polynomial's PolyKernel; then the command-response kernel's Python model, its HLS body written to read the same way, and the host that drives it."
---

# Defining the kernel in Python and HLS

A kernel is written **twice**:

1. **In Python** -- a module whose `run_iter` says what one firing does. It is the model pysim runs, and
   the **specification** of the hardware.
2. **In HLS C++** -- the body Vitis synthesizes. It is the **hardware**, written to read the same way as
   `run_iter`, line for line.

Nothing makes the two agree by construction except the parts that are generated from one source: the
message structs and their readers and writers (from the schemas), the lane routines (from the element
types), the top and its ports (from the module's ports). The rest -- the loop, the arithmetic, the error
handling -- is in both, and **the RTL comparison is what checks them against each other**: pysim runs the
Python, the RTL runs the HLS, and `compare` requires the same words
([Functional verification](verification.md)). So write the Python first, verify it against the golden,
then write the HLS to match it. (A simple body can be generated from `run_iter` instead of written --
[how a body is realized](../comp_codegen/freerunning.md) -- but the kernels in this flow write theirs.)

Everything else is derived from the one Python class: the testbench, the RTL top, the calibration.

## A hardware module

A **[`HwModule`](../flows/modules.md)** is a [`SimObj`](../sim/simobj.md) with **typed ports** and a
**behavior**. Three things define it: its endpoints (the ports, each an interface type with a
direction), how it is wired (to other modules, by interfaces), and what it does. It comes in three kinds:

| kind | what it is | in the flow |
|---|---|---|
| `HwModule` | a model with ports, simulated only | a testbench part, the world around the DUT |
| **`FreeRunMod`** | a kernel that runs on its own, fired once per message; a **leaf** (`run_iter`) or a **composite** (children wired by internal interfaces) | the DUT's kernels: one `hls::task` each |
| `HostActivated` | a kernel a host launches (`ap_start` / `ap_done`) | a device a host starts; its own sequential flow is [legacy](../legacy/index.md) |

A kernel in this flow is a `FreeRunMod`.

## A free-running kernel: the structure

Every `FreeRunMod` has the same parts. The polynomial's, with each part's job:

```python
@dataclass
class PolyKernel(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "poly_kernel"     # its name in C++: the top, the task
    calib_holdout: ClassVar[dict] = {"nwords": CALIB_HOLDOUT}  # values held out of the timing fit
    mm_views: ClassVar[tuple] = (...)                          # how a bus reaches it, behind one

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))   # parameters are dataclass fields

    def __post_init__(self) -> None:      # the structure: its ports and its timed phases
        ...
    def run_iter(self):                   # the behavior: one firing, one command
        ...
    def kernel_task(self):                # the HLS body that realizes run_iter
        ...
    def workload(self, kind="test"):      # the scenarios its harness plays
        ...
```

| part | what it says | who uses it |
|---|---|---|
| `cpp_kernel_name` | the kernel's name in C++ | code generation: the top, the task, the csynth script |
| dataclass fields | build parameters ([`HwParam`](../flows/parametrization.md): a width, a depth) and the clock | every flow: a different value is a different module |
| `__post_init__` | the ports, each registered with `add_endpoint`; the [timed phases](../timing_model/timed_phases.md) | pysim, the harness, the RTL top, calibration |
| `run_iter` | one firing: read a message, do the work, write the answer | pysim -- and the specification of the HLS body |
| `kernel_task()` | which hand-written HLS body realizes `run_iter`, and its argument order | code generation |
| `workload(kind)` | the scenarios, `{input port: bursts}`: `"test"`, `"calib"` | its [harness](pysim.md#the-cut) |
| `mm_views`, `calib_holdout` | optional: its memory-mapped side; the calibration's held-out values | a bus system; calibration |

### The ports

```python
def __post_init__(self) -> None:
    super().__post_init__()
    self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=W)
    self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=W)
    self.m_status = FramedStreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=W)
    for ep in (self.s_in, self.m_out, self.m_status):
        self.add_endpoint(ep)
    self.samp_phase = TimedPhase("samples", spans={...}, features=("nwords",), calib_dir=HERE / "calib")
    self.status_phase = TimedPhase("status", spans={...}, calib_dir=HERE / "calib")
    for ph in (self.samp_phase, self.status_phase):
        self.add_timing_phase(ph)
```

A **slave** port receives, a **master** port sends; a **framed** port carries TLAST, because the protocol
is made of messages ([Stream patterns](streams.md)). The timed phases are the module's timing, declared
here and calibrated later ([Timing and resources](calibration.md)).

### The HLS body it names

```python
def kernel_task(self):
    return KernelTask("poly_kernel_task", "poly_kernel_task.h", ("s_in", "m_out", "m_status"),
                      template_args=(W,))
```

The body's ports are named in the order the C++ function takes them; the generator writes the top
around it ([Simulating at RTL](rtl.md#a-modules-hls-body)). A leaf whose body can be extracted from
`run_iter` names none.

### A composite instead

A kernel whose work has stages that should overlap is a **composite**: no `run_iter`, only children
(`add_comp`) wired by internal interfaces (`add_if`), each child a leaf of its own -- block_eq's
broadcast, gain and apply ([Writing a free-running module](../flows/concurrent_python.md)).

## The command-response kernel in Python {#the-python-model}

For a [command-response](command_response.md) kernel, `run_iter` is one job -- in general form:

```python
# __post_init__: the job loop is a pipelined timed phase -- a latency, then the results at its rate
self.job_phase = TimedPhase(
    "job", spans={"latency": (self.s_in.first, self.m_out.first),
                  "fill_time": (self.m_out.first, self.m_out.last)}, features=("n",))
self.add_timing_phase(self.job_phase)

def run_iter(self):
    hdr = yield from self.s_in.get_schema(CmdHdr)                  # 1. the command
    n = hdr.n
    yield from self.m_out.write(Resp(tx_id=hdr.tx_id, n=n))        #    the results' header
    r = yield from self.job_phase.pipeline(                        # 2. n elements in, the work,
        lambda x: array(S64, golden(x.val)),                       #    vectorized, n results out --
        load=self.s_in.get_array(S16, n),                         #    overlapped, as in hardware
        store=self.m_out.write_array(S64, n), n=n)
    code = StatusCode.OK if len(r.y.val) == n else StatusCode.TLAST_EARLY
    yield from self.m_status.write(Status(tx_id=hdr.tx_id, status=code))   # 3. the status
    if code != StatusCode.OK:
        yield self.event()                                         # fatal: never triggered
```

- **The work is the golden**, called on the whole job at once -- a NumPy expression, not a Python loop
  over elements ([Vectorization](../vectorization/index.md)).
- **The timing is a [timed phase](../timing_model/timed_phases.md)** with the HLS loop's two numbers as
  its spans: the latency from the first element in to the first result out, and the fill at the loop's
  rate.  `pipeline` starts the results a latency after the first element arrived, so reading, computing
  and writing overlap as they do in hardware ([Pipelined bodies](../timing_model/streaming.md)); the
  numbers come from [calibrating the phase](../timing_model/fit.md), not the code.
- **A fatal error waits on an event nothing triggers**: the module stops taking input, as the RTL does.
- **`run_iter` is the specification of the HLS body**, which should read the same way, line for line.

## The HLS body

One firing of the `hls::task` is one job, written straight down -- the same three steps, and a flag for
the fatal error:

```cpp
static bool halted = false;                               // set by an error, cleared by a reset
#pragma HLS reset variable=halted
if (halted) return;                                       // stopped: read nothing, write nothing

CmdHdr h;
h.read_axi4_stream<W>(s_in);                              // 1. the command
Resp r; r.tx_id = h.tx_id; r.n = h.n;
r.write_axi4_stream<W>(m_out, true);                      //    the results' header

bool early = false;
for (int i = 0; i < h.n; i += PF) {                       // 2. the work, one word per iteration
#pragma HLS PIPELINE II=1
    ... read a word with the lane routines; TLAST before n sets early and breaks ...
    ... compute its lanes, write the result word; TLAST on the last one ...
}

Status s; s.tx_id = h.tx_id; s.status = early ? StatusCode::TLAST_EARLY : StatusCode::OK;
s.write_axi4_stream<W>(m_status, true);                   // 3. the status
if (early) halted = true;
```

The command, header and status structs come from the schemas; the loop over `n` is the **lane loop**,
because several elements are packed into each bus word. Both of the loop's shapes -- a word per
iteration with the work unrolled, or an element per iteration -- and why the body is a straight loop
rather than a state machine are in [Design patterns for loop optimization](../vectorization/hls/loop_optimization.md).

## The host

The host sends commands and collects results and statuses, usually as two processes -- a **writer** and
a **reader** -- so it never has to finish one job before starting the next:

- **Keep a bounded number of jobs in flight.** The writer sends a command only while fewer than `k` are
  outstanding; the reader frees a slot per status. That bounds everything a job leaves behind without
  the host polling anything.
- **Wait, do not poll.** Over a bus the host waits on each queue's interrupt
  ([Interrupts](../interface/axi_mm/slave.md#interrupts)).
- **Read as the kernel writes**: the header, the `n` results it announces, the status.
- **Check the echo.** Every header's and status's `tx_id` (and `cfg_id`, if there is one) against what
  was sent.

Next: [Simulating in Python](pysim.md) -- the kernel run beside everything else.
