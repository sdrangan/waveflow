# Review sheet: device/01_scale_offload

For the reviewer, not the agent (no spec links this file). Fill one sheet per run. First run:

- `python -m pytest <trial folder> -q`: the trial's own tests (G1).
- `python evaluation/skill_tests/lint_schema_use.py <trial folder>`: hand-packing lines (G2).

Run: ____________  arm: [ ] MCP only  [ ] MCP + skill   model: ________  tokens in/out: ________
Skill loaded (from summary.md): [ ] yes, at tool call # ___   [ ] no

Score each item **2** (as the guide does it), **1** (partly), **0** (not done / done otherwise).
The reference is `ref_01/` (its module docstrings state every choice below).

## A. Kernel and adaptors (`systems/kernels.md`, `systems/devices.md`, `interface/axi_mm/master.md`)

| # | item | the pattern | score |
|---|---|---|---|
| A1 | the compute is a **kernel**: a `FreeRunMod` leaf with stream ports only | an `MMIFMaster`, an address read or a bus call in its `run_iter` is 0 | |
| A2 | memory reached through the framework's **`MemRStream` / `MemWStream`**, children of a composite with the kernel (`add_comp`, `add_if`) | a hand-made memory model or a kernel owning `m_axi` is 0 | |
| A3 | the **in-band** protocol (`inband=True`): `MemRCmd` / `MemWCmd`, the write command on the same stream as its data | two-stream (`MRCmd` / `MWCmd`) is 1 | |
| A4 | addresses in **word** coordinates (the reader's and writer's `addr`), the buffer's word count from the schema (`get_nwords` or equivalent), samples packed by the array utils | bytes passed as `addr`, or a word count by hand arithmetic, is 1 | |
| A5 | the composite is called a **device** (not a kernel), and `DESIGN.md` names its parts by the guide's words | | |

## B. Done means stored (`systems/command_response.md`, *Memory is reached the same way*)

| # | item | the pattern | score |
|---|---|---|---|
| B1 | the status reaches the host **through the writer's echo** (`fwd_bursts=1`, `m_done`), so it arrives only once the output is written | a status written by the kernel straight to the host (it can arrive before the data is stored) is 0 | |
| B2 | the writer's forward buffer sized for the status (`max_fwd_words`), `emit_done=True` | | |
| B3 | the error case's status also goes through the writer (`MemWCmd(len=0, fwd_bursts=1)`, or equivalent), then the kernel stops taking commands | an error that skips the job and goes on is 0 | |

## C. The bus side (`interface/axi_mm/slave.md`, `slave_views.md`)

| # | item | the pattern | score |
|---|---|---|---|
| C1 | jobs arrive through a **queue in** view declared in `mm_views` (`QueueIn`) on the device | a register bank per job (one job at a time) is 1 | |
| C2 | the **trap**: the spec asks for a status register *and* an interrupt; the register bank raises none. The run **says so** and gives the host a **queue out** (`QueueOut`) with its `IrqIF` for "done" (a `RegBank` beside it for "latest status" is fine) | a `RegBank` alone with an interrupt invented, or polling, is 0; using a queue out without saying why is 1 | |
| C3 | the host never polls: its endpoints take `irq=` and the interrupt lines are bound (`IrqIF`, source = the view's `m_irq`) | | |
| C4 | the system built by `build_mm_device`, a crossbar (`AXIMMCrossBarIF`) with the host and **both** memory masters on it, `assign_address_ranges`, `BoundMemSlaveAdaptor` | | |
| C5 | the host keeps at most a stated number of jobs outstanding, no more than the status queue holds (or states why it cannot overflow) | | |

## D. Choices stated (`DESIGN.md`)

| # | item | score |
|---|---|---|
| D1 | the status path and why (C2) | |
| D2 | whole words: the last output word is written whole, so samples past `n` in it are overwritten (zero); the host allocates whole words | |
| D3 | the parameters travel in the command (one input stream); no order kept between two streams | |
| D4 | what is not built and would be needed at RTL is named, not worked around: the kernel's HLS body; the host's C++ twin; the slave adaptor's limits in `slave.md` (*What is not built yet*) | |

## E. Tests (`systems/verification.md`)

| # | item | the pattern | score |
|---|---|---|---|
| E1 | the device alone in its **harness** (`harness(Device, workload=...)`, the memory image under a memory port) | a hand-built `Simulation` with a `MemoryMod` is 1 | |
| E2 | the **number** of statuses asserted, and the outputs read back from the memory and compared with the golden | values only, no count, is 1 | |
| E3 | the error placed mid-scenario with jobs after it; the test checks nothing after it is answered | | |
| E4 | the spare lanes of a part-filled last word checked (or the overwrite otherwise tested) | | |
| E5 | the bus system run with the host: every status and every output checked | | |

## G. Over all

| # | item | score |
|---|---|---|
| G1 | the trial's tests pass | |
| G2 | every message by its schema; no hand packing (the lint) | |
| G3 | process: the guide pages (or the skill) read before the first module was written; API names not guessed (count corrections: ___ ) | |

**Total:** ____ / 50.

Free notes -- what it got wrong and which line of the skill (or which missing line) would have
prevented it:
