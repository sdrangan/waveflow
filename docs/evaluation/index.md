---
title: Evaluation
parent: Waveflow
nav_order: 3.5
has_children: true
summary: "An honest assessment of Waveflow, measured rather than claimed: what each potential gain is, how it was tested, the numbers -- including where Waveflow is slower or does less than the alternative -- and what to improve. Starts with simulation speed; agentic design and design-space exploration will follow. Every number names the script that produced it, and the section is expected to change as the measurements do."
---

# Evaluation

Waveflow makes claims: one Python specification for simulation, synthesis and documentation; a
faster design loop than RTL; a platform an agent can design with. This section tests them, one
measurement at a time, and reports what it finds -- including where Waveflow is slower than the
alternative, does less, or has not been measured yet.

Three rules for every page here:

- **Every number has a script.** The page names the command that produced it, and the results file
  is committed beside the script, so a reader can rerun it and a reviewer can check it.
- **The comparison is against what people actually use**, not against a straw man -- and where a
  stronger baseline exists, the page says so.
- **The weaknesses are part of the result.** Each page ends with what is slow, what is not covered,
  and what to do next.

This is a living section: the numbers change as Waveflow does, and each page records when it was
measured and on what machine.

## The evaluations

- [Simulation speed](sim_speed/index.md) -- what a loosely-timed Python simulation costs against an
  untimed model and against RTL, per transaction and per frame.

Planned: agentic design (an agent building a block from the docs), and design-space exploration (how
many design points per hour each fidelity allows).
