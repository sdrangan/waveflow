# Review sheet: module/01_three_blocks

For the reviewer, not the agent (no spec links this file). Fill one sheet per run. First run:

- `python evaluation/skill_tests/module/check_blocks.py <trial folder>`: runs the trial's
  `test_blocks.py`, then lists every module class in `block_*.py` with its ports by type and its
  `mm_views` (items G1, A1, B2, C1); it points, the sheet scores.
- `python evaluation/skill_tests/lint_schema_use.py <trial folder>`: hand-packing lines (G2).

Run: ____________  arm: [ ] MCP only  [ ] MCP + skill   model: ________  tokens in/out: ________
Skill loaded (from summary.md): [ ] yes, at tool call # ___   [ ] no

Score each item **2** (as the guide does it), **1** (partly), **0** (not done / done otherwise).
The reference is `ref_01/` (its `DESIGN.md` states every choice below).

## A. Block A: the protocol the description left open (`systems/command_response.md`)

| # | item | the pattern | score |
|---|---|---|---|
| A1 | `FreeRunMod`, framed stream ports, no bus or memory port | | |
| A2 | `lo`, `g`, `n` travel in a command header **on the same stream as the pixels** | a separate settings stream is 0: it creates an order between two streams that nothing keeps | |
| A3 | the choice is **stated** in `DESIGN.md` with its reason and the alternative | 1 if made but not stated, or stated without the reason | |
| A4 | a response header (`tx_id`, `n`) heads the results; one status per row on its own stream | status mixed into `m_out` is 1 | |
| A5 | errors fatal: `g == 0` and a short pixel burst each reported, then no more input read | an error that skips the row and goes on is 0 | |

## B. Block B: different sources, a host on the bus (`systems/streams.md`, `interface/axi_mm/slave.md`)

| # | item | the pattern | score |
|---|---|---|---|
| B1 | `FreeRunMod`, though the word "host" is all over the description | `HostActivated` / `ap_start` / an s_axilite register map is 0 | |
| B2 | the logic reads and writes only streams: samples, settings, reports, status each a stream port | an `MMIFMaster` / register / address in the module is 0 | |
| B3 | samples and settings on **two** streams (different sources) | | |
| B4 | settings from a **register bank** view (`RegBank`, `cfg_port`), declared in `mm_views` (or named in `DESIGN.md` as the adaptor) | | |
| B5 | settings checked at a **stated point**, before a frame, without blocking (`get_schema_nb`); the first frame waits for the first setting | a blocking read every frame is 0 (the monitor stalls until the host writes again); a check inside the frame is 0 (a mix) | |
| B6 | reports through a **queue out**, the host woken by its `IrqIF`, no polling anywhere | | |
| B7 | frames processed and last fault: a status stream to the register bank's status side | | |
| B8 | the interrupt-enable / clear-on-write registers: **said not built**, the level interrupt used instead | imitating them with a hand-made register map, or silently dropping the request, is 0 | |
| B9 | a short frame is fatal: reported in the status, then no more frames processed | | |

## C. Block C: two streams the user asked for (`systems/command_response.md`, `mm_fir`)

| # | item | the pattern | score |
|---|---|---|---|
| C1 | templates and jobs on two separate input streams, as specified | merged into one stream is 0 | |
| C2 | the order between them carried by an **id**: a template carries one, a job names it, the block waits for it | 1 for a count kept at both ends ("the k-th job after the k-th template"); 0 for none (a job uses whatever template arrived last) | |
| C3 | the response echoes the template id used | | |
| C4 | a template sent while earlier jobs wait is **not** applied to them (a test shows it) | | |
| C5 | status per job; `n < L` and a short burst fatal | | |

## D. Tests (`systems/verification.md`)

| # | item | the pattern | score |
|---|---|---|---|
| D1 | each block run in simulation (its harness, or a `Simulation` with sources and sinks) on a scenario | | |
| D2 | the **number** of bursts on every output asserted, not only the values of those received | 1 if only some outputs are counted | |
| D3 | each error case placed mid-scenario, with input after it, and the test checks nothing after it is answered | | |
| D4 | block B's settings change mid-run in a test, and the test checks a frame boundary | | |

## G. Over all

| # | item | score |
|---|---|---|
| G1 | `test_blocks.py` passes (`check_blocks.py`) | |
| G2 | every message by its schema; no hand packing (the lint) | |
| G3 | process: the guide pages (or the skill) read before the first module was written; API names not guessed (count corrections: ___ ) | |

**Total:** ____ / 52.

Free notes -- what it got wrong and which line of the skill (or which missing line) would have
prevented it:
