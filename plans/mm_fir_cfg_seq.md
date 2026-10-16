# Plan: mm_fir on the in-band command-header pattern, with a config sequence number

> **Status (2026-10-03): BUILT** (branch `mm-host-endpoints`, unpushed). RTL bit-exact, 768 / 783.
> Where the build departed from the text below, the **Built** section at the end says so.
>
> **Superseded in part (2026-10-04):** the counted sequence number (`cfg_seq`, config *k* = the *k*-th
> COMMIT) became a **carried config id** -- `FirCfg.cfg_id`, named by the host; the header's `cfg_id`
> names the config a packet needs; the kernel takes configs until the one in force has that id
> (equality, not a count).  A count kept at both ends drifts the first time a host restarts or a commit
> is lost; an id in the message needs nothing to be in step.  Same wire sizes, same cycle counts.

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

## Built

- **A response FIFO was added** (the user's idea, mid-build): a second memory-mapped queue out,
  `qresp` at `0x3000`, on a new kernel port `m_resp`. Per packet the kernel writes
  `FirRespHdr(nsamp, tx_id, cfg_seq actually used)`, after the results. `FirCmdHdr` gained `tx_id`
  (16 bits; `cfg_seq` 16, `nsamp` 32 -- still one word). The wait ENFORCES the order; the echo
  VERIFIES it. The negative control became a wrong tag (`stale_tag`) exposed by the responses; the
  late commit (`lag`) became a positive test (waited for, still exact).
- **A config that never arrives is waited for forever** -- added to the limits. The wait is on the
  kernel's own `s_cfg` stream, never the bus (the user asked).
- **`FirCfg` puts `coeffs` first.** At 64 bits Python packs a DataList densely and the generated C++
  starts an array on a fresh word (`plans/stream_array_alignment.md`, unfixed). With `ntaps` first the
  RTL read the taps 32 bits late -- the RTL gate's first run caught it. 16 int16 taps = 4 full words,
  so array-first agrees in both layouts.
- **Samples are typed `S16`, packed by the serializer four to a word** (the user's catch: the first
  build hand-packed one sample per 64-bit word). Results are `S64`, one per word. The HLS body uses
  the generated `int16_array_utils::read_array_lane` / `int64_array_utils::write_array_lane`, holding
  the input lane across firings so it filters ONE sample per cycle (16 multipliers, not 64). RTL
  937 / 922 -> 768 / 783: a quarter of the sample words.
- **Cost, stated:** ~6 bus ops per packet against ~4 for the `apply_at` protocol (567 / 721); packing
  won back most of it. pysim 536 / 874 gets the ORDER of the two topologies wrong -- open.
