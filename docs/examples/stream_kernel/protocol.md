---
title: The polynomial's protocol
parent: Free-running polynomial
nav_order: 1
---

# The protocol

The kernel speaks the [command-response pattern](../../guide/patterns/command_response.md) on three
AXI4-Streams of 32-bit words -- a data plane and a status plane:

```text
s_in     : CmdHdr(tx_id, nsamp, c0..c3) | x[0] .. x[nsamp-1]     TLAST on the header, and on the samples
m_out    : Resp(tx_id, nsamp)           | y[0] .. y[nsamp-1]     TLAST on the header, and on the results
m_status : Status(tx_id, status)                                 one per command, after its results
```

- **A command carries everything it needs.** The header holds the host's `tx_id`, the number of samples
  `nsamp`, and the four coefficients. Nothing carries over from one command to the next, so commands
  can arrive in any mix.
- **One firing is one command.** The kernel reads a command, heads its results with `Resp`, runs the
  samples, writes the status, and takes the next command. There is no start signal and no end-of-run
  command.
- **`m_out` mirrors `s_in`.** The results come headed by which command they answer and how many there
  are, so a reader sizes them from the stream itself.
- **`m_status` is the last command seen**: `tx_id` and `OK` -- or the error that stopped the kernel.

## Why the status has its own stream

The status could have travelled on `m_out` after the results, as one more message. It has a stream of
its own for three reasons:

1. **It goes somewhere else.** The results go to whatever uses them -- a host draining a queue, or in a
   receiver the next kernel. The status goes to control. On a shared stream, every consumer of results
   would have to recognize and skip status messages; here the results stream is nothing but a header
   and results.
2. **On a bus it becomes a register.** A status register can be read at any moment without touching
   the data -- above all when the data has stopped. A host that times out waiting for results reads it
   and learns which command the kernel last saw and how it ended. On real hardware that is often the
   first thing you look at: is the kernel alive, and where is it?
3. **It reports on a job, not on data.** One status per command, whatever the command's length, written
   after its results: "this job is done, and this is how it ended."

The cost is one more port, and one rule for a reader: read in the order the kernel writes -- the
header, the results, then the status -- or a full results stream blocks the kernel before it ever
writes the status ([the rules](../../guide/patterns/command_response.md#the-rules-that-make-it-robust)).

## An error is fatal

A sample burst can end early: TLAST arrives after `k` samples, fewer than `nsamp`. That is the
sender's bug -- the command promised `nsamp` -- so the kernel does not try to carry on. It:

1. produces results for the `k` samples that did arrive, the last one closed with TLAST -- so the results
   burst is shorter than its header announced;
2. writes `Status(tx_id, TLAST_EARLY)`;
3. **stops taking `s_in`**: it reads nothing more and answers nothing more, until a reset.

The test scenario puts such a command fourth of five, and checks that the fifth is never answered on
either stream.

The schemas that carry this -- `CmdHdr`, `Resp` and `Status` -- are on the next page, [The golden](golden.md).
