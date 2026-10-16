---
title: RTL simulation
parent: A memory reached three ways
nav_order: 4
has_children: false
summary: "Running the design through the system flow under XSI, Vivado's shared-library simulation interface: the kernel and its memory as the DUT, the drivers and sinks answered by their own C++ models, all derived from the same graph the pysim testbench came from. Not cosim: this top is ap_ctrl_none with two hls::task bodies that never return, and Vitis cosim of such a design is unreliable — the generated top says so in its own header comment."
---

# RTL simulation

[Code generation](codegen.md) produced the synthesized kernel, and the memory beside it is
hand-written. This page runs the design through the **system flow** under **XSI**, Vivado's
shared-library simulation interface. The testbench is derived from the same graph the pysim testbench
came from, and the run produces the waveform [the next page](timing.md) reads.

```bash
python -m examples.bram_access.bram_access_system_build      # scenario zero, traced
pytest -m xsi tests/examples/test_bram_access_xsi.py         # the gate
```

## Why XSI and not cosim

Vitis HLS's own C/RTL cosimulation drives a kernel through its `ap_ctrl` handshake, and this design
has none: it is `ap_ctrl_none`, free-running, with two `hls::task` bodies that never return. Cosim of
such a top is unreliable — the generated top says so in its own header comment. XSI instead loads the
elaborated design as a shared library and lets a C++ testbench drive every pin cycle by cycle, which
is what a free-running design needs.

`BramAccessSystem` (in `bram_access.py`) is a [`GraphSystem`](../../guide/build/xsi_system.md#graph-system)
around the `BramAccessTB` graph. The cut is `BramAccess` alone, the kernel together with its memory.
The memory is hand-written RTL beside the kernel (`add_rtl_mod`), so the system top instantiates
the DUT as its **wrapper**, `bram_access_top`: the kernel and `bram_t2p` joined, rendered into the
top's own file. The steps are every system's:

- `csynth` C-synthesizes the kernel, unless its RTL is current;
- `system_rtl` derives the top;
- `scenario` and `pysim` write the stimulus and run the graph in Python;
- `system_xsi` compiles the RTL with `bram_t2p.v` on the compile list, builds the harness, and runs it;
- `compare` checks the three sinks' recordings, RTL against pysim, word for word.

## The harness drives pins; the memory is not one of them

The harness binds each testbench participant's own C++ model to a port of the system top: three
`AxisMaster`s for `cmd_w` / `data_w` / `cmd_r`, and three `AxisSlave`s for the answers. Each
`AxisMaster` loads the *same on-disk bundle* the pysim driver read, so both backends provably play
identical bytes.

**No model stands in for the memory**, because the memory is not on the boundary: it is inside the
DUT. In this simulation `bram_t2p.v` is the real thing, compiled with the kernel. There is no
emulation that could disagree with the hardware. That property is worth having, and an `m_axi`
design does not get it.

The generated main runs the scenario's cycle bound, which is a testbench constant rather than a
latency:

```cpp
int main() {
    bram_access_system_tb::Harness h("bram_access_system.wdb");
    h.run(wfbfm::run_param("n_cycles", 1000000));   // 4000, from vectors/run.json
    h.close();
    return 0;
}
```

Nothing terminates early. The sinks timestamp each word as it arrives, so the *real* completion is a
measurement rather than a stopping condition. Undersize the bound and the Python check afterwards
fails loudly rather than passing quietly.

## Producing the trace

The system build traces the run (`add_system_steps(..., trace=True)`). A `$dumpvars` module is
elaborated as a **second top**, and it dumps the wrapper's scope inside the system top:

```verilog
module vcd_dumper_bram_access_system;
    initial begin
        $dumpfile("bram_access_system_trace.vcd");
        $dumpvars(1, bram_access_system.u_xsi_tb_dut_sub.u_xsi_tb_dut);
    end
endmodule
```

**Level 1 of the wrapper is the right depth, and it is enough.** The memory's address, enable and
write-enable wires are declared in the *wrapper's own scope*, as the join between the kernel's
`bram` ports and `bram_t2p`. So a level-1 dump of that scope captures exactly what the next page
needs.

The run also writes `hazards.json`: the wrapper's hazard manifest, naming those wires, plus the
`scope` they live under. `bram_trace` resolves names under that scope, so the scan reads a system
run exactly as it would read the wrapper alone.

**Tracing costs no cycles.** The dumper is a separate top, so the XSI top, every BFM port number and
every cycle count are untouched. That is checked rather than assumed: the traced run finishes at the
same cycle the untraced gate records.

```python
import numpy as np
from pathlib import Path

run = Path("examples/bram_access/xsi_work/bram_access_system/xsi_run")
cycles = np.fromfile(run / "vectors" / "data_r" / "cycles.bin", dtype="<u8")
print("last read word at cycle", int(cycles[-1]))
```

```
last read word at cycle 568
```

## Checking the run

The RTL run is checked against the **same golden function** pysim is, reading what the sinks
recorded:

```python
from pathlib import Path
from examples.bram_access.bram_access import check_xsi_outputs, scenario_zero

check_xsi_outputs(Path("examples/bram_access/xsi_work/bram_access_system/xsi_run"), scenario_zero(),
                  want_cycles=568)
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
cyc = np.fromfile(Path("examples/bram_access/xsi_work/bram_access_system/xsi_run/vectors/data_r/cycles.bin"),
                  dtype="<u8")
i = 0
for c in sc.cmd_r:
    n = 0 if c.raddr + c.nsamp > DEPTH else c.nsamp
    if n:
        g = cyc[i:i + n]; i += n
        print(f"tid={c.tid:2d} n={n:3d} @{c.raddr:4d}: {g[0]}..{g[-1]}")
    else:
        print(f"tid={c.tid:2d} n={c.nsamp:3d} @{c.raddr:4d}: REFUSED, no data")
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

`tests/examples/test_bram_access_xsi.py` runs two traced systems, scenario zero and a scenario built
to collide, each in its own work directory, and checks twelve things. Five of them cannot be checked any other way:

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

## Traps this flow guards against

- **A cached snapshot proves nothing.** The flow reuses an elaboration only if its content stamp
  matches the RTL just written, and every run starts from a fresh copy of the scenario. So a failed
  run cannot leave the *previous* run's captures or waveform to be measured.
- **The scenario is an input.** Scenario zero and the collision are two systems with two work
  directories, so a figure step reading scenario zero's trace can never read the collision's.
- **Never trust a stale csynth.** The gate runs with `synth="check"`: RTL whose sources have changed
  since it was synthesized fails the run, naming it.

## See also

- [Reading the trace](timing.md) — the activity diagram, the hazard scan, and the comparison to
  pysim.
- [Code generation](codegen.md) — what this page runs.
- [Tracing a kernel run](../../guide/timing/trace_steps.md) — the trace steps in general.
