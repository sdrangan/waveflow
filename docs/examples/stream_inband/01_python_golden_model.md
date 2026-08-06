---
title: Python Golden Model
parent: Streaming polynomial
nav_order: 1
---

# Python golden model

The first group is the Python golden — input vectors, a SimPy
simulation, and a structured cycle-count measurement.  Everything
downstream of this group is verified against artifacts the golden
produces.

| Step | Produces | What it does |
|------|----------|--------------|
| `build_inputs` | `coeffs`, `data_cmd_hdr`, `samp_in`, `end_cmd_hdr`, `data_dir` | Writes the four binary test-vector files into `data/` |
| `py_sim`      | `sim_dir`, `log` | Runs `PolyAccel` + `PolyTB` in SimPy; writes `results/sim/resp_hdr.bin`, `samp_out.bin`, `regmap_status.json` and a structured event log to `results/sim_log.csv` |
| `extract_py_timing` | `py_timing`, `durations` | Parses the event log into `results/py_timing.json` (structured `transaction_cycles` + raw event timestamps) |

## Schemas: the single source of truth

The same `DataSchema` definitions in
[`examples/stream_inband/poly.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly.py)
drive Python serialization, generated C++ headers, and runtime
sample-buffer sizing:

```python
class CoeffArray(DataArray):
    element_type = Float32
    static = True
    max_shape = (4,)
    cpp_storage = "raw"

class PolyCmdHdr(DataList):
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField, "description": "DATA or END"},
        "tx_id":    {"schema": TxIdField,        "description": "Transaction ID"},
        "nsamp":    {"schema": NsampField,       "description": "Sample count"},
    }
```

`BuildInputsStep` uses these classes to write the binary vectors that
both the Python sim and the C++ testbench (Group 2) read.

## The Python simulation

`PolyAccel` is a SimPy model of the kernel — it owns two
stream endpoints, an AXI-Lite `VitisRegMap`, and an `on_start` body
that runs as a `while True` coroutine.  `PolyTB` (the *SimPy* TB,
distinct from the codegen-source `PolyTBHls` in Group 2) writes
coefficients, raises `ap_start`, streams one DATA + END pair, and
captures the response.

The component carries timing parameters that the model uses to
approximate RTL behaviour:

```python
proc_ii:      int = 1
proc_latency: int = 40   # calibrated from RTL cosim — see Group 5
```

`proc_latency` is the fitted timing parameter — the manual v1 of the
future model-training workflow that will fit such parameters per
variant from a corpus of cosim measurements.  Group 5 closes that
loop.

## Structured timing artifact

`ExtractPyTimingStep` reads the SimPy event log and converts the
`samp_read_begin → samp_out_write_end` interval into a structured
JSON the cosim side can be compared against directly:

```json
{
    "transaction_cycles": 140,
    "transaction_seconds": 1.4e-06,
    "clk_freq": 100000000.0,
    "source": "py_sim",
    "events": {
        "samp_read_begin": 3.0e-08,
        "samp_out_write_end": 1.43e-06
    }
}
```

The named `transaction_cycles` field is the load-bearing one: it is
the input to `ValidateTimingStep` in Group 5 and to any future
parameter-fitting tooling that consumes a corpus of these files.

## Run just this group

```bash
python -m examples.stream_inband.poly_build --through extract_py_timing
```

Produces `results/sim/`, `results/sim_log.csv`, and
`results/py_timing.json`.

---

Next: [HLS code generation →](./02_hls_codegen.md)
