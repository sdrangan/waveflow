---
title: "The Python model"
parent: Overview
grand_parent: Waveflow
nav_order: 2
---

# What a Waveflow module is

A common first reaction to "Python-native hardware" is: *you write a kernel in Python and transpile it to
HLS.* That is **not** what Waveflow does, and the difference is the whole point.

A Waveflow hardware module (an [`HwModule`](../guide/flows/modules.md)) is a structured
**specification**, not a loop to be transpiled. It declares four things — the **data** it consumes and
produces, the **interfaces** it moves that data over, the **parameters** that size it, and its
**behavior** — and from that one declaration Waveflow derives the simulation, the message structs and
their readers and writers, the HLS top and its ports, the RTL testbench, the calibration, and a host's
endpoints. The kernel's HLS body is written to read the same way as the Python behavior (a simple body
can be generated from it instead), and the RTL comparison checks the two against each other
([Defining the kernel in Python and HLS](../guide/systems/hwmodule.md)). The Python is the source of
truth; the hardware is checked against it.

## The four parts

Here is the free-running polynomial (`examples/stream_kernel/stream_kernel.py`, the guide's
[running example](../examples/stream_kernel/index.md)) at altitude — a real module, trimmed to its
skeleton:

```python
# 1. DATA — typed schemas for what crosses the interfaces
class CoeffArray(DataArray):                # the polynomial coefficients, c0..c3
    ncoeff: HwConst[int] = 4
    element_type = Float32

class CmdHdr(DataList):                     # one command: everything it needs
    elements = {
        "tx_id":  {"schema": U16},          # the host's command id, echoed back
        "nsamp":  {"schema": U16},          # samples announced
        "coeffs": {"schema": CoeffArray},   # each command carries its own
    }
# Resp(tx_id, nsamp) heads the results; Status(tx_id, status) reports on each command

@dataclass
class PolyKernel(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "poly_kernel"
    # 3. PARAMETERS — dataclass fields: the clock here; a width or a depth is an HwParam
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self):
        super().__post_init__()
        # 2. INTERFACES — typed ports, with direction
        self.s_in     = FramedStreamIFSlave(...)    # a command and its samples in
        self.m_out    = FramedStreamIFMaster(...)   # the results' header, then the results
        self.m_status = FramedStreamIFMaster(...)   # one status per command
        ...                                         # and its timed phases

    # 4. BEHAVIOR — run_iter: one firing, one command, plain Python over the typed values
    def run_iter(self):
        hdr = yield from self.s_in.get_schema(CmdHdr)
        yield from self.m_out.write(Resp(tx_id=hdr.tx_id, nsamp=hdr.nsamp))
        ...                                         # the samples, through a pipelined phase:
        y = poly_eval(hdr.coeffs, x)                # y = c0 + c1·x + c2·x² + c3·x³
        ...
        yield from self.m_status.write(Status(tx_id=hdr.tx_id, status=code))
```

Read it top to bottom and the four parts are right there: **typed data** (`CmdHdr`, `CoeffArray`), a
**declared interface** (three framed streams: a command and its samples in, the results out, a status per
command), **parameters** (dataclass fields — the clock here), and a **behavior** (`run_iter`, one firing
per command) that is just Python and NumPy over the typed values. Nothing here is a magic kernel — it is a
description a machine can read.

## "Isn't that a lot of boilerplate for a three-line function?"

The polynomial math is three lines; the module around it is a few dozen. A fair question — with a
four-part answer.

**1. You're comparing the tip; the cost is the iceberg.** Those three lines aren't deployable hardware. To
actually ship them you also need a typed input/output contract, the packing logic that moves data over a
32- or 64-bit bus, the datapath, a testbench, the build script, the host-side driver, and a fast model to
explore with — all kept *consistent* with one another as the design changes. Hand-written, that is hundreds
of lines across half a dozen languages, re-synchronized by hand on every edit. Waveflow generates most of
that iceberg from the one declaration — the contract, the packing, the top, the testbench, the build — and
checks the part you write, the HLS body, against the Python. Past a toy, it is *less* total work — and far
less re-work.

**2. The "boilerplate" is the specification you were writing anyway.** The schemas, the interface, the bit
widths — every hardware project pins these down. The only question is whether they live *implicitly*,
scattered and duplicated across a notebook, a spreadsheet, a C++ header, and a testbench — or *explicitly*,
in one executable, checkable place. Waveflow makes you write the spec once.

**3. It buys what an HLS kernel alone cannot.** Because the module is structured, you get a
**NumPy-speed, bit-exact simulation** (no toolchain in the loop), **parameter sweeps** over its build
parameters for design-space exploration, **composition** with other modules, and a **golden** to check
the hardware against. A bare HLS function gives you none of these.

**4. It is the substrate AI needs.** An agent asked to "write the polynomial kernel" against a blank HLS
file produces something *plausible*. An agent asked to write the kernel's body against generated message
structs and ports, with `run_iter` as its specification and a bit-exact golden to check against, produces
something *verifiable*. Structure is
what turns AI output from a guess into a checkable artifact — see [the harness for AI](./aiharness.md).

## The real tradeoff

Waveflow optimizes the **lifecycle** and the **system**, not the one-off. For a script you will run once
and throw away, the three-line function wins — write it and move on. The value appears the moment the design
must be *simulated, swept, composed, verified, and regenerated* — which is to say, the moment it becomes
real hardware.

It is the familiar **typed-library-versus-throwaway-script** tradeoff, brought to hardware — except the
"library" here also generates its own simulation, its RTL inputs, its tests, and its documentation.

---

Next: [how you iterate on a module](./flow.md) — the fast all-Python inner loop and the RTL-calibrated
outer loop.
