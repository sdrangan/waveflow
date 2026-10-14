"""Stage 8, the machine-independent half (plans/pipelined_ops.md): SimPy events per memcpy copy, by
type, on the code checked out -- the deterministic half of docs/evaluation/sim_speed's cost model
(pysim time = model time + events x cost per event).  Run from the repo root of each version and diff
the outputs; wall time needs a quiet machine and is measured separately.

    python plans/witness/pipelined_ops/event_counts.py
"""
import json
import sys

from examples.mem_copy.mem_copy_runtime import GRID, count_events

if __name__ == "__main__":
    out = {f"{n}x{b}": count_events(n, b) for n, b in GRID}
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    print()
