---
title: C-Sim Functional Verification
parent: Streaming polynomial
nav_order: 3
---

# C-sim functional verification

The third group runs the generated C++ kernel under Vitis HLS C-sim
and compares its outputs against the Python golden's outputs.  The
"functional verification" name is literal: this group certifies that
the same inputs produce the same outputs in both Python and Vitis.

| Step | Produces | What it does |
|------|----------|--------------|
| `csim` | `csim_data_dir` | Invokes `vitis_hls run.tcl` with `WAVEFLOW_POLY_COSIM=0`; the generated `gen/poly_tb.cpp` reads `data/*.bin`, runs `poly(...)`, and writes `resp_hdr_data.bin`, `samp_out_data.bin`, `regmap_status.json` back into `data/` |
| `validate_csim` | `verify_report`, `vitis_dir` | Runs a generic `FunctionalVerifyStep` that compares the Vitis-side outputs against the Python-side outputs file-by-file |

## How the comparison is wired

`FunctionalVerifyStep` is a generic comparator — it knows nothing
about poly specifically.  All the poly-specific knowledge sits in the
declarative manifest the step is constructed with:

```python
dag.add(FunctionalVerifyStep(
    name="validate_csim",
    golden_dir_artifact="sim_dir",            # Python golden (Group 1)
    actual_dir_artifact="csim_data_dir",      # Vitis output (this group)
    extra_artifacts=["data_cmd_hdr"],
    schemas=[
        {"filename": "resp_hdr_data.bin",
         "golden_filename": "resp_hdr.bin",
         "schema": PolyRespHdr},
    ],
    arrays=[
        {"filename": "samp_out_data.bin",
         "golden_filename": "samp_out.bin",
         "elem_type": Float32,
         "count_from_extra": "data_cmd_hdr",
         "count_schema": PolyCmdHdr,
         "count_field": "nsamp",
         "rtol": 1e-6, "atol": 1e-6},
    ],
    jsons=[
        {"filename": "regmap_status.json",
         "expect_zero": ["halted", "error"]},
    ],
    output_dir="results/vitis",
    output_artifact="vitis_dir",
    report_path="results/verify_csim.json",
))
```

Three comparator flavours run:

- **Schema** — `PolyRespHdr` parsed from both sides, compared via
  `DataSchema.is_close`.
- **Array** — `samp_out` parsed as a `Float32` buffer with `count`
  pulled from the `data_cmd_hdr`'s `nsamp` field; compared via
  `np.allclose` with `rtol=atol=1e-6`.
- **JSON** — `regmap_status.json` parsed as a flat dict; `halted`
  and `error` must both be zero (no halt, no error).

## What you see when it passes

`results/verify_csim.json`:

```json
{
    "pass": true,
    "checks": [
        {"kind": "schema", "filename": "resp_hdr_data.bin", "pass": true},
        {"kind": "array",  "filename": "samp_out_data.bin", "pass": true},
        {"kind": "json",   "filename": "regmap_status.json", "pass": true}
    ]
}
```

On a mismatch the step raises `RuntimeError` with a one-line
description of every failed check, and the report records what
diverged.  `output_dir` (`results/vitis/`) mirrors every actual file
under its golden filename so a side-by-side hexdump is one
`diff -r results/sim results/vitis` away.

## Run just this group

```bash
python -m examples.stream_inband.poly_build --through validate_csim
```

Requires Vitis HLS on `PATH` (the C-sim step shells out to
`vitis_hls`).  Produces a populated `results/vitis/` and a green
`results/verify_csim.json`.

---

Next: [C-synth resource estimation →](./04_csynth_resources.md)
