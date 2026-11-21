# Design: the gain-and-shift offload

```text
host --q_cmd (QueueIn)--> s_cmd [ScaleCore] m_rd --> MemRStream --m_axi--> DDR
                                            s_rd <-- input words
                                            m_wr --> MemWStream --m_axi--> DDR
                                                     m_done = m_status --q_status (QueueOut + IrqIF)--> host
```

- **ScaleCore** is the kernel: a `FreeRunMod` leaf on four streams. Per job it reads a `ScaleCmd`, sends
  the reader `MemRCmd(src, words(n), 0)`, takes the `n` samples, computes, and sends the writer
  `[MemWCmd(dst, words(n), fwd_bursts=1) | ScaleStatus | y]`.
- **ScaleDev** is the device: the kernel and the framework's in-band `MemRStream` and `MemWStream`
  (`add_comp`, framed internal streams). It holds adaptors, so it is a device, not a kernel. Its
  `mm_views`: `QueueIn("q_cmd")` on `s_cmd`, `QueueOut("q_status")` on the writer's `m_done`.

## Choices where the spec left one open

- **Status: a queue out with its interrupt, not a register.** The host wants a status register *and*
  an interrupt when a job is done. Waveflow's register bank raises no interrupt
  (`interface/axi_mm/slave.md`, *What is not built yet*; `systems/command_response.md`, *On a bus*),
  so the status goes to a queue out whose level interrupt means "a status is waiting". A register bank
  for "the latest status at any time" could sit beside it; it would not wake the host.
- **Done means stored.** The status rides through the writer, which echoes it only once the output is
  written, so a host holding a status can read the output.
- **One input stream.** The command carries every parameter, so nothing must be kept in order between
  two streams.
- **Whole words.** The writer writes whole 64-bit words, four samples each: when `n` is not a multiple
  of four, the spare lanes of the last word are written as zero. The host allocates every output
  buffer in whole words.
- **Errors are fatal.** `n > 2048` is reported through the writer with nothing to write
  (`MemWCmd(len=0, fwd_bursts=1)`), then the kernel takes no more commands until a reset.
- **Admission.** The host keeps at most 4 jobs outstanding; the status queue holds 16, so the writer
  never stalls on it.

## Not built here (the RTL steps)

- The kernel's HLS body (its `kernel_task()`): the `waveflow-kernel-hls` step. In it, the request to the
  reader and the wait for its data are on two streams; the body must not write the request and then
  block on the reply in one state (`systems/streams.md`, *Read in the order written*).
- The host's C++ twin, for an RTL run (`build/sw_threads.md`).
