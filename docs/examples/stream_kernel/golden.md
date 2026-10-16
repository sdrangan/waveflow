---
title: The polynomial's golden
parent: Free-running polynomial
nav_order: 2
---

# The golden

Everything on this page is a pure function or a schema, in the first two sections of
[`stream_kernel.py`](../../../examples/stream_kernel/stream_kernel.py).

## The schemas

```python
class CmdHdr(DataList):
    elements = {
        "tx_id":  {"schema": U16,        "description": "the host's command id, echoed in the response"},
        "nsamp":  {"schema": U16,        "description": "samples announced (at least 1)"},
        "coeffs": {"schema": CoeffArray, "description": "c0..c3, constant term first"},
    }

class Resp(DataList):
    elements = {
        "tx_id":  {"schema": U16,         "description": "echo of the command's tx_id"},
        "nsamp":  {"schema": U16,         "description": "samples actually processed"},
        "status": {"schema": StatusField, "description": "OK, or TLAST_EARLY"},
    }
```

The same classes generate the C++ structs the HLS body reads and writes (`cmd_hdr.h`, `resp.h`), so the
two sides agree on every bit by construction. See [Data schemas](../../guide/schema/index.md).

## The arithmetic

`poly_eval(coeffs, x)` computes the polynomial in float32 in Horner order, one rounding per multiply and
per add -- the order the C++ uses, so the results match bit for bit. It is shared with
[Streaming polynomial](../stream_inband/index.md).

## Intent, stimulus, and expected outputs

A test scenario is a list of **intents**, `Cmd(tx_id, samples, coeffs, sent=None)`: what the command is
for, and how many samples are actually sent (`sent` fewer than `nsamp` is a burst that ends early).
Two functions turn intents into streams:

- `stimulus(cmds)` -- the input stream, one burst per header and one per sample run;
- `expected(cmds)` -- the two output streams, **computed from the intents**, not from the stimulus: per
  command, the results for the samples sent, and the response with the right status.

Computing the expectation from intent is what makes it a check: a bug in `stimulus` cannot hide by
being repeated in `expected`. `default_cmds()` is the test scenario -- five commands of different
lengths and coefficients, one of a single sample and one that ends early.

Next, [The kernel](kernel.md) that has to produce these outputs.
