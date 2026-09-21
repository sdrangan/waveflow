"""Frozen-grid reference and budget-matched search baselines, with no model dependency.

Run deliberately: python -m examples.dse_fir.benchmark --root /new/oracle --output oracle.json
This reference is for the scorer, never an artifact exposed to the benchmark agent.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from examples.fir_block.fir_block_corpus import GRID

from .contracts import Candidate, Experiment, canonical, identity
from .service import DseService


def grid():
    return [Candidate(ntap=n, samp_w=w, unroll_lane=u).model_dump() for n, w, u in sorted(GRID)]


def best(rows):
    feasible = [r for r in rows if r["feasible"] is True]
    return max(feasible, key=lambda r: r["quality"]["stopband_rej_db"], default=None)


def score(rows, optimum):
    selected = best(rows)
    optimum_quality = optimum["quality"]["stopband_rej_db"] if optimum else None
    value = selected["quality"]["stopband_rej_db"] if selected else None
    return {"best_candidate_id": selected["candidate_id"] if selected else None,
            "best_params": selected["params"] if selected else None, "quality_db": value,
            "regret_db": optimum_quality - value if value is not None and optimum_quality is not None else None,
            "optimum_found": value is not None and optimum_quality is not None and abs(value - optimum_quality) < 1e-9}


def build_reference(root, config=None, synth_budget=8, random_trials=1000):
    if not 1 <= synth_budget <= len(GRID) or random_trials < 1:
        raise ValueError("require 1 <= synth_budget <= grid size and random_trials >= 1")
    experiment = Experiment.model_validate(config or {}).model_dump()
    experiment["budget"] = {"pysim": len(GRID), "predict_resource": len(GRID),
                            "synth": len(GRID), "rtlsim": 0}
    service = DseService(root, experiment)
    for point in grid():
        service.invoke("dse_pysim", {"params": point})
        service.invoke("dse_predict_resource", {"params": point})
    service.invoke("dse_synth", {"params": grid()})
    results = service.results()
    failures = [r for r in results["rows"] if r["status"] != "ok"]
    if failures or results["candidate_count"] != len(GRID):
        raise RuntimeError(f"reference incomplete: {len(failures)} failed observations, {results['candidate_count']} candidates")
    rows = results["candidates"]
    optimum = best(rows)
    if optimum is None:
        raise RuntimeError("no feasible grid point under declared constraints")
    random_scores = [score(random.Random(seed).sample(rows, synth_budget), optimum)
                     for seed in range(random_trials)]
    constraints = service.config.constraints
    # A cheap-screening baseline prevents attributing trivial surrogate ranking to agent intelligence.
    screened = [r for r in rows if r["quality"]["passband_sndr_db"] >= constraints.min_passband_sndr_db
                and r["quality"]["throughput_samp_per_cyc"] >= constraints.min_throughput
                and r["predictions"]["top_dsp"]["est"] <= constraints.max_top_dsp
                and r["predictions"]["top_lut"]["est"] <= constraints.max_top_lut]
    screened.sort(key=lambda r: r["quality"]["stopband_rej_db"], reverse=True)
    observed = [s["regret_db"] for s in random_scores if s["regret_db"] is not None]
    return {"schema_version": "fir-benchmark-v1", "experiment": service.config.model_dump(),
            "semantics": service.store.manifest["semantics"], "grid_size": len(rows),
            "feasible_count": sum(r["feasible"] is True for r in rows), "optimum": score(rows, optimum),
            "oracle_rows": rows,
            "baselines": {
                "full_grid": {**score(rows, optimum), "pysim_queries": len(rows), "synth_queries": len(rows)},
                "grid_prefix": {**score(rows[:synth_budget], optimum), "pysim_queries": synth_budget,
                                "synth_queries": synth_budget, "ordering": "ntap,samp_w,unroll_lane ascending"},
                "random": {"trials": random_trials, "seeds": [0, random_trials - 1],
                           "sampling": "uniform without replacement; equal expensive-query budget",
                           "pysim_queries": synth_budget, "synth_queries": synth_budget,
                           "optimum_found_rate": sum(s["optimum_found"] for s in random_scores) / random_trials,
                           "feasible_found_rate": len(observed) / random_trials,
                           "mean_regret_db_when_feasible": sum(observed) / len(observed) if observed else None},
                "cheap_screened": {**score(screened[:synth_budget], optimum),
                                   "pysim_queries": len(rows), "prediction_queries": len(rows),
                                   "synth_queries": min(synth_budget, len(screened)),
                                   "rule": "quality/throughput/predicted-resource filter, quality descending"}},
            "limitations": ["Fixed public 24-point replay grid; not general DSE or new hardware validation.",
                            "Full grid is a reference, not an equal-budget competitor.",
                            "Random baseline matches expensive queries, not reasoning/cheap evaluation effort.",
                            "The surrogate is fitted on this corpus; scores do not validate generalization."]}


def score_experiment(reference, root):
    api = DseService(root)
    # Budgets legitimately differ between oracle and trial; the scientific protocol may not.
    actual, expected = api.config.model_dump(), reference["experiment"]
    for key in ("evaluation", "constraints", "objective", "platform"):
        if actual[key] != expected[key]:
            raise ValueError(f"incomparable benchmark {key}")
    if api.store.manifest["semantics"] != reference["semantics"]:
        raise ValueError("incomparable source/backend semantics")
    allowed = {identity(p) for p in grid()}
    rows = [r for r in api.results()["candidates"] if r["candidate_id"] in allowed]
    optimum = best(reference["oracle_rows"])
    return {**score(rows, optimum), "budget": api.context()["budget"],
            "experiment_id": api.store.manifest["experiment_id"],
            "scope": "best evidenced candidate found, not necessarily agent final selection"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config")
    parser.add_argument("--synth-budget", type=int, default=8)
    parser.add_argument("--random-trials", type=int, default=1000)
    parser.add_argument("--reference", help="score existing root against reference instead of building oracle")
    args = parser.parse_args()
    if args.reference:
        result = score_experiment(json.loads(Path(args.reference).read_text()), args.root)
    else:
        config = json.loads(Path(args.config).read_text()) if args.config else None
        result = build_reference(args.root, config, args.synth_budget, args.random_trials)
    Path(args.output).write_text(canonical(result) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("oracle_rows", "semantics")}, indent=2))


if __name__ == "__main__":
    main()
