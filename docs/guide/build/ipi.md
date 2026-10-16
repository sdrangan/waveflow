---
title: IP Packaging and Block Design
parent: Build System
nav_order: 6.7
audience: python
api: [IpPackageStep, package_system, SystemIp, PackagedIp, BusInterface, read_component, BlockDesignStep, build_block_design, BlockDesign, BdLink, bd_links, add_system_steps]
summary: "A system packaged as Vivado IP, one per device: each device sub-top and the on-chip memory through ipx::package_project, from exactly the files the XSI gates compile, with their AXI interfaces inferred, their interrupt lines declared and the clock associated with every AXI interface; AMD's crossbar used as its .xci. One opt-in step, ip_package, on a system's DAG; each IP cached by content; what comes back read from each IP's own component.xml. Then a block design, block_design: the IPs and the crossbar instantiated and wired from the same spec as the XSI system top, every master's address map assigned at the crossbar's own windows, validate_bd_design as the gate, and the design read back against the system top. Worked on markov: four IPs, every link between them AXI4 or an interrupt."
---

# IP packaging and block design

The [XSI system simulation](./xsi_system.md) runs a system's DUT as one Verilog top. To put that DUT
on a board through a Vivado block design, its parts must be **IP**: cells a block design can
instantiate and wire. Two steps do it: `ip_package` packages the IPs, and
[`block_design`](#the-block-design) wires them. The packaging's unit is the **device**, the sub-top
the system top already renders: a kernel with its views, its adaptor and the bus writers acting for
it. The on-chip memory is a unit of its own (`plans/ip_packaging_routes.md`,
*Granularity: one IP per device*).

```bash
python examples/markov/markov_build.py --through ip_package
```

```
ip_package:
    xsi_work\markov\ip_repo\manifest.json
  markov_top: 3 IPs (waveflow.org:markov_top:markov_top_gen:1.0, waveflow.org:markov_top:markov_top_chain:1.0,
  waveflow.org:markov_top:markov_top_mem:1.0) + xilinx.com:ip:axi_crossbar:2.1; packaged now: ['gen', 'chain', 'mem']
    PASSED (18.9 s)
```

## What one IP is

Each sub-top is packaged with Vivado's `ipx::package_project`, from what that sub-top compiles:

- **its own module**, rendered alone (`SystemTopSpec.render_sub_verilog`);
- **its leaves'** hand-written Verilog: the AXI front, the views, a FIFO, a memory;
- **its kernels'** csynth Verilog.

These are the same files the [per-device synthesis](../resource/systems.md#per-device) reads, and
the IP imports them (`-import_files`), so it is self-contained. A gate checks that they are the files
the XSI gates verified, byte for byte. The IP's own module is also the very text the system top
holds for that sub-top.

The crossbar is not packaged. It is AMD's IP already, so the `.xci` the system flow generates for it
is used as it is.

## Its interfaces

A block design wires interfaces, not ports, so each IP's interfaces must say what they are:

| ports | interface | how |
|---|---|---|
| an AXI4 group facing a crossbar master port (`mi<k>_axi_*`) | AXI4 **slave** | inferred from the names |
| an AXI4 group facing a crossbar slave port (`si<k>_axi_*`) | AXI4 **master** | inferred from the names |
| `ap_clk`, `ap_rst_n` | clock, reset | inferred from the names |
| an interrupt line (`SystemTopSpec.irqs`) | interrupt | declared: the system top knows which outputs are interrupts |

Then the clock is **associated** with every AXI interface and with the reset (`ASSOCIATED_BUSIF`,
`ASSOCIATED_RESET`). That is what a block design's validation reads to put each interface in a clock
domain.

At device granularity, **every link between IPs is AXI4 or an interrupt**. The kernel-to-view
streams, the credit-stream pairs and the FIFOs are all inside a device, so no custom interface
definition is needed. markov's four:

| IP | VLNV | interfaces |
|---|---|---|
| generator device | `waveflow.org:markov_top:markov_top_gen:1.0` | `mi0_axi` slave, `si1_axi` master, `irq_gen_qcmd` |
| chain device | `waveflow.org:markov_top:markov_top_chain:1.0` | `mi1_axi` slave, `si2_axi` / `si3_axi` masters, `irq_chain_qresp` |
| memory | `waveflow.org:markov_top:markov_top_mem:1.0` | `mi2_axi` slave |
| crossbar | `xilinx.com:ip:axi_crossbar:2.1` | its `.xci` |

The AXI groups keep the names of the crossbar slots they meet, as the system top's nets do. The
generator's master is its forward writer, and the chain's two are its credit writer and its memory
writer.

## The step

`add_system_steps` puts `ip_package` on every system's DAG (`IpPackageStep`). It needs Vivado, so
it is **opt-in**: no default target depends on it. The step:

- **targets one part.** It packages for the part the kernels were csynth'd for
  (`tcl_target`: the build's platform, else the default), and generates the crossbar for that part;
- **packages in one session.** Every IP is packaged in one Vivado session, since a session costs
  20-40 s to start;
- **caches by content.** Each IP is cached by the content of everything it reads and by the
  packaging recipe's revision, so an unchanged device is never re-packaged and a re-run starts no
  Vivado.

It writes an IP repository, `<work>/ip_repo/`, with one directory per IP, and `manifest.json`. The
manifest is read back from each IP's own `component.xml`, not from what the script asked for. It
holds each IP's VLNV, ports and bus interfaces (the `PackagedIp` fields), plus the crossbar's
`.xci` and VLNV. The same is available in Python:

```python
from waveflow.build.system_ip import package_system

res = package_system(spec, kernel_dirs=kdirs, part=part, out_dir=work / "ip_repo", xbar_ip=xbar)
res.ips["gen"].vlnv              # 'waveflow.org:markov_top:markov_top_gen:1.0'
res.ips["gen"].interface("mi0_axi").mode    # 'slave'
```

## The gate

`tests/examples/test_markov_ip.py` (`-m xsi`, with the other Vivado gates) checks rung 3 of
`plans/board_packaging.md` on markov's four IPs:

- the expected VLNVs, and the crossbar's;
- exactly the expected interfaces, each with its bus type and mode;
- no port outside an interface;
- the clock associated with every AXI interface and the reset;
- an unchanged system re-packaged without Vivado;
- each IP carrying exactly what the XSI gates verified.

## The block design

The second step, `block_design` (`BlockDesignStep`), builds a Vivado block design from the IP
repository. It is opt-in like `ip_package`, and it depends on it:

```bash
python examples/markov/markov_build.py --through block_design
```

```
block_design:
    xsi_work\markov\bd\block_design.json
  markov_top_bd: 3 IPs + crossbar, 6 interface links, 41 ports; validated in 13 s
```

It instantiates each IP under its sub-top's instance name (`u_gen_sub`, ...) and the crossbar as
`u_xbar`, configured as the XSI crossbar is (`AxiXbarConfig.config_props`). Then it wires them
from the same `SystemTopSpec` the XSI system top is rendered from, by the rule the system top
already follows: **a sub-top's port is named for the net it carries.** So each IP interface meets:

| its name | it meets |
|---|---|
| `si<k>_axi` | the crossbar's slave slot *k* (`S0k_AXI`) |
| `mi<k>_axi` | the crossbar's master slot *k* (`M0k_AXI`) |
| a name another IP's interface also has | that interface: a direct link between two devices |
| any other name | a port of the design: the host's bus (`s0_axi`), an interrupt line |

The clock and the reset are ports of the design, wired to every cell.

**The address map is assigned, not inferred.** Vivado's `assign_bd_address`, left to itself, gives
each slave a window of its own choosing (1 GB each, on markov). The step instead maps every slave,
in every master's address space, at its crossbar slot's base and size. That is the address map
`assign_address_ranges` wrote once, in Python, and both the pysim crossbar and the XSI one decode.

`validate_bd_design` then runs. What the step returns is **read back from Vivado** after it, in
`<work>/bd/block_design.json` (`BlockDesign`):

- each interface net's two ends, and each net's (clock, reset, interrupts);
- each master's segments, with their offsets and ranges;
- the crossbar cell's configuration;
- the ports of the HDL wrapper `make_wrapper` writes;
- the session's `CRITICAL WARNING` lines.

The step also writes `<top>_bd.tcl` (`write_bd_tcl`), which recreates the design in any project
that has the IP repository on its path. Like the IPs, the design is cached by its inputs, so an
unchanged system starts no Vivado.

```python
from waveflow.build.system_bd import bd_links, build_block_design

bd_links(spec, sysip.ips)        # the connections, found by name -- no Vivado
bd = build_block_design(spec, sysip, period_ns=10.0, out_dir=work / "bd")
bd.segments["/s0_axi"]           # {'/u_gen_sub/mi0_axi/reg0': [0, 8192], ...}
```

Its gate, `tests/examples/test_markov_bd.py`, holds the design against the system top the XSI gates
run:

- it validates, with no critical warning;
- the wrapper has the system top's ports: the same names (an AXI group's in lower case),
  directions and widths, so the block design can stand in for the system top;
- its interface nets are the system top's links, and the clock, reset and interrupts are wired as
  there;
- every one of the four masters (the host and the three bus writers) sees every slave at its
  crossbar window;
- the crossbar cell holds the XSI crossbar's configuration after validation;
- an unchanged system starts no Vivado.

The block design is rung 4 of `plans/board_packaging.md`. The rungs after it, a processor and a
bitstream for a board, come with that plan.
