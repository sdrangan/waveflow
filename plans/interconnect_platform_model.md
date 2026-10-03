# Plan: the interconnect's timing in the platform model

> **Status (2026-10-03): proposed, not started.** Follows the mm_fir timing probe
> (`plans/mm_adaptor_host_endpoints.md`; commit "three pysim timing fixes").

## Why

The pysim crossbar now has two measured numbers that make it match AMD's `axi_crossbar` at RTL:
`latency_init = 4` and `latency_travel = 2` (of those 4, the cycles a request spends reaching the
slave). Today each design sets them itself -- mm_fir has `XBAR_LATENCY, XBAR_TRAVEL = 4.0, 2.0` -- so
every design that uses a crossbar either copies the numbers or forgets them, and nothing re-measures
them when the crossbar's configuration changes.

They are a property of **an interconnect as built on a platform**: the crossbar IP, its configuration
(mode, register slices, connectivity), and the clock. They are not a property of the FPGA part as
such -- XSI simulates the configured IP the same way for any part -- but a platform's choices (a slower
part needing register slices, a different clock, SmartConnect instead of `axi_crossbar` in a board
design) change them. That is exactly what the platform model already exists for: it carries the
fitted bus model for `m_axi` transfers to memory (`calib/platforms/<platform>/mm_bus.json`, resolved
through `Platform.resolve`).

The master settings found by the same probe (`MMIFMaster.max_outstanding`, `issue_cycles`) are **not**
platform data: they describe the host driving a master, and stay with whatever models the host.

## The design

1. **A stored interconnect model per platform.** Next to `mm_bus.json`, an `interconnect.json`:

   ```json
   {"axi_crossbar": {"clk_freq": 100000000.0, "latency_init": 4, "latency_travel": 2,
                     "config": {"data_width": 64, "addr_width": 32, "id_width": 1}}}
   ```

   keyed by the interconnect kind and the configuration fields that change its timing.

2. **Measured, never typed.** A calibration step derives the numbers from the crossbar's RTL, the way
   `BusCalib` fits `mm_bus.json`:
   - `latency_init`: an uncontended single-beat read through the crossbar
     (`test_axi_xbar_xsi.py` already measures the uncontended costs);
   - `latency_travel`: a write queued behind a read at a one-transaction-at-a-time slave -- the
     cycles the write's completion moves earlier than a full `latency_init` after the read's end
     (the mm_fir probe measured it this way, from the read<->write switches behind the front).
   Re-running the step after an IP or configuration change re-measures; the gate fails if the stored
   numbers no longer match.

3. **Resolved, not copied.** `AXIMMCrossBarIF` gains a way to take its latencies from the platform
   (`AXIMMCrossBarIF.from_platform(platform, ...)` or a `platform=` argument), and mm_fir drops its
   constants for it. A design that sets the numbers explicitly still can; a design that sets nothing
   keeps the old defaults, so nothing moves unasked.

## Gates

- the calibration step reproduces 4 / 2 for the configuration mm_fir uses;
- mm_fir's pysim numbers (734 / 792) are unchanged when its latencies come from the platform;
- `tests/calib/test_key_stability.py` unchanged (the latencies are interconnect data, not module
  structure).

## Open

- Whether the stored model is per *interconnect configuration* (data width, register slices) or per
  platform with one default configuration. Start with one entry per platform; split when a second
  configuration is measured.
- SmartConnect, for board designs (`plans/board_packaging.md`): a second interconnect kind, measured
  the same way, once a block design exists.
