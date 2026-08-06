---
title: Confirming the match
parent: Basic vector arithmetic
nav_order: 3
---
# Confirming the match

The [Python golden](./python.md) and the [Vitis kernels](./vitis.md) are compared
**bit-for-bit** by a small build DAG — the same `gen → csim → compare` machinery the rigorous
`examples/schemas/fixedpoint` conformance uses.

## The build DAG

```python
def build_basic_vec_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="basic_vec_source", path=...))   # the sources
    dag.add(SourceStep(artifact="kernels_source",   path=...))
    dag.add(SourceStep(artifact="run_tcl",          path=...))
    dag.add(GenStep(name="gen"))                                 # write kernels + vectors + golden
    dag.add(RunStep(name="run"))                                 # Vitis csim + compare bits
    return dag
```

- **`gen`** — for each kind, computes the Python golden (the operators), renders the matching
  kernel, and writes `kernel.cpp`, `in_{a,b,c}.txt`, and `expected.json` (the golden bits).
  No Vitis needed.
- **`run`** — runs each kernel in Vitis C-sim and asserts the emitted bits equal
  `expected.json` **exactly**; the first mismatch **stops the build**:

```python
failed = [r for r in results if not r["exact"]]
if failed:
    raise RuntimeError(f"STOP — Vitis disagreed with the Python golden: {failed[0]}")
```

## Running it

```bash
python examples/basic_vec/basic_vec_build.py --through gen   # generate (no Vitis)
python examples/basic_vec/basic_vec_build.py --through run   # the bit-exact conformance (Vitis)
```

When `run` passes, the Python operator model and the Vitis kernel produced **identical bits**
for int, float, and fixed — the whole point of `basic_vec`: a vectorized Python golden that
predicts the hardware exactly. The same contract runs as a test in
`tests/examples/test_basic_vec.py` (under `pytest -m vitis`).

## See also

- [Vectorization guide](../../guide/vectorization/) — the concepts this example embodies (the
  two paths, growth rules, why vectorized sim is fast *and* bit-exact).
- `examples/schemas/fixedpoint` — the rigorous all-modes/all-widths conformance sweep.
