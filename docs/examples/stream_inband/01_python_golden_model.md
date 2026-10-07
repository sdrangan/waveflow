---
title: Python Golden Model
parent: Streaming polynomial
nav_order: 5
summary: "How we know the Python model is right: schemas that generate the C++ wire format (the command header carries the coefficients), a pure bit-exact model of the kernel -- the arithmetic in the C++ operation order, and the whole protocol over a stream at either width -- scenarios whose expected responses are computed from intent rather than from any implementation, a checker that is shown to reject wrong answers, and pysim's cycle estimate."
---

# Python model

How do we know the Python model is right?  Not by running it and looking at the output.  We
write down, separately, what each test *should* produce, and check the model against that.  This
page covers the first group of build steps, which runs entirely in Python.  It produces the
stimulus every later stage reads, the expected responses every stage is checked against, and
pysim's cycle estimate.

| Step | What it does |
| --- | --- |
| `scenarios` | Writes each scenario's stimulus and expected response, at 32 and 64 bits (`data/w32/<scenario>/`, `data/w64/<scenario>/`) |
| `py_model` | Runs the pure model on every scenario |
| `check_model` | Compares it with the expected responses |
| `py_sim` | Runs pysim -- the module's Python body with its timing model -- on every scenario it can express |
| `check_pysim` | Compares those |
| `extract_py_timing_w32`, `_w64` | pysim's cycle count for the timing scenario |

## Schemas: one source for the wire format

The `DataSchema` classes in
[`poly.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly.py)
define every message.  The coefficients are a `CoeffArray` **inside the command header**, so each
`DATA` command carries the coefficients it is to be evaluated with:

```python
class CoeffArray(DataArray):            # c0..c3, constant term first
    ncoeff: HwConst[int] = 4
    element_type = Float32
    static = True
    max_shape = (ncoeff,)

class PolyCmdHdr(DataList):
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField, "description": "DATA or END"},
        "tx_id":    {"schema": TxIdField,        "description": "Command ID: echoed, or reported on error"},
        "nsamp":    {"schema": NsampField,       "description": "Sample count (0 for END)"},
        "coeffs":   {"schema": CoeffArray,       "description": "c0..c3, constant term first"},
    }
```

Waveflow generates the C++ struct and serializer for each schema.  Python and C++ then pack words
with code from the same declaration, and nothing is packed by hand.  The word-by-word layout is on
[Protocol and interfaces](./protocol.md#word-layouts).

## The pure model

The model is two pure functions, with no simulator in them.  They are what a system-level
simulation calls, and what the C++ kernel is checked against, bit for bit.

`poly_eval` is the arithmetic.  It evaluates the polynomial in **Horner order, in float32, one
operation at a time**, which is exactly what the C++ body does:

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

Matching the operation order is what makes the comparison exact rather than within a tolerance.  The
C++ keeps each multiply and add a separate statement for the same reason: a compiler may fuse
`y * x + c` into a single multiply-add, which rounds once instead of twice.

`poly_stream_model(bursts, word_bw)` is one kernel run as a function of its input stream.  It
starts with the status clear, reads command headers, and answers each `DATA` with a response header
and the results, using that command's coefficients.  It applies the TLAST rules a word at a time,
one or two samples per word depending on the width, and stops on `END` or on the first error.  It
returns the output bursts and the final status.  It splits its output at TLAST, the only boundary on
the wire, so its output compares directly with what the C++ testbench records.

## Scenarios, and why their expected responses are independent

[`scenarios.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/scenarios.py)
describes each scenario as **intents**, such as "a DATA command with these samples and these
coefficients" or "one whose sample burst ends after six samples".  Each scenario is one kernel run:

| Scenario | Commands | What it checks |
| --- | --- | --- |
| `multi_data` | three `DATA` (100, 7, 1 samples), `END` | back-to-back commands |
| `coeff_change` | the same 16 samples under coefficients A, B, A, then `END` | each command uses its own coefficients; nothing carries over (rule 4) |
| `zero_len` | `DATA` of 0, 5, 0 samples, `END` | an empty command answers with a header alone |
| `early_tlast` | `DATA` 20, then `DATA` 10 whose burst ends after 6, then `DATA` 5 and `END` | halt with `TLAST_EARLY_SAMP_IN`; 6 results, **closed with TLAST**; the last command and `END` stay unread |
| `no_tlast` | `DATA` 10 with no TLAST on its last word, then `DATA` 5 and `END` | 10 results, then a halt with `NO_TLAST_SAMP_IN` |
| `timing` | one `DATA` of 100 samples, `END` | the cycle count |
| `early_tlast_vcd` | `DATA` 8, then `DATA` 10 ending after 6, and nothing after | the error, with nothing left unread, so co-simulation can record it |

From each intent, the module writes the stimulus *and* the expected response.  The expected
response comes from the intent ("this command's results, closed, then a halt"), not from parsing the
stimulus as the model does.  So a model or a kernel that misreads the protocol cannot pass by
agreeing with a reference that shares its mistake.  The arithmetic is pinned down separately, by
worked examples with hand-computed values in the example's tests.

The stimulus is a **burst bundle**: the words, the burst boundaries, and a TLAST flag per burst.  A
flag of 0 is how a scenario sends a malformed command.  The pure model, pysim and the C++ testbench
all read the same files.

## The checker

`scenarios.check` compares any stage's output with the expected response, **exactly**: the number of
bursts, every word, every TLAST flag, and the final status.  For a scenario that ends in an error, it
also checks rule 6 on its own and reports *"the output burst in progress at the error was not closed
with TLAST"* if it was not.

A checker is only worth something if it rejects wrong answers.  The tests feed it three, and it
rejects each:

- one result word flipped by a bit;
- a kernel that did not clear its status: the testbenches **poison** the status registers before
  every run (`halted = 1`, an error code, `tx_id = 0xFFFF`), so a kernel that forgets rule 5 leaves
  that poison behind;
- an error scenario whose last output burst has no TLAST.

## pysim and the timing estimate

`PolyAccel` is a body-only module: it declares its ports and its status-only register map, and names
its kernel body (`cpp_body = "body"`).  Its Python `body()` is the same kernel for pysim -- a thin
port wrapper that calls `poly_eval` -- plus a timing model:

```python
proc_ii:      int = 1
proc_latency: int = 40    # calibrated against RTL cosim (page 05)
```

pysim runs every scenario except `no_tlast`: a pysim stream can end a burst early, but it cannot
omit TLAST.  The pysim testbench also waits for the kernel's `ap_done` **interrupt** rather than
polling, then reads the status.

For the timing scenario, `extract_py_timing_w*` measures **one whole kernel call**, from the start
of the body to its return, which is the same span the co-simulation report measures.
[RTL co-simulation timing](./05_cosim_timing.md) compares the two.

## Run just this group

```bash
python examples/stream_inband/poly_build.py --through check_pysim
```

No Vitis needed.

## Check your understanding

1. Why are the expected responses computed from the intent instead of by running the model?  What
   kind of bug would slip through otherwise?
2. A kernel passes every scenario except that its status after `multi_data` is
   `{"halted": 1, "error": 2, "tx_id": 65535}`.  What did it forget?
3. Why can pysim run `early_tlast` but not `no_tlast`?

---

Next: [Code generation →](./02_hls_codegen.md)
