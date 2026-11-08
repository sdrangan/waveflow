---
title: Retired Components
parent: DSP Blocks
nav_order: 9
has_children: true
audience: python
api: [CgVectorUnit, CgVectorCore, SystolicUnit, SystolicCore]
summary: "Retired linear-algebra units, kept only for the study that used them (examples/mimo_cg): SystolicUnit and SystolicCore, superseded by SystolicEngine and SystolicEngineMem, and CgVectorUnit and CgVectorCore, superseded by CG as a program on the programmable VecUnit. Their code is in waveflow/linalg/legacy/, their task bodies in waveflow/build/legacy/, their tests in tests/linalg/legacy/. New designs use the engine and the vector unit."
---

# Retired Components

These units are **retired**: kept, unchanged in behaviour, for the study that used them
([the MIMO-CG example](../../../examples/mimo_cg/index.md), a 2024.1 study), and not for new designs.

| Retired | Use instead | Code |
|---|---|---|
| `SystolicUnit`, `SystolicCore` | [`SystolicEngine`](../systolic/systolic_engine.md), [`SystolicEngineMem`](../systolic/systolic_engine_mem.md) | `waveflow/linalg/legacy/systolic.py`, cost model `systolic_cost.py` |
| `CgVectorUnit`, `CgVectorCore` ([page](cg_vector.md)) | CG as a program on [`VecUnit`](../vecunit/index.md) ([the CG example](../../../examples/vecunit_cg/index.md)) | `waveflow/linalg/legacy/cg_vector.py`, cost model `cg_cost.py` |

Their HLS task bodies are in `waveflow/build/legacy/` and their tests in `tests/linalg/legacy/` (most of
them quarantined with the example, `tests/conftest.py`). The packaged platform still holds their 2025.1
models, which describe the units before the 2026-10 reply-FIFO fix (`plans/calib_2025_1.md`, Stage 4).
What the two generations share is not retired: the bit-exact model and `MatmulOp`
(`waveflow.linalg.matmul`), the array's traits (`waveflow.linalg.formats`), the platform
(`waveflow.linalg.cost`) and the CG algorithm (`waveflow.linalg.cg`).
