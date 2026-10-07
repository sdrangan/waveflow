---
title: "The Python model"
parent: Overview
grand_parent: Waveflow
nav_order: 2
---

# What a Waveflow component is

A common first reaction to "Python-native hardware" is: *you write a kernel in Python and transpile it to
HLS.* That is **not** what Waveflow does, and the difference is the whole point.

A Waveflow component is a structured **specification**, not a loop to be transpiled. It declares four
things — the **data** it consumes and produces, the **interfaces** it moves that data over, the
**parameters** that size it, and a compute **hook** — and from that one declaration Waveflow derives the
simulation, the synthesizable C++, the testbench, the host glue, and the documentation. The Python is the
source of truth; the HLS kernel is one of its *outputs*.

## The four parts

Here is the polynomial accelerator (`examples/stream_inband/poly.py`) at altitude — a real component,
trimmed to its skeleton:

```python
# 1. DATA — typed schemas for what crosses the interface
class CoeffArray(DataArray):                # the polynomial coefficients
    element_type = Float32
    max_shape = (4,)

class PolyCmdHdr(DataList):                 # a command header
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField},   # DATA or END
        "tx_id":    {"schema": TxIdField},          # transaction id
        "nsamp":    {"schema": NsampField},         # sample count
        "coeffs":   {"schema": CoeffArray},         # each command carries its own
    }

@dataclass
class PolyAccel(HostActivated):
    # 3. PARAMETERS — the knobs that size the hardware
    in_bw:  HwParam[int] = 32
    out_bw: HwParam[int] = 32
    clk:    Clock = ...

    def __post_init__(self):
        # 2. INTERFACES — typed ports, with direction
        self.s_in   = StreamIFSlave(...)    # commands + samples in
        self.m_out  = StreamIFMaster(...)   # responses + results out
        self.s_lite = VitisRegMapMMIFSlave(..., regmap=...)  # AXI-Lite: start, done, status

    # 4. HOOK — the behavior, as plain Python over the typed values
    def body(self):
        while True:
            cmd_hdr = yield from self.s_in.get_schema(PolyCmdHdr)
            if cmd_hdr.cmd_type == PolyCmdType.END:
                return
            ...                                       # response header, then:
            y = poly_eval(cmd_hdr.coeffs, samp_in)    # y = c0 + c1·x + c2·x² + c3·x³
            yield from self.m_out.write(array(Float32, y))
```

Read it top to bottom and the four parts are right there: **typed data** (`PolyCmdHdr`, `CoeffArray`), a
**declared interface** (a stream in, a stream out, an AXI-Lite register map for control and status),
**parameters** (`in_bw`, `out_bw`, the clock), and a **compute hook** (`body`) that is just Python and NumPy
over the typed values. Nothing here is a magic kernel — it is a description a machine can read.

## "Isn't that a lot of boilerplate for a three-line function?"

The polynomial math is three lines; the component around it is a few dozen. A fair question — with a
four-part answer.

**1. You're comparing the tip; the cost is the iceberg.** Those three lines aren't deployable hardware. To
actually ship them you also need a typed input/output contract, the packing logic that moves data over a
32- or 64-bit bus, the datapath, a testbench, the build script, the host-side driver, and a fast model to
explore with — all kept *consistent* with one another as the design changes. Hand-written, that is hundreds
of lines across half a dozen languages, re-synchronized by hand on every edit. Waveflow generates that
iceberg from the one declaration. Past a toy, it is *less* total work — and far less re-work.

**2. The "boilerplate" is the specification you were writing anyway.** The schemas, the interface, the bit
widths — every hardware project pins these down. The only question is whether they live *implicitly*,
scattered and duplicated across a notebook, a spreadsheet, a C++ header, and a testbench — or *explicitly*,
in one executable, checkable place. Waveflow makes you write the spec once.

**3. It buys what an HLS kernel alone cannot.** Because the component is structured, you get a
**NumPy-speed, bit-exact simulation** (no toolchain in the loop), **parameter sweeps** over the `bw` knobs
for design-space exploration, **composition** with other components, and a **golden** to check the
generated hardware against. A bare HLS function gives you none of these.

**4. It is the substrate AI needs.** An agent asked to "write the polynomial kernel" against a blank HLS
file produces something *plausible*. An agent asked to fill the `evaluate` hook against an explicit
interface contract, with a bit-exact golden to check against, produces something *verifiable*. Structure is
what turns AI output from a guess into a checkable artifact — see [the harness for AI](./aiharness.md).

## The real tradeoff

Waveflow optimizes the **lifecycle** and the **system**, not the one-off. For a script you will run once
and throw away, the three-line function wins — write it and move on. The value appears the moment the design
must be *simulated, swept, composed, verified, and regenerated* — which is to say, the moment it becomes
real hardware.

It is the familiar **typed-library-versus-throwaway-script** tradeoff, brought to hardware — except the
"library" here also generates its own simulation, its RTL inputs, its tests, and its documentation.

---

Next: [how you iterate on a component](./flow.md) — the fast all-Python inner loop and the Vitis-calibrated
outer loop.
