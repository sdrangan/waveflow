"""integration.py -- the system-level integration term of pysim's resources (``plans/dse_cg.md`` step 4).

The units' resource models -- the systolic engine's tasks, the vector unit -- were fitted to csynth's
estimate (plan step 1e), and the parts' glue (a device's bus front, its views, the vector wrapper's
decoder, program readers, memory streams and writer), the Resp hub and the crossbar have no model.
What G1 measures is Vivado's out-of-context synthesis of each built part.  This term maps, per part,
the units' estimate (``LtbfCgMemAdaptor.estimate``'s raw counters) to Vivado's numbers, fitted on the
six points of step 2c and nowhere else -- the points ``g1_prereg.INTEGRATION_POINTS`` pre-registers
and scoring excludes.

Its forms, one per part and resource, chosen for what the part is:

* ``sys`` (the systolic engine and its views): LUT and FF in proportion to the estimate (Vivado packs
  the engine's logic into about 55% of csynth's LUTs and 72% of its FFs at every shape measured); DSP
  as estimated (csynth's count, which Vivado keeps within 0-3); BRAM a constant -- Vivado maps the
  engine's blocks to LUTRAM, leaving the two BRAM18 of its ``m_axi`` adapters whatever the shape;
* ``vec`` (the vector unit and its wrapper): DSP twice the unit model's less one (the device carries
  the unit's multipliers twice; exact at both points), LUT in proportion, FF and BRAM on the line
  through the two points;
* ``hub``: its synthesis, a constant (one configuration in the grid);
* ``xbar``: its synthesis at each ``banks`` (both measured among the six).

:func:`fit` recomputes :data:`TERM` from the points' records (``points.py``'s
``BUILD_DIR/points/*.json``, local) and the adaptor's raw estimate; the committed values are its output.
"""

from __future__ import annotations

#: The fitted term (``fit()``'s output).  ``{part: {resource: {"form", ...}}}``.
TERM: dict = {
    "sys": {
        "lut": {"form": "scale", "a": 0.5547},
        "ff": {"form": "scale", "a": 0.7257},
        "dsp": {"form": "scale", "a": 1.0},
        "bram": {"form": "const", "c": 2},
    },
    "vec": {
        "lut": {"form": "scale", "a": 0.7789},
        "ff": {"form": "line", "a": 0.36625, "b": 24885.46},
        "dsp": {"form": "line", "a": 2.0, "b": -1.0},
        "bram": {"form": "line", "a": 0.04545, "b": 49.27},
    },
    "hub": {"lut": {"form": "const", "c": 591}, "ff": {"form": "const", "c": 732},
            "dsp": {"form": "const", "c": 0}, "bram": {"form": "const", "c": 0}},
    "xbar": {"by_banks": {1: {"lut": 6055, "ff": 1526, "dsp": 0, "bram": 0},
                          2: {"lut": 6752, "ff": 1699, "dsp": 0, "bram": 0}}},
}

RESOURCES = ("lut", "ff", "dsp", "bram")


def apply(part: str, raw: dict, params: dict) -> dict:
    """Part *part*'s resources, Vivado's as the term predicts them from the units' *raw* counters at
    the point *params* (filled)."""
    t = TERM[part]
    if "by_banks" in t:
        return dict(t["by_banks"][int(params["banks"])])
    out = {}
    for r in RESOURCES:
        f = t[r]
        x = float(raw.get(r, 0))
        v = {"scale": lambda f=f, x=x: f["a"] * x, "line": lambda f=f, x=x: f["a"] * x + f["b"],
             "const": lambda f=f: f["c"]}[f["form"]]()
        out[r] = round(v)
    return out


def fit() -> dict:
    """:data:`TERM` from the six points' synthesized parts and the adaptor's raw estimate there."""
    import json

    import numpy as np

    from examples.dse_cg.g1_prereg import INTEGRATION_POINTS
    from examples.dse_cg.ltbf_cg_mem import BUILD_DIR, LtbfCgMemAdaptor
    from examples.dse_cg.points import POINTS

    a = LtbfCgMemAdaptor()
    rows: dict[str, dict] = {}
    for name in INTEGRATION_POINTS:
        params = POINTS[name][0]
        calls = json.loads((BUILD_DIR / "points" / f"{name}.json").read_text())["calls"]
        est, ids, p = a.estimate(params), a.identities(params), a.fill(params)
        for node in ("sys", "vec", "hub", "xbar"):
            rows.setdefault(node, {})[ids[node]] = (est[node]["counters"], calls[f"synth.{node}"], p)

    def scale(xs, ys):
        xs, ys = np.asarray(xs, float), np.asarray(ys, float)
        return {"form": "scale", "a": round(float(xs @ ys / (xs @ xs)), 4)}

    def line(xs, ys):
        a_, b_ = np.polyfit(np.asarray(xs, float), np.asarray(ys, float), 1)
        return {"form": "line", "a": round(float(a_), 5), "b": round(float(b_), 2)}

    def xy(node, r):
        return [v[0][r] for v in rows[node].values()], [v[1][r] for v in rows[node].values()]

    sys_bram = sorted({v[1]["bram"] for v in rows["sys"].values()})
    hub = next(iter(rows["hub"].values()))[1]
    return {
        "sys": {"lut": scale(*xy("sys", "lut")), "ff": scale(*xy("sys", "ff")),
                "dsp": {"form": "scale", "a": 1.0},
                "bram": {"form": "const", "c": sys_bram[0] if len(sys_bram) == 1 else sys_bram}},
        "vec": {"lut": scale(*xy("vec", "lut")), "ff": line(*xy("vec", "ff")),
                "dsp": line(*xy("vec", "dsp")), "bram": line(*xy("vec", "bram"))},
        "hub": {r: {"form": "const", "c": int(hub[r])} for r in RESOURCES},
        "xbar": {"by_banks": {int(v[2]["banks"]): {r: int(v[1][r]) for r in RESOURCES}
                              for v in rows["xbar"].values()}},
    }


if __name__ == "__main__":
    import pprint

    pprint.pprint(fit(), width=110)
