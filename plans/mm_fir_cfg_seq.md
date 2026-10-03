# Plan: mm_fir on the in-band command-header pattern, with a config sequence number

> **Status (2026-10-03): proposed, building.** Follows `plans/mm_adaptor_host_endpoints.md` on branch
> `mm-host-endpoints`.

## Why

mm_fir carries cross-stream order with `apply_at` (a sample index in each config) plus a host that
polls the status until each config is *received* before sending the sample it applies at. That works,
but it is not the house pattern (stream_inband: `[CmdHdr | x...]`, read header, read samples
pipelined, write results pipelined), it needs a status round trip per config, and the pysim kernel
reads a packet by TLAST, which the RTL kernel does not have.

## The design

Every sample packet is preceded, in-band on `s_in`, by a one-word header:

```text
FirCmdHdr(nsamp, cfg_seq) | x[0] ... x[nsamp-1]
```

`cfg_seq` is the number of the config the packet needs: config *k* is the *k*-th COMMIT. The kernel:

1. reads the header;
2. while it has received fewer than `cfg_seq` configs, reads the next config from `s_cfg` --
   **blocking**: the packet waits for its config;
3. reads `nsamp` samples (pipelined), filters with the config in force, writes the results
   (pipelined);
4. publishes its status `(nsamp, ncfg)`.

**Order is carried in the messages** (the slave page's ordering statement 2), but as a sequence number
rather than a sample index:

- a packet can never use a config older than the one it names (the kernel waits for it);
- a packet can never use a newer one (a config the kernel has not been asked for stays in the stream);
- so the host commits config *k* and sends the packets tagged *k*, in either order, and never polls.

Packets are cut at config switch points, so one packet sees one config. `apply_at`, the pending slot
and `late` go away; the plan's `(apply_at, taps)` entries now only say where the host cuts and which
sequence number it tags each packet with.

The host writes the header and the samples as **two** queue-in packets (two `write`s). In pysim each
becomes one burst, so the kernel's exact-count reads (`get_schema(FirCmdHdr)`, then
`get_pipelined(..., nsamp)`) each take one whole burst -- no reliance on TLAST to find the header, and
none on the unframed-get truncation hazard recorded in `plans/mm_adaptor_host_endpoints.md`.

## Limits, stated rather than hidden

- **One config outstanding.** The register bank holds one config the kernel has not taken; a second
  COMMIT stalls the bus until the first is taken. A host must not commit config *k+1* while config *k*
  is untaken -- in practice: not until a packet tagged *k* has gone out.
- **A config committed late must fit the queue.** Packets tagged *k* sent before config *k* is
  committed wait in queue in; more than a queue's worth and the writer blocks on room while the kernel
  blocks on the config.

## Tests

- every rung bit-exact in all three wirings (unchanged);
- **late commit is waited for**: the host commits config 2 thirty-two samples after the packets
  tagged 2 went out; output still bit-exact (`lag`, which used to be the negative control);
- **negative control -- stale tag**: the host tags every packet with config 1; the kernel never takes
  config 2, the output equals the golden for taps A throughout, and status shows `ncfg = 1`;
- RTL: bit-exact both topologies; cycle counts re-measured.

## Touches

`examples/mm_fir/mm_fir.py` (schemas, kernel, host), `include/mm_fir_task.h` (HLS body), csynth,
`mm_fir_build.py` (the new header struct), `mm_fir_xsi.py` (C++ host), tests, the mm_fir docs, and the
slave page's ordering section.
