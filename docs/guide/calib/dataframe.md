---
title: The corpus — CalibDataFrame
parent: Calibration
nav_order: 6
audience: python
api: [CalibDataFrame]
summary: "CalibDataFrame is the calibration corpus: a thin wrapper composing a pandas.DataFrame, one row per synth/cosim measurement. It adds only what a raw frame lacks — a per-row measured_at timestamp and a save/load storage path — and otherwise exposes the underlying frame as .df for native pandas filter/select."
---

# The corpus — `CalibDataFrame`

Before you can fit a [model](./models.md) you need the *measurements*: a table with one row per
synth or cosim run, holding both the **features** (sizes, parameters — `n_row`, `n_col`, …) and the
measured **targets** (`cycles`, `bram`, …). That table is the calibration corpus, and it is the thing
a future [model-training workflow](./index.md) reuses — so it is worth keeping structured and
persisted rather than scattered across ad-hoc scripts.

[`CalibDataFrame`](../../../waveflow/calib/calib.py) is that corpus. It is a deliberately thin
wrapper composing a `pandas.DataFrame` (exposed as `.df`) — it does **not** reimplement the frame. It
adds only the two things a raw frame lacks for this job:

- a per-row **`measured_at`** timestamp, stamped automatically on `add_datapoint`, so the corpus
  records *when* each measurement was taken;
- a **storage path** — `save` / `load` to CSV — so the corpus is a committed artifact.

Everything else is native pandas on `.df`.

## Building and using a corpus

```python
from waveflow.calib import CalibDataFrame

db = CalibDataFrame(columns=["n_row", "n_col", "cycles"])    # declare a column order (optional)
db.add_datapoint({"n_row": 1, "n_col": 64,  "cycles": 128})  # one cosim run; stamped measured_at
db.add_datapoint({"n_row": 4, "n_col": 64,  "cycles": 655})
db.extend([                                                  # or add several at once
    {"n_row": 4, "n_col": 256, "cycles": 2383},
    {"n_row": 8, "n_col": 256, "cycles": 4720},
])

db.df                       # the underlying pandas.DataFrame — filter / select natively
db.df[db.df.n_row == 4]     # e.g. just the 4-row points
db.df["cycles"].max()       # any pandas operation
len(db)                     # number of measurements
```

| member | what it does |
|---|---|
| `CalibDataFrame(columns=None, path=None)` | empty corpus; `columns` fixes display order, `path` is the default `save`/`load` target |
| `add_datapoint(point, *, measured_at=None)` | append one row (a column → value mapping), timestamped |
| `extend(points)` | append many rows |
| `.df` | the underlying `pandas.DataFrame` (filter / select / display) |
| `len(db)` | number of rows |
| `save(path=None)` | write the corpus to CSV (uses `path` or the `path` set at construction) |
| `CalibDataFrame.load(path)` | reload a previously saved corpus |

```python
db.save("calib/fir_grid.csv")                 # persist the corpus
db2 = CalibDataFrame.load("calib/fir_grid.csv")
```

## `measured_at` is metadata, never a feature

The `measured_at` column is **provenance, not a model input**. Models select explicit `basis` /
`target` columns (see [Models](./models.md)), so the timestamp can never leak into a fit — there is
no "use all columns" path that would sweep it in.

## See also

- [Models](./models.md) — fitting a `CalibModel` over the corpus's `basis` / `target` columns.
- [A worked example](./example.md) — building a corpus and fitting it end-to-end.
- [Calibration](./index.md) — the overall workflow this corpus feeds.
