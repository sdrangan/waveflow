# Onboarding test: does an agent on a researcher's project find Waveflow?

**The question is only whether the agent gets oriented**, not how good the design is (the skill tests
measure that, skill by skill). A colleague with their own project and their own `AGENTS.md` asks an
agent to try Waveflow on a small design. Does it load `waveflow-start` and look Waveflow up (MCP tools
or `waveflow kb`) before writing Waveflow code, instead of guessing the API? In a cloud session, does
it install Waveflow by itself?

The first stage of a real task shows all of that, so a run stops there: the frame's process asks for a
review after its first stage, and `--no-approve` ends the run at that point (`--timeout 0.25`, 15
minutes, is a cap in case it does not stop).

The request (`01_energy.md`) is written as a researcher would write it, naming Waveflow but none of
its concepts.

## Arms

Both arms get the MCP server, the request, and the lab's own `AGENTS.md` and `CLAUDE.md`
(`given_01/`; the `CLAUDE.md` imports the `AGENTS.md`, as a lab using several agents would have):

| arm | adds |
|---|---|
| **mcp** | nothing: the MCP server alone (what the setup page gave before `waveflow init`) |
| **init** | `waveflow init` on the folder (`--init`): the five-line Waveflow section appended to the lab's `AGENTS.md`, and the skills |

Run each on Opus and Sonnet. From the repository root, with the venv active:

```bash
MSG="I'd like to try Waveflow on a small accelerator. What I want is in 01_energy.md; build it in this folder."
G=evaluation/skill_tests/onboarding/given_01

waveflow blind-test --prompt evaluation/skill_tests/onboarding/01_energy.md \
    --folder ../waveflow_blind_tests/onboard01_mcp_opus --model opus --no-approve --timeout 0.25 \
    --files $G/AGENTS.md --files $G/CLAUDE.md --message "$MSG"

waveflow blind-test --prompt evaluation/skill_tests/onboarding/01_energy.md \
    --folder ../waveflow_blind_tests/onboard01_init_opus --model opus --no-approve --timeout 0.25 \
    --files $G/AGENTS.md --files $G/CLAUDE.md --init --message "$MSG"
```

(and `--model sonnet` into `onboard01_{mcp,init}_sonnet`).

## What to record

All of it is in `summary.md` (`<folder>.blindtest/`), sections *Skills*, *Choice* and *Waveflow tools*:

| | |
|---|---|
| skill | `waveflow-start` loaded? at which tool call; before or after the first write |
| lookups | the first Waveflow lookup's tool call, against the first write |
| guessing | a run that failed on a Waveflow name the agent made up before looking it up |
| the lab's rules | still followed (tests in `tests/`)? The Waveflow section must not crowd them out |
| cost | turns and tokens to the first stop |

Record the batch in `../results/onboard01_<date>.md`.

## The cloud case (by hand, in claude.ai/code)

The harness runs locally, with Waveflow installed. The case it cannot run is a cloud session on a
researcher's repository, where Waveflow is **not** installed and there is no MCP server: there, the
`AGENTS.md` section and `waveflow-start` must get the agent to clone and install Waveflow itself.

1. Make a new GitHub repository (private is fine) holding `01_energy.md`, `given_01/AGENTS.md` and
   `given_01/CLAUDE.md` at its root.
2. Locally, in a clone of it: `waveflow init --no-mcp`, then commit everything it wrote.
3. Open the repository in claude.ai/code and send the same message (`MSG` above).
4. Record the same table, plus: did it clone and install Waveflow, and did `waveflow kb` work after.
