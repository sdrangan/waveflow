# blocks/dist.md -- `dist` and `coll`: routing PRBs to the four engines and back

- **Built: no library block routes** a PRB's samples to the engine that owns its filter.
- **`dist`** (a leaf kernel): reads the A53's `SlotCmd` per filter group (through a `QueueIn` view,
  `systems/devices.md`), the group's `W` from the solve, and the group's `Y` from the FFT; writes the
  `EngineCmd`, `W` and the 7 `Y` blocks to engine `prb mod 4`. It forwards the command's `tx_id`.
- **`coll`** (a leaf kernel): reads the four engines' outputs in PRB order (engine `j`, then `j + 1`),
  drops padded rows when `U` is not a multiple of 4, writes `x_hat` per PRB to the demapper and a
  `Status` per filter group to the A53 (`QueueOut`).
- **Rate**: `dist` 2.94 words a cycle in total: **over one stream's word a cycle** -- it takes `Y` as
  four streams (one per engine) or the design splits it per engine (open question 1 of `arch.md`).
  `coll` 0.73 words a cycle.
- **II**: a word a cycle, II = 1 [lane loop]. **Resources**: unknown -- csynth probe (no arithmetic,
  expected small).
- **Frame**: with the engines and the solve, `stream_pipeline` (stages joined by internal streams, no
  memory); the commands from the A53 make the whole a device on the bus: `bus_system`.
