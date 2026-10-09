---
title: Python simulation
parent: Two kernels on a bus
nav_order: 5
summary: "The whole system simulated in Python, two ways -- the kernels joined directly, and routed over one crossbar with four bus masters -- with the same kernel code. Bit-exact against the golden in both; what the gates check (no bus stall, batched credit, no host polling, at most two jobs in flight); the chain's output and its stationary probability; and how close pysim's timing is to the RTL."
---

# Python simulation

## Running it

```python
from examples.markov.markov import demo
demo("direct")["bit_exact"], demo("mm")["bit_exact"]      # (True, True)
```

`demo` runs four jobs of 300 steps (`default_jobs`) and compares every job's `x` with the golden.
`MarkovSystem(jobs=..., link=...)` is the system itself:

| `link` | between the kernels | between the host and the kernels | cycles |
|---|---|---|---|
| `"direct"` | a plain `CreditStreamIF` | plain streams; the memory joined directly | 1811 |
| `"mm"` | a `MmCreditStreamIF` over the crossbar ([The credit link](credit_link.md)) | the queues' views on the crossbar, interrupts to the host; the memory on the crossbar | 1926 |

The kernels are the same objects with the same code in both. The routed system is 6% slower: every
word between the kernels now crosses the crossbar, and the generator sometimes waits for credit.

## Results

| job | `p01`, `p10` (Q16) | x0 | `ones` | bit-exact |
|---|---|---|---|---|
| 0 | 4408, 4314 | 0 | 171 | yes |
| 1 | 10987, 12620 | 1 | 135 | yes |
| 2 | 14819, 2516 | 0 | 257 | yes |
| 3 | 4662, 9226 | 1 | 125 | yes |

Job 2 has a large `p01` and a small `p10` -- easy to enter state 1, hard to leave -- and spends 257 of
its 300 steps there; its stationary probability is 14819 / (14819 + 2516) = 0.85. The others sit near
the middle. Over 300 steps a chain this short has not fully converged; the long run is in the figure:

![The chain's state over its first steps, and the running fraction of ones converging to the stationary probability](images/chain_output.svg)

*From the golden model, which the RTL reproduces bit for bit. Rendered by a build step,
`python -m examples.markov.markov_build --through sync_docs_figures`; a test re-renders it and fails if the committed copy
is stale.*

## What the gates check

[`tests/examples/test_markov.py`](../../../tests/examples/test_markov.py), in both wirings:

- **bit-exact** -- every job's `x` against the golden, and `ones` against the golden's count; a job that
  is not a whole number of chunks (135 steps);
- **the bus is never stalled** -- the chain's queue never received a packet it had no room for
  (`nstall == 0`), while the generator did wait for credit;
- **credit is batched** -- one offer per 32 to 47 words consumed, and at most one bus write per offer
  (the credit writer merges offers that queue up behind a busy bus);
- **the host never polls** -- every host read is a response or a read of `x`; no count is ever read;
- **at most two jobs in flight** -- with eight jobs queued.

And the theory: over 20,000 steps the fraction of ones is within 0.03 of `p01 / (p01 + p10)`.

## How close is the timing?

The RTL takes **1870** cycles for this scenario; pysim says **1926**, 3.0% over. A gate keeps them
within 5% ([RTL simulation](rtlsim.md)).

pysim is a loosely-timed model, so its timing is only as good as what it charges. Here that is the HLS
bodies' rate -- one step per cycle -- plus each kernel's **fixed cost per chunk**, measured at RTL with
handshake probes: 7 cycles for the generator (its credit check, its loop's fill and drain) and 15 for
the chain (the memory command words, its 5-deep loop's drain, its credit offer). Before those two
numbers were in the model, pysim was 9% fast; before the two design fixes the probes also found, it was
24% fast against the RTL of the time. That story is [Finding the time](rtlsim.md#finding-the-time).
