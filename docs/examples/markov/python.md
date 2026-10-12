---
title: Python model
parent: Two kernels on a bus
nav_order: 3
summary: "The Markov example in Python: the command and response schemas, the golden model (xorshift32 and the chain), the generator kernel, and the chain -- a composite of the chain core and the framework's in-band memory writer, so the response goes out only once the states are stored. Each kernel's run_iter is one job, the twin of its HLS body. The host and the system have pages of their own."
---

# Python model

Everything is in [`examples/markov/markov.py`](../../../examples/markov/markov.py).

## The messages

The command and the response are [`DataList`](../../guide/schema/python/datalists.md) schemas, so their
C++ structs and serializers are generated and neither side packs a word by hand:

```python
class MkvCmd(DataList):
    """One job: three 64-bit words, scalars only.  The generator forwards it to the chain."""
    elements = {
        "n":       {"schema": U32, "description": "steps to run"},
        "tx_id":   {"schema": U16, "description": "the host's job id, echoed in the response"},
        "x0":      {"schema": U16, "description": "initial state, 0 or 1"},
        "seed":    {"schema": U32, "description": "xorshift32 seed (0 is replaced by 1)"},
        "p01":     {"schema": U16, "description": "P(0 -> 1) in Q16"},
        "p10":     {"schema": U16, "description": "P(1 -> 0) in Q16"},
        "dstaddr": {"schema": U64, "description": "bus address x[0..n-1] is written to"},
    }

class MkvResp(DataList):
    """The chain's response to one job: two 64-bit words."""
    elements = {
        "n":     {"schema": U32, "description": "steps run"},
        "ones":  {"schema": U32, "description": "how many x[k] were 1"},
        "tx_id": {"schema": U16, "description": "echo of the job's tx_id"},
    }
```

The draws `u` are `U16`, packed four to a 64-bit word; the states `x` are `U8`, eight to a word -- both
by the serializer.

## The golden model

The reference the kernels are checked against, bit for bit, at both levels:

```python
def xorshift32(seed, n):           # n successive states after seed (0 is replaced by 1)
    ...
def uniforms(seed, n):             # the top 16 bits of each state
    return (xorshift32(seed, n) >> (32 - UBITS)).astype(np.uint16)

def chain_golden(u, x0, p01, p10):
    u = np.asarray(u, dtype=np.int64)
    t0 = u < int(p01)              # the compares, vectorized
    t1 = u >= int(p10)
    x = np.empty(len(u), dtype=np.uint8)
    s = int(x0) & 1
    for k in range(len(u)):        # the one sequential part: the select
        s = int(t1[k]) if s else int(t0[k])
        x[k] = s
    return x
```

xorshift is sequential by nature (each state comes from the last), and so is the chain's select; the
compares are not, and are computed for the whole job at once.

## The generator

One firing is one job: take the command, forward it, then send the draws in chunks.

```python
class MarkovGen(FreeRunMod):
    mm_views = (
        QueueIn("qcmd", port="s_cmd", depth=CDEPTH),     # the host writes commands here
        CreditIn("u_crd", port="m_u"),                   # the chain writes credit here
    )

    def run_iter(self):
        cmd = yield from self.s_cmd.get_schema(MkvCmd)
        yield from self.m_u.write(cmd)                   # forward the command
        n = cmd.n
        u = uniforms(cmd.seed, n)
        for k0 in range(0, n, CHUNK):
            c = min(CHUNK, n - k0)
            yield self.timeout((self.chunk_overhead + c * self.proc_ii) * self.clk.period)
            yield from self.m_u.write(array(U16, u[k0:k0 + c]))   # waits for credit
```

`m_u` is a credit stream's producer end (a `FramedCreditStreamMasterIF`), so each `write` waits until
the credit says the chunk fits -- see [The credit link](credit_link.md). The timeout is the HLS body's
timing: one draw per cycle, plus a fixed cost per chunk measured at RTL (`chunk_overhead = 7`).

## The chain

The chain is a **composite** of two parts:

- **`ChainCore`** -- the chain itself, streams only;
- **`MemWStream`** -- the framework's in-band memory writer, which stores the states and then
  forwards the response.

```python
class ChainCore(FreeRunMod):
    def run_iter(self):
        cmd = yield from self.s_u.get_schema(MkvCmd)
        n, x = cmd.n, cmd.x0 & 1
        dst, ones = cmd.dstaddr, 0
        for k0 in range(0, n, CHUNK):
            c = min(CHUNK, n - k0)
            u = yield from self.s_u.get_array(U16, c)            # credit goes back from here
            xs = chain_golden(u.val, x, cmd.p01, cmd.p10)
            x, ones = int(xs[-1]), ones + int(xs.sum())
            yield self.timeout((self.chunk_overhead + c * self.proc_ii) * self.clk.period)
            xw = array(U8, xs).serialize(word_bw=DW)
            yield from self.m_x.write(MemWCmd(addr=(dst + k0) // 8, len=len(xw), fwd_bursts=0))
            yield from self.m_x.write(np.asarray(xw, dtype=np.uint64))
        yield from self.m_x.write(MemWCmd(addr=0, len=0, fwd_bursts=1))
        yield from self.m_x.write(MkvResp(n=n, ones=ones, tx_id=cmd.tx_id))
```

The core talks to the writer in **frames**: per chunk `[MemWCmd(addr, len) | x words]` -- "write these
words here" -- and at the end `[MemWCmd(len=0, fwd_bursts=1) | MkvResp]` -- "write nothing, then
forward this". The writer handles frames in order and forwards only after the writes before them have
completed, so the response leaves **after the states are stored**. The chain needs no memory logic of
its own; the writer is the same one [mem_copy](../memcpy/index.md) uses.

```python
class MarkovChain(FreeRunMod):
    mm_views = (
        QueueIn("qu", port="s_u", depth=QDEPTH),         # the generator writes draws here
        QueueOut("qresp", port="m_resp", depth=RDEPTH),  # the host reads responses here
    )

    def __post_init__(self):
        ...
        self.core = ChainCore(...)
        self.writer = MemWStream(..., inband=True, emit_done=True, done_framed=False,
                                 max_fwd_words=int(MkvResp.nwords_per_inst(DW)))
        ...                                               # core.m_x -> writer.s_in, a framed FIFO
        self.s_u = self.core.s_u                          # the composite's ports
        self.m_mem = self.writer.m_mem
        self.m_resp = self.writer.s_done
```

## The host and the system

The rest of the Python side has a page each:

- [The host](host.md) -- `MarkovHost`, the program that sends the jobs and collects the results, read
  as a recipe for writing a host;
- [The system](system.md) -- `MarkovSystem`, which builds the kernels, the memory and the host and
  wires them, directly or across one bus, step by step.
