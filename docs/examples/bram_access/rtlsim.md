---
title: RTL simulation
parent: A memory reached three ways
nav_order: 4
has_children: false
---

# RTL simulation

[Code generation](codegen.md) produced three things: a synthesized kernel, a hand-written memory, and
a wrapper joining them. This page runs the wrapper through **XSI** — Vivado's shared-library
simulation interface — with a C++ testbench built from the same graph the pysim testbench came from,
and produces the waveform [the next page](timing.md) reads.

```bash
cd examples/bram_access
python bram_access_build.py --through rtl_trace
```

## Why XSI and not cosim

Vitis HLS's own C/RTL cosimulation drives a kernel through its `ap_ctrl` handshake, and this design
has none: it is `ap_ctrl_none`, free-running, with two `hls::task` bodies that never return. Cosim of
such a top is unreliable — the generated top says so in its own header comment. XSI instead loads the
elaborated design as a shared library and lets a C++ testbench drive every pin cycle by cycle, which
is what a free-running design needs.

The flow is four steps, all inside `xsi/run.bat` (`run.sh` on Linux):

- `xvlog -f rtl_bram_access_top.f` — analyze every file the list names: `csynth`'s output, then
  `bram_t2p.v`, then the wrapper.
- `xelab work.bram_access_top -dll -s bram_access_top` — elaborate **the wrapper** and build
  `xsim.dir/bram_access_top/xsimk.dll`.
- `g++` the generated BFM main against the XSI loader.
- run it.

## The harness drives pins; the memory is not one of them

`render_tb_harness` walks the same `BramAccessTB` graph that ran in SimPy and emits one BFM per pin:
three `AxisMaster`s for `cmd_w` / `data_w` / `cmd_r`, three `AxisSlave`s for the answers. Each
`AxisMaster` loads the *same on-disk bundle* the pysim driver read, so both backends provably play
identical bytes.

**No BFM stands in for the memory**, because the memory is not on the boundary — it is inside the
wrapper. In this simulation `bram_t2p.v` is the real thing, compiled by the same `xvlog` invocation
as the kernel. There is no emulation that could disagree with the hardware, which is a property worth
having and one an `m_axi` design does not get.

The generated main is four lines, and the run bound is a testbench constant rather than a latency:

```cpp
int main() {
    bram_access_tb::Harness h("bram_access_bfm.wdb");
    h.run(4000);
    h.close();
    return 0;
}
```

Nothing terminates early. The sinks timestamp each word as it arrives, so the *real* completion is a
measurement rather than a stopping condition; undersize the bound and the Python check afterwards
fails loudly rather than passing quietly.

## Producing the trace

The `trace` argument to `run.bat` additionally elaborates a `$dumpvars` module as a **second top**:

```verilog
module vcd_dumper_bram_access_top;
    initial begin
        $dumpfile("bram_access_top_trace.vcd");
        $dumpvars(1, bram_access_top);
    end
endmodule
```

Two things about that are specific to a wrapped design and both have bitten:

- **The dumper is named for the WRAPPER, not the kernel.** `run.bat` picks `vcd_dumper_%TOP%.v`, and
  a `$dumpvars` naming a scope that is not part of this elaboration is a hard error. A dumper emitted
  for `bram_access` would be the wrong file name *and* the wrong scope, and the run would produce no
  trace at all.
- **Level 1 is the right depth, and it is enough.** The memory's address, enable and write-enable
  wires are declared in the *wrapper's own scope* — they are the join between the kernel's `bram`
  ports and `bram_t2p` — so a level-1 dump captures exactly what the next page needs. Reaching
  *inside* the kernel from a wrapped top would need a scope prefix, and nothing needs that yet.

**Tracing costs no cycles.** The dumper is a separate top, so the XSI top, every BFM port number and
every cycle count are untouched. That is checked rather than assumed: the traced run finishes at the
same cycle the untraced gate records.

```python
import numpy as np
from pathlib import Path

xsi = Path("examples/bram_access/xsi")
cycles = np.fromfile(xsi / "vectors" / "data_r" / "cycles.bin", dtype="<u8")
print("last read word at cycle", int(cycles[-1]))
```

```
last read word at cycle 568
```

## Checking the run

The RTL run is checked against the **same golden function** pysim is, reading the bundles the sinks
dumped:

```python
from pathlib import Path
from examples.bram_access.bram_access import check_xsi_outputs, scenario_zero

check_xsi_outputs(Path("examples/bram_access/xsi"), scenario_zero(), want_cycles=568)
print("bit-exact against the pysim golden, and finished at 568")
```

```
bit-exact against the pysim golden, and finished at 568
```

The cycle count is **exact, not a bound**. A number that moves is either a regression or an
improvement, and both deserve a human — so when it moves, the new one is *accounted for* rather than
edited to fit. It has moved twice.

**386 -> 394**, when the commands became `DataList` messages: three words instead of the two the old
hand-unpacked pair occupied costs the reader one extra cycle per command, and it served eight.

**394 -> 568**, when the `COMPUTE` opcode landed. Every cycle of the `+174` is readable off the same
`cycles.bin`, one read command at a time:

```python
import numpy as np
from pathlib import Path
from examples.bram_access.bram_access import DEPTH, scenario_zero

sc = scenario_zero()
cyc = np.fromfile(Path("examples/bram_access/xsi/vectors/data_r/cycles.bin"), dtype="<u8")
i = 0
for c in sc.cmd_r:
    n = 0 if int(c.raddr) + int(c.nsamp) > DEPTH else int(c.nsamp)
    if n:
        g = cyc[i:i + n]; i += n
        print(f"tid={int(c.tid):2d} n={n:3d} @{int(c.raddr):4d}: {int(g[0])}..{int(g[-1])}")
    else:
        print(f"tid={int(c.tid):2d} n={int(c.nsamp):3d} @{int(c.raddr):4d}: REFUSED, no data")
```

```
tid= 1 n=  1 @   0: 274..274
tid= 2 n=  1 @   1: 282..282
tid= 3 n=  1 @   7: 290..290
tid= 4 n=  1 @ 255: 298..298
tid= 5 n=  1 @ 128: 306..306
tid= 6 n=  8 @1020: REFUSED, no data
tid= 7 n= 64 @   0: 320..383
tid= 8 n=128 @ 128: 391..518
tid= 9 n=  4 @1020: 526..529
tid=10 n= 32 @ 512: 537..568
```

The shape is **8 cycles of per-command overhead, then one cycle per returned word**, throughout. The
reader now returns 233 words against the old 73 and issues ten commands against eight, so:

```
160 extra words  +  2 extra commands x 8  +  8  ~=  +174
```

The trailing `8` is the arming token going out later: a write/compute command is four words now that
the opcode is a field, so the writer's first command takes one cycle longer per command to read.

**The `COMPUTE`'s own 63 cycles cost this number nothing**, which is worth reading twice. It runs at
418…480 while the reader is busy with its 128-word read at 391…518. Two free-running tasks sharing a
true-dual-port memory is what the design is for, and here it is in the arithmetic.

## The gate is more than the values

`tests/examples/test_bram_access_xsi.py` runs this twice — scenario zero and a scenario built to
collide — and checks twelve things. Five of them cannot be checked any other way:

- **`mode=bram` really took effect, and each port got exactly the halves it declared.** An unsized
  pointer degrades to an `ap_vld` scalar port silently, so "csynth OK" is not evidence of anything;
  the port list is. `buf_r` is read-only and must carry all fourteen nets; `buf_w` is read-write, so
  `ram_1p` gives it seven and a `_B` half appearing on it would mean the pragma reverted. Both
  directions are asserted, against names `bram_port_signals` derived without ever seeing this RTL.
- **The tasks are not gated.** A shared local array between two `hls::task` bodies becomes a PIPO
  channel whose handshake *stalls the writer*. The gate asserts the opposite: both tasks' `ap_start`
  and `ap_continue` are tied high.
- **The wrapper's shift is the shift Vitis emitted.** The generated task RTL literally contains
  `Addr_A_local = Addr_A_orig << 32'd3`, and the test greps for that number rather than trusting the
  emitter's belief about it.
- **The overlap really happened.** Checked in *cycles*, because the overlapping ranges are disjoint
  and the data would be identical either way.
- **The in-place loop costs two cycles per element, and not one.** Asserted from the waveform *and*
  from the csynth report, and refused in **both** directions: an II of 1 there would mean Vitis found
  a second physical port that the wrapper does not wire.

```bash
pytest -m xsi tests/examples/test_bram_access_xsi.py
```

## Traps this flow has

- **A cached snapshot proves nothing.** Re-running the built `.exe` does not re-elaborate and does
  not regenerate the VCD, so a failed or skipped run leaves the *previous* trace on disk and
  everything downstream is silently measured from the wrong run. The test deletes the snapshot, the
  built testbench, the capture bundles and the waveform before every run.
- **The scenario is an input.** The RTL gate leaves the *collision* vectors behind it, so a later
  trace step that did not write its own would render figures of whichever run went last. The build's
  trace step writes scenario zero itself.
- **Never trust the committed `.f`.** It is regenerated from the RTL actually on disk before each
  run.

## See also

- [Reading the trace](timing.md) — the activity diagram, the hazard scan, and the comparison to
  pysim.
- [Code generation](codegen.md) — what this page runs.
- [Tracing a kernel run](../../guide/timing/trace_steps.md) — the trace steps in general.
