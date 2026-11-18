# Mem-stream follow-ups from the hwdesign histogram demo

## Where these came from

The agent that built the unit 9 histogram demo in `hwdesign` (repo `hwdesign-histogram`, branch
`demo/histogram`, `demos/histogram/hist_build.py`) reported five problems with the mem-stream
adaptors, measured on Vitis/Vivado 2025.1 with Waveflow's XSI bus models. The histogram uses the
**two-stream** protocol (`MRCmd` / `MWCmd` on `s_cmd`, `MemComplete` on `m_done`), not the in-band
(framed) one that `mem_copy` and the other curated examples use.

| # | report | status |
|---|---|---|
| 1 | the write completion leaves before the bus acknowledges the write | fixed on `fix/mem-w-done-after-bresp` (below) |
| 2 | the fixed 8-word `xfer_msg` makes every two-stream command long | **open**: [Command length](#2-command-length-open) |
| 3 | `MemStreamStep` copies unrelated headers | fixed: `MemStreamStep(headers=[...])` |
| 4 | `run_dag_cli --timing` marks a good XSI build FAILED | fixed: `trace_steps.xsi_output_ok` |
| 5 | the XSI memory models are optimistic | **open**: [Memory-model knobs](#5-memory-model-knobs-open) |
| 6 | `render_vcd_dumper` with nested scopes dumps only the outer one, silently (xsim 2025.1) | fixed: it raises `ValueError` naming the level to dump the outer scope at |

## 1. Completion before the write response (fixed)

`mem_w_stream_done_task.h` and `mem_w_stream_framed_done_task.h` wrote the completion on `m_done`
after the `S2A` loop, with nothing ordering it after the write response (B). Measured on the mem_copy
trace (2025.1): every job's `CopyResp` left **25 cycles before** that job's last B handshake, 16 of
16 jobs. A consumer that reads the destination on the completion can see stale data, and nothing
orders the two writes at all when the completion is itself forwarded to memory through another
`m_axi` port.

The fix is `hls::fence({m_mem}, {s_done})` (`hls_fence.h`, Vitis 2025.1; the C++ parameter is still
`s_done`, see `plans/memstream_m_done.md` S2) between the burst and the completion. The acceptance
check is per job: the first completion beat comes after that job's last B handshake
(`examples/mem_copy/mem_copy.py::check_completion_after_write_response`, gated in
`tests/build/test_xsi_snapshot_xsi.py`). The branch records the new gate cycle counts.

**What it cost (Vitis/Vivado 2025.1).** 4 cycles a writer firing (183 -> 187 at 128 words, 615 -> 619
at 512) and a later first completion: mem_copy 2908 -> 3000, markov 1870 -> 1951,
interleaver_inband 1300 -> 1326 (its 98-cycle period unchanged). markov's area +5 LUT, +1 FF. The
writer residual on `zynq7020_bfm_100mhz` was re-fit (intercept +4).

**Left over from item 1.**

- **pysim does not charge the write-response wait.** markov's pysim is 1854 against RTL 1951,
  -5.0%, at the edge of its 5% gate (pysim did not move; the RTL did). The bus model needs a
  write-response latency that `MemWStream` waits out before its echo -- the pysim half of item 5.
- **The staleness guard cannot see a framework header change.** `rtl_staleness` compares the RTL
  with the design's own `include/` copy, which only a build refreshes; `fir_block`, the systolic
  engine's memory harness and the vector-unit builds under `C:\wfsc` kept pre-fence copies and their
  gates passed on pre-fence RTL. The guard should hash the framework source the copy came from.
- **LTBF** (`tests/examples/test_ltbf_cg_mem_dse_xsi.py`) needs its local drops; re-measure on the
  lab machine.
- `docs/guide/resource/systems.md` quotes report outputs dated 2026-10-13 (pre-fence markov totals);
  re-run them with the next resource-model change.

## 2. Command length (open)

**Problem.** `MRCmd`, `MWCmd` and `MemComplete` are `ParamSchema`s with `max_xfer_len = Param(8)`. At
`MEM_DW=32` they serialize to 11, 11 and 10 words, and all 8 cookie words move whatever `xfer_len`
says. In the histogram, 13 of the 23 cycles from command to first read beat move the 11-word
`MRCmd`, whose cookie is never used (a reader with no completion). The read port also idles about 25
cycles between back-to-back jobs while `mem_r_stream_task` takes the next command.

**Why it is not just a parameter.** `MemRStream` / `MemWStream` already carry
`max_xfer_len: HwParam[int]` and specialize their pysim schemas with it, but the C++ side does not
follow: `MemStreamStep` copies fixed bodies and the `DataSchemaStep` entries emit the default
`max_xfer_len=8` structs under fixed names (`m_r_cmd.h`, `MRCmd`), and the bodies hard-code
`COPY_MSG: for (i < 8)`. Two designs with different capacities in one build would collide on the
names.

**Options, cheapest first.**

1. **Use the in-band protocol.** `MemRCmd` / `MemWCmd` are three words (`addr`, `len`,
   `fwd_bursts`) and carry no cookie: the response is forwarded opaquely. A design that only needs
   correlation can frame its own response. This is a docs change: say in
   `docs/guide/interface/axi_mm/master.md` when to prefer in-band, with the command-length cost in
   numbers.
2. **A no-cookie two-stream reader.** An `MRCmd` form without `xfer_len` / `xfer_msg` (2 words) for
   a `MemRStream` with `emit_done=False`, which never echoes the cookie. Needs its own schema name and
   header, and a body variant.
3. **Send only `xfer_len` cookie words.** Changes the wire format to variable-length; the
   `read_stream` / `write_stream` of a `ParamSchema` is fixed-length today. Most invasive.
4. **Generate at the design's `max_xfer_len`.** Name the schemas per capacity (`MRCmd8`, `MRCmd0`),
   emit them from the module's specialization, and template the bodies on the capacity.

Recommendation: (1) now, then (4) if a two-stream design still needs it -- (4) subsumes (2).

**Separately, the inter-job gap.** The ~25-cycle idle between back-to-back reads is the single-firing
body re-firing per command (the `hls::task` runtime) plus the command read. Measure it on the
`mem_r_stream` gate before and after a shorter command; if most of it survives, it is the re-fire,
and the fix is a body that reads the next command while the current burst drains.

**Acceptance.** On the `mem_r_stream` XSI gate (158 cycles today): command-to-first-beat and
inter-job gap measured before and after, the gate re-recorded with the new number.

## 5. Memory-model knobs (open)

**Problem.** `AxiMmReadSlave` and `AxiMmWriteSlave` (`waveflow/build/xsi/xsi_bfm.h`) answer one
cycle after the request and serve one burst at a time: reads run 16 beats per 17 cycles, writes
AW -> 16 W -> B per burst, and there is no first-word latency. Latency and throughput measured
against them are best cases, not DRAM.

**Proposal.** Optional knobs, as members like the AXIS models' `stall_pct` / `burst_gap_cycles`
(init-time config the harness emits from the pysim stand-in's DynParams):

- `read_latency` -- cycles from AR accepted to the first R beat;
- `write_resp_latency` -- cycles from the last W beat to BVALID;
- `max_outstanding` -- bursts accepted before the first is served (AR / AW queue depth).

Defaults reproduce today's behaviour exactly, so no gate cycle count moves; a test with non-default
knobs asserts the latency it set. The pysim memory stand-in should carry the same three DynParams so
the pysim and XSI runs of one scenario stay comparable.

**Acceptance.** All `-m xsi` gates unchanged at the defaults; a `mem_copy` run with
`read_latency=40`, `max_outstanding=4` is bit-exact and its completion cycle is checked against a
hand-computed bound.
