# Accelerator: gain and clip

Build a Vitis kernel that computes:

```
y[i] = clip(gain * x[i], lo, hi)
```

- `lo` and `hi` are int16; `gain` is Q8.8.
- `x` and `y` are sent via streams.
- `gain`, `lo` and `hi` are sent in the command header.

Tests:

- Confirm the kernel is bit-exact with a Python model.
- Measure the error against floating point.
- Test the error handling on malformed data.
