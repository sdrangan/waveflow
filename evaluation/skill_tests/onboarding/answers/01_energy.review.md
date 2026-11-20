# Review sheet: onboarding 01, the energy detector

Each item 0 / 1 / 2 (0 = missing or wrong, 1 = partly, 2 = right). 14 items, 28 points. Turns,
tokens and wall time are recorded beside the score, not in it.

Score from the trial folder and `summary.md` in `<folder>.blindtest/`; rerun the agent's tests
yourself (`python -m pytest -q` in the trial folder, with the Waveflow venv).

## A. Getting oriented (the point of the test)

| # | item | 2 = |
|---|---|---|
| A1 | Looked Waveflow up before writing code | the first Waveflow file was written after reading at least the guide's process (a frame's process, `systems/index.md` or a reference example), not from memory |
| A2 | Chose the architecture | named a frame (`stream_kernel` is the fit: one kernel, streams in and out, no host, no memory) or the closest reference example, and said why |
| A3 | Followed the command-response protocol | read `systems/command_response.md` (or the frame's process, which cites it): a response echoing `tx_id`, and a `Status` per command on its own stream |
| A4 | `waveflow-start` (the init arm only; the MCP arm scores this item 2 if it found its way without it) | loaded the skill before the first write, or reached the same lookups otherwise. Record which, and at which tool call |

## B. The design

| # | item | 2 = |
|---|---|---|
| B1 | Messages as schemas | the command, the response and the status are `DataList`s with `IntField`s (and an `EnumField` for the status code), not hand-packed words |
| B2 | The module | an `HwModule` with its pysim behaviour, shaped as the frame / reference example does it (ports, `run_proc` or the frame's equivalent) |
| B3 | The sum of squares never overflows | its field is at least 41 bits unsigned (1024 * 32768^2 = 2^40), and the model computes it in a wide type (numpy int16 squared overflows) |
| B4 | The magnitude of -32768 | `abs` taken in a wide type: `np.abs(np.int16(-32768))` is -32768. The magnitude comparison is right for every 16-bit sample |
| B5 | The error is fatal, per the protocol | `n` = 0 or > 1024: status with an error code, the output closed, and **nothing more taken or answered** (not "skip the command and continue") |

## C. Verification

| # | item | 2 = |
|---|---|---|
| C1 | A golden model apart from the module | a plain-Python (or numpy) reference the test compares against, not the module checked against itself |
| C2 | The test asserts values and counts | every response and status compared with the golden, and the **number** of each asserted, so a hang or a lost message cannot pass |
| C3 | Edge cases in the test | -32768 and 32767 samples, `n` = 1 and 1024, a threshold at a sample's magnitude, and an erroring command **in the middle** with nothing answered after it |
| C4 | It runs | the reviewer's rerun of the agent's tests passes |

## D. The experience

| # | item | 2 = |
|---|---|---|
| D1 | No invented API | no run failed on a Waveflow name or signature the agent guessed (1 = it guessed, failed, then looked it up and fixed it once) |
| D2 | The hand-off | it ends telling the user how to run it, and what comes next for hardware (csynth / the system flow, Vitis 2025.1), and it states anything it did not do |

## Also record (not scored)

- Which `waveflow_*` tools or `waveflow kb` commands it used, in order (from `summary.md`).
- Whether it followed the lab's own `AGENTS.md` conventions (tests in `tests/`, module docstrings):
  the Waveflow section must not crowd them out.
- Turns, tokens in / out, and anything denied.
