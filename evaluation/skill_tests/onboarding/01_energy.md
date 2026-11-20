# An energy detector

I'd like to try Waveflow on a small accelerator before using it in my research.

**What it does.** A streaming kernel takes commands. Each command is a transaction id (16 bits), a
count `n` (16 bits) and a threshold (unsigned, 16 bits), followed by `n` signed 16-bit samples. For
each command it returns the transaction id, the sum of the squares of the `n` samples (exact: it must
never overflow) and how many samples have a magnitude above the threshold.

`n` must be from 1 to 1024. A command outside that range is an error.

**What I want now.** The design working in Python simulation, with a test that shows it is right.
No synthesis yet: I don't have Vitis on this machine. When you are done, tell me how to run it and what
I would do next to take it to hardware.
