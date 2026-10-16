---
title: The polynomial's protocol
parent: Free-running polynomial
nav_order: 1
---

# The protocol

The kernel speaks the [command-response pattern](../../guide/patterns/command_response.md) on three
AXI4-Streams of 32-bit words:

```text
s_in   : CmdHdr(tx_id, nsamp, c0..c3) | x[0] .. x[nsamp-1]    TLAST on the header, and on the samples
m_out  : y[0] .. y[k-1]                                       TLAST on the last result
m_resp : Resp(tx_id, nsamp=k, status)                         one per command
```

- **A command carries everything it needs.** The header holds the host's `tx_id`, the number of samples
  `nsamp`, and the four coefficients. Nothing carries over from one command to the next, so commands
  can arrive in any mix.
- **One firing is one command.** The kernel reads a header, runs its samples, answers, and takes the
  next header. There is no start signal and no end-of-run command.
- **The response echoes the command.** `tx_id` lets the host match an answer to its question; `nsamp`
  says how many samples were actually processed.

## Errors are answered, not halted on

A sample burst can end early: TLAST arrives before `nsamp` samples. The kernel then:

1. produces results for the `k` samples that did arrive, the last one closed with TLAST;
2. answers with `status = TLAST_EARLY` and `nsamp = k`;
3. takes the next command.

There is no status register to read and nothing to reset. Everything the kernel has to say is on its
streams, so a host learns of an error the same way it learns of a success.

The schemas that carry this, `CmdHdr` and `Resp`, are on the next page, [The golden](golden.md).
