"""costs.py -- what each evaluation of the LTBF design costs on the lab machine, as DSE.md quotes it.

The median tool time of each evaluation over plan step 2c's six points (``points.py``), Vitis HLS /
Vivado 2025.1 on the lab machine (i7-7700K, two heavy jobs at once), written by ``points.py
--costs``; ``None`` until measured.  Python rather than JSON: the repository ignores ``*.json``.
The ground truth (plan step 4) records every evaluation's own cost; these are what an agent is told
to expect.
"""

#: Tool-seconds: ``golden`` and ``pysim`` per call, ``hls_estimate`` per kernel, ``synth`` per part,
#: ``rtlsim_build`` for a design whose RTL is new, ``rtlsim_run`` for a new run on elaborated RTL.
COSTS: dict[str, float | None] = {
    "golden": 215.8,
    "pysim": 1.1,
    "hls_estimate": 55.7,
    "synth": 346.2,
    "rtlsim_build": 902.4,
    "rtlsim_run": 744.2,
}
#: Where they were measured.
MEASURED = "2026-11-07, Vitis HLS / Vivado 2025.1, the lab machine"
