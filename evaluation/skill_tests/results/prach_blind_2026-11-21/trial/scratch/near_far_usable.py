"""Near-far inside the usable zone (decision 17: delays up to the N_CS zone less 1 us), float, with
the bit-exact model on the 15 dB point.  Same model as scratch/robust.py.

    python scratch/near_far_usable.py      -> results/near_far_usable.json
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import robust as rb  # noqa: E402


def main():
    rng = np.random.default_rng(31)
    res = {}
    for gap in (0, 10, 13, 15, 18, 20):
        res[gap] = rb.run(rb.near_far(gap, guard_s=1.0e-6), rng)
        print("near-far", gap, res[gap], flush=True)
    res["bit_exact_15dB"] = rb.run(rb.near_far(15, guard_s=1.0e-6), rng, n=100, fixed=True)
    print("bit-exact 15", res["bit_exact_15dB"], flush=True)
    Path("results/near_far_usable.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
