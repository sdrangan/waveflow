"""robustness.py — a harness swept over hostile stand-in policies (``plans/unit_harness.md`` Stage 5).

Each policy (:data:`~waveflow.simulation.policy.POLICIES`) that changes anything about a harness's
stand-ins adds that harness again, under the policy, as a system of its own -- prefix
``<prefix><policy>_``, the same top, so codegen and csynth are shared.  Its ``compare`` is the check:
pysim (the functional reference) against the RTL under hostile stand-ins.  A hang shows as a recording
cut short, or a host that never finished; a protocol slip as different words.  Either is a finding.

    for policy, pfx in add_robustness_steps(dag, lambda p: harness(PolyKernel, policy=p),
                                            work_dir=work_dir, top=TOP, prefix="rob_"):
        ...                                   # pfx + "compare" is the step that judges it
"""
from __future__ import annotations

from waveflow.build.build import BuildDag


def add_robustness_steps(dag: BuildDag, make, *, work_dir, top: str, prefix: str,
                         policies=None, **kw) -> list:
    """Add *make(policy)* -- a harness under each policy of *policies* (default: the Stage 5 sweep)
    that touches its stand-ins -- to *dag*, through ``compare``.  Returns ``[(policy, step prefix)]``.
    *kw* goes to :func:`~waveflow.build.system_dag.add_system_steps`."""
    from waveflow.build.system_dag import add_system_steps
    from waveflow.simulation.policy import POLICIES

    out = []
    for policy in (POLICIES if policies is None else policies):
        sysm = make(policy)
        if not policy.touches(getattr(sysm, "stand_ins", {}).values()):
            continue
        pfx = f"{prefix}{policy.name}_"
        add_system_steps(dag, sysm, work_dir=work_dir, top=top, prefix=pfx, **kw)
        out.append((policy, pfx))
    return out


__all__ = ["add_robustness_steps"]
