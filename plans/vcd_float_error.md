# `vcd_float_error` — `binary_str_to_numeric` decodes floats byte-swapped

**Status: FIXED** (2026-09-29, `waveflow/utils/vcd.py` + 23 tests in `tests/utils/test_vcd.py`). Found 2026-09-29 while building the
`hwdesign/demos/stream/avgfilt` timing diagram, which works around it.

---

## The prompt

```
Read plans/vcd_float_error.md and fix the bug it describes in
waveflow/utils/vcd.py, with the regression tests it lists in
tests/utils/test_vcd.py.  Run tests/utils/test_vcd.py before and after.
```

---

## The bug

`binary_str_to_numeric(bin_str, "float", wid)` in `waveflow/utils/vcd.py`
returns the wrong value for every float on a little-endian machine, which
means essentially every machine this runs on (x86 and ARM alike). It affects both 32- and 64-bit widths.

Reproduction, on Windows / Python 3.12:

```python
import numpy as np
from waveflow.utils.vcd import binary_str_to_numeric

bits = format(np.array([0.5], dtype=np.float32).view(np.uint32)[0], "032b")
binary_str_to_numeric(bits, "float", 32)     # -> 8.8e-44, should be 0.5

bits = format(np.array([0.5], dtype=np.float64).view(np.uint64)[0], "064b")
binary_str_to_numeric(bits, "float", 64)     # -> 2.8363e-319, should be 0.5
```

Anything that sets a `SigInfo`'s `numeric_type = "float"` gets garbage.
In `VcdParser`, `SigInfo.get_values()` calls this function for every
sample, so a timing diagram of a float `TDATA` shows labels like
`446136739208080681598976.00` where the wire carries `0.47`.

## Root cause

```python
elif dtype == 'float':
    # Float conversion (assuming IEEE 754 format)
    if wid == 32:
        int_value = int(bin_str, 2)
        value = np.frombuffer(int_value.to_bytes(4, byteorder='big'), dtype=np.float32)[0]
    elif wid == 64:
        int_value = int(bin_str, 2)
        value = np.frombuffer(int_value.to_bytes(8, byteorder='big'), dtype=np.float64)[0]
```

The integer is serialized **big-endian**, but `np.float32` / `np.float64`
mean **native** byte order, which is little-endian on these machines. The
four (or eight) bytes are read back in reverse. The code is only correct
on a big-endian host, which is presumably why it was never noticed.

A secondary defect in the same branch: for `dtype == "float"` with any
`wid` other than 32 or 64 (a 16-bit half, say), neither branch assigns
`value`, and the `return value` raises `UnboundLocalError` instead of a
clear message.

## Suggested fix

Reinterpret the integer's bits directly, which has no byte order to get
wrong:

```python
elif dtype == 'float':
    # IEEE 754: reinterpret the word's bits.  Viewing an unsigned integer
    # of the same width avoids serializing to bytes, so there is no byte
    # order to get wrong.
    int_value = int(bin_str, 2)
    if wid == 32:
        value = np.array([int_value], dtype=np.uint32).view(np.float32)[0]
    elif wid == 64:
        value = np.array([int_value], dtype=np.uint64).view(np.float64)[0]
    else:
        raise ValueError(f"Float conversion supports widths 32 and 64, not {wid}.")
```

Keeping the byte serialization and making it explicit, with
`dtype=">f4"` and `dtype=">f8"` in the two `np.frombuffer` calls, is an
equally correct one-token fix. The `.view` form is preferred because it
states the intent, bit reinterpretation, rather than a byte layout.

The `bin_str.zfill(wid)` above the branch already handles VCD values that
drop leading zeros, so no change is needed there. Keep it covered by a
test, though.

## Regression tests to add (`tests/utils/test_vcd.py`)

1. **Round trip, both widths.** For values such as `0.0`, `-0.0`, `0.5`,
   `-1.25`, `1e-30`, `3.4e38`, a subnormal, `inf` and `-inf`: take the
   bits with `np.array([v], dtype=np.float32).view(np.uint32)`, format them
   as a binary string, decode, and assert the result is **bit-identical**.
   Compare `.view(np.uint32)` of the result, not `==`, so `-0.0` is checked
   properly. Do the same for float64. Assert `nan` decodes to a `nan`.
2. **Leading zeros dropped.** `binary_str_to_numeric(format(0x3F800000, "b"), "float", 32) == 1.0`.
   VCD writers omit leading zeros, and a positive float's sign bit is
   zero, so every positive float arrives shorter than `wid`.
3. **Unsupported width.** `wid=16` with `dtype="float"` raises `ValueError`.
4. **Through the parser.** Build a tiny VCD with a 32-bit signal carrying
   a known float, and read it with `VcdParser.add_signal(...,
   numeric_type="float")`. Assert `SigInfo.numeric_values` holds the float
   and `disp_values` holds its formatted text. `test_vcd.py` already
   writes temp VCDs for other tests (the `tmp_path` fixtures); follow
   that pattern.

   **Trap:** `SigInfo.__init__` forces `numeric_type = "uint"` whenever
   every sample is the single character `'0'` or `'1'` (the `two_level`
   check). A float bus that carries only `0.0` is written `b0 sig`, which
   vcdvcd hands back as `'0'`, so the signal quietly becomes a `uint` and
   the float path is never exercised. Give the signal at least one
   nonzero float sample (e.g. `0.5`, `-1.25`), and assert
   `si.numeric_type == "float"` so the test can't pass by skipping the
   decode. The default `numeric_fmt_str` for float is `"%.3f"`, so
   `0.5` displays as `"0.500"`.

Test 1 fails on the current code on any little-endian machine, so it
reproduces the bug before the fix goes in.

## Blast radius

A grep of the repo found no caller that sets `numeric_type="float"`, so
nothing in pysilicon depends on the buggy output, and the fix changes no
existing result. The known downstream user is hwdesign's
`demos/stream/avgfilt/avgfilt_timing.py`. Its `_as_float()` decodes TDATA
itself to avoid this bug, and it can switch back to
`numeric_type = "float"` once this ships. That change is in the hwdesign
repo, not here.
