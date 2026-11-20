# An energy detector

I'd like to try Waveflow on a small accelerator before using it in my research.

A streaming kernel takes commands. Each command is a transaction id, a count `n` and a threshold,
followed by `n` signed 16-bit samples. For each command it returns the transaction id, the sum of
the squares of the samples, and how many samples have a magnitude above the threshold.

Get it working in Python simulation with a test. No synthesis yet: I don't have Vitis on this
machine.
