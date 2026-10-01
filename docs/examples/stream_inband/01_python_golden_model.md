---
title: Python Golden Model
parent: Streaming polynomial
nav_order: 1
summary: "The Python half: schemas that generate the C++ wire format, a pure bit-exact model of the kernel (the arithmetic in the C++ operation order, and the whole protocol over a stream), scenarios whose expected responses are computed from intent rather than from any implementation, and pysim's cycle estimate for the timing check."
---

# Python model

The first group runs entirely in Python.  It produces the stimulus every later stage
reads, the expected responses every stage is checked against, and pysim's cycle
estimate.

| Step | What it does |
| --- | --- |
| `scenarios` | Writes each scenario's stimulus (`data/<scenario>/in`) and expected response (`data/<scenario>/expected`, `expected_status.json`) |
| `py_model` | Runs the pure model on every scenario, into `data/<scenario>/model` |
| `check_model` | Compares it with the expected responses |
| `py_sim` | Runs pysim -- the module's Python body with its timing model -- on the well-formed scenarios |
| `check_pysim` | Compares those |
| `extract_py_timing` | pysim's cycle count for the timing scenario, into `results/py_timing.json` |

## Schemas: one source for the wire format

The `DataSchema` classes in
[`poly.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly.py)
define the command header, response header, error codes and coefficients.  Waveflow
generates their C++ headers and serializers, so Python and C++ pack words with code
from the same declaration and nothing is packed by hand:

```python
class PolyCmdHdr(DataList):
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField, "description": "DATA or END"},
        "tx_id":    {"schema": TxIdField,        "description": "Transaction ID"},
        "nsamp":    {"schema": NsampField,       "description": "Sample count"},
    }
```

## The pure model

The model is two pure functions, with no simulator in them.  They are what a
system-level simulation calls, and what the C++ kernel is checked against, byte for byte.

`poly_eval` is the arithmetic.  It evaluates the polynomial in **Horner order, in
float32, one operation at a time**, which is exactly what the C++ body does:

```python
def poly_eval(coeffs, x):
    c = np.asarray(coeffs, dtype=np.float32)
    xs = np.asarray(x, dtype=np.float32)
    y = np.full(xs.shape, c[3], dtype=np.float32)
    for k in (2, 1, 0):
        y = (y * xs).astype(np.float32)
        y = (y + c[k]).astype(np.float32)
    return y
```

Matching the operation order is what makes the comparison exact rather than a
tolerance.  The C++ keeps each multiply and add a separate statement for the same
reason: a compiler may fuse `y * x + c` into a single multiply-add, which rounds once
instead of twice.

`poly_stream_model` is the whole kernel as a function of its input stream.  It reads
command headers, answers each `DATA` transaction with a response header and the
results, applies the TLAST rules, and returns the output bursts and the final register
status.  It splits its output at TLAST, the only boundary on the wire, so it compares
directly with what the C++ testbench records.

## The scenarios, and why their expected responses are independent

[`scenarios.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/scenarios.py)
describes each scenario as **intents** -- "a transaction with these samples", "one whose
sample burst ends early", "END":

| Scenario | What it covers |
| --- | --- |
| `nominal` | three back-to-back transactions of 100, 7 and 1 samples |
| `zero_len` | zero-length transactions, which have no sample burst and are not an error |
| `early_tlast` | a sample burst that ends 4 words early: the kernel answers what it read and halts with `TLAST_EARLY_SAMP_IN` |
| `no_tlast` | a sample burst with no TLAST on its last word: all outputs, then a halt with `NO_TLAST_SAMP_IN` |
| `timing` | one 100-sample transaction, for the cycle count |

From each intent it writes the stimulus *and* the expected response.  The expected
response comes from the intent ("this transaction's outputs, then a halt"), not from
parsing the stimulus as the model does.  So a model or a kernel that misreads the
protocol cannot pass by agreeing with a reference that shares its mistake.  The
arithmetic itself is pinned down separately, by worked examples with hand-computed
values in the example's tests.

The stimulus is a **burst bundle**: the words, the burst boundaries, and a TLAST flag
per burst.  A flag of 0 is how a scenario sends a malformed transaction.  The pure
model, pysim and the C++ testbench all read the same files.

## pysim and the timing estimate

`PolyAccel` is a body-only module: it declares its ports and register map and names its
kernel body, `cpp_body = "body"`.  Its Python `body()` is the same kernel for pysim -- a
thin port wrapper that calls `poly_eval`, plus a timing model:

```python
proc_ii:      int = 1
proc_latency: int = 40   # calibrated against RTL cosim
```

pysim runs the well-formed scenarios.  It cannot run the malformed ones, because a pysim
stream has no way to omit TLAST; those are checked through the pure model and the C++
kernel.  For the timing scenario, `extract_py_timing` turns the event log into a cycle
count:

```json
{
    "transaction_cycles": 140,
    "transaction_seconds": 1.4e-06,
    "clk_freq": 100000000.0,
    "source": "py_sim"
}
```

That is 100 samples at one per cycle, plus the 40-cycle latency.
[RTL co-simulation timing](./05_cosim_timing.md) compares it with what the RTL measures.

## Run just this group

```bash
python examples/stream_inband/poly_build.py --through check_pysim
```

No Vitis needed.

---

Next: [Code generation →](./02_hls_codegen.md)
