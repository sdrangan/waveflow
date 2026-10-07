---
title: Why this contract
parent: Streaming polynomial
nav_order: 2
summary: "Why the stream_inband contract is the way it is, argued against the four designs students usually propose first: configuration over AXI-Lite (a race with the commands already in flight), a response footer with recovery after an error (hardware that cannot remove the reset), draining to TLAST on an error (it removes one burst and guarantees nothing), and configuration that persists across commands or activations (hidden state). For each, the failure it causes, and when a variant of it is the right choice."
---

# Why this contract

Why is the [contract](./index.md#the-contract) this way and not another?  Each rule rules out a
design that looks reasonable at first.  This page takes the four alternatives students most often
propose and shows what goes wrong with each.

## Configuration over AXI-Lite

*"The coefficients change rarely.  Put them in AXI-Lite registers, and send only samples on the
stream."*

It breaks as soon as the host changes the coefficients while commands are in flight.

The host does not hand commands to the kernel one at a time.  It queues them: a DMA engine is
working through a buffer of commands, and a FIFO in front of the kernel holds several more.  Now the
host writes new coefficients over AXI-Lite.  That write takes a different path from the stream, and
**nothing orders the two paths**.  Which commands are evaluated with the old coefficients, and which
with the new?  It depends on how far the DMA had got and how full the FIFO was at the moment the
register write landed.  The answer changes from run to run, and no test that runs one command at a
time will ever show it.

Putting the coefficients **in the command** (rule 2) removes the question.  Each command arrives
with its own coefficients, on the one path that is ordered.  Each command stands alone (rule 4).

**When a variant is right.**  Some configuration really is too large to repeat in every command: a
filter with hundreds of taps, a lookup table.  It can still live behind AXI-Lite or in memory, but
then the ordering must be made explicit.  The [memory-mapped FIR](../mm_fir/index.md) does this.
The host names each configuration with a `cfg_id`, each command names the `cfg_id` it needs, the
kernel waits until that configuration has arrived, and the response echoes the `cfg_id` it used.  It
works, but it costs an id in every message, a wait in the kernel, and a check in the host.  Use it
when you must, not by default.

## A response footer with recovery

*"On an error, the kernel should send a footer that says what went wrong and carry on with the next
command.  Then the host never has to restart anything."*

To carry on, the kernel must find the start of the next command.  Sometimes it can: after an early
TLAST, the next word is probably a header.  But "probably" is the problem.  After a missing TLAST,
the kernel does not know where the bad burst ends, so it will read the next command's header as
samples, or a sample as a header.  To recover reliably, the protocol needs a way to resynchronize:
a magic word, a length check, a sequence number.  The kernel needs the hardware to search for it.
The host needs code to parse error footers on every response and to decide what to resend.

**And the host still needs the reset.**  A kernel that resynchronizes on a pattern can be fooled
by the data.  A host that cannot tell what state the kernel is in has one safe move: reset it.  So
recovery does not replace the reset path, it adds a second one beside it, in hardware and in host
code, and both must be tested.

For a host-activated kernel that runs a bounded batch, an error ends the run (rules 6 and 7).  The
kernel spends no hardware on recovery, the host has one error path, and a response footer -- if the
design has one -- carries only results ([Adding a footer](./protocol.md#adding-a-footer)).

**When a variant is right.**  A kernel that must keep running through bad input -- a receiver
that cannot stop because one packet was malformed -- does need to resynchronize.  That is a
free-running kernel with a protocol designed for resynchronization, not this one.

## Draining to TLAST on an error

*"On an error, the kernel should read and discard input up to the next TLAST, so the stream is
clean for the next run."*

The drain removes **the current burst** and nothing else.

- **After an early TLAST** there is nothing left to drain: the TLAST already arrived.
- **After a missing TLAST** there is no TLAST to drain to.  The drain reads straight into the next
  command, discards its header, and stops at the end of *that* command's samples -- or never stops.
- **In both cases the commands the host had already queued** behind the failed one are still in the
  FIFO, and in the DMA engine's buffer behind it.  The drain does not touch them.

So the stream is not clean after a drain, and the host cannot know how much of it was eaten.  The
drain promises a recovery that does not exist.  The contract says the honest thing instead: after an
error the contents of `in_stream` are undefined, and the host resets the stream path (rule 7).

## Configuration that persists

*"The coefficients are the same for the whole run.  Send them once in a `CONFIG` command, or keep
them from the last run, instead of in every `DATA` header."*

Persistent configuration is **state**, and state is where bugs hide:

- **Across activations,** a host that forgets to configure gets the last run's coefficients,
  silently, with no error.  The results depend on a run that may have been minutes ago, or a
  different program.  A kernel that keeps nothing (rules 4 and 5) gives the same answer for the same
  stream every time.
- **Across commands within a run,** a `CONFIG` command makes every `DATA` depend on the `CONFIG`
  before it.  That brings a new error (`DATA` before any `CONFIG`), new state in the kernel, and
  tests that must cover the order of commands, not just each command.

For four coefficients, none of that is worth saving 16 bytes per command.

**When a variant is right.**  When the configuration is large compared with a command's data -- many
taps, short bursts -- a `CONFIG` command earns its keep.  Then the kernel starts each activation
unconfigured, a `DATA` before any `CONFIG` is an error with its own code, and the scenarios cover
`CONFIG` mid-run.  The contract's other rules stay as they are.

## Check your understanding

1. The host has eight `DATA` commands queued in a DMA buffer, and writes new coefficients to an
   AXI-Lite register halfway through.  With the old design, which commands use the new coefficients?
   With this one?
2. After a missing TLAST, the kernel drains to the next TLAST.  What has it consumed, and what is
   still in the FIFO?
3. Name one accelerator for which a separate `CONFIG` command is the better choice, and say what
   new error code it needs.

---

Next: [The error path →](./error_path.md)
