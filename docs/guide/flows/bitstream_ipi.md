---
title: Full system, on hardware
parent: Realization Flows
nav_order: 4
audience: python
summary: "Flow 4 — the assembled multi-block system taken all the way to a bitstream: blocks exported as IP, wired in Vivado IPI, and run on the FPGA with the host CPU as the driver. No simulation. Future work on the RFSoC bring-up path."
---

# Flow 4 — Full system, on hardware

**DUT output:** an **FPGA bitstream** — the assembled multi-block system, each block exported as IP and
wired together in **Vivado IPI**, synthesized to a bitstream.
**Testbench:** none — the "driver" is **host software** on the real FPGA.

Each block is exported as IP, wired in Vivado IPI, synthesized to a bitstream, and run on the FPGA with
the host CPU driving it. There is no simulation and no testbench here — it is real software on real
hardware.

> **Status: future.** This is the end of the realization ladder and the target of the RFSoC bring-up
> path. Stub only until the IPI / bitstream flow is built.
