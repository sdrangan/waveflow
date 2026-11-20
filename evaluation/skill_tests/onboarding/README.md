# Onboarding test: does a researcher's first request succeed?

The question: a colleague with their own project and their own `AGENTS.md` asks an agent to "try
Waveflow" on a small design. Does the agent get oriented (look Waveflow up, choose the architecture,
follow the protocol) and deliver a design that runs, without guessing the API?

The request (`01_energy.md`) is written as a researcher would write it: no Waveflow vocabulary beyond
the name, nothing about frames, schemas or the protocol. It has three traps no worked example
answers: a sum of squares that needs 41 bits, the magnitude of -32768, and an error that the protocol
says is fatal.

## Arms

Both arms get the MCP server, the request, and the lab's own `AGENTS.md` and `CLAUDE.md`
(`given_01/`, the `CLAUDE.md` imports the `AGENTS.md`, as a lab using several agents would have):

| arm | adds |
|---|---|
| **mcp** | nothing: the MCP server is how the agent finds Waveflow (what the setup page gave before `waveflow init`) |
| **init** | `waveflow init` on the folder (`--init`): the five-line Waveflow section appended to the lab's `AGENTS.md`, and the skills (`waveflow-start`, schema, HLS body) |

Run each on Opus and Sonnet. From the repository root, with the venv active:

```bash
MSG="I'd like to try Waveflow on a small accelerator. What I want is in 01_energy.md; build it in this folder."
G=evaluation/skill_tests/onboarding/given_01

waveflow blind-test --prompt evaluation/skill_tests/onboarding/01_energy.md \
    --folder ../waveflow_blind_tests/onboard01_mcp_opus --model opus --rounds 3 \
    --files $G/AGENTS.md --files $G/CLAUDE.md --message "$MSG"

waveflow blind-test --prompt evaluation/skill_tests/onboarding/01_energy.md \
    --folder ../waveflow_blind_tests/onboard01_init_opus --model opus --rounds 3 \
    --files $G/AGENTS.md --files $G/CLAUDE.md --init --message "$MSG"
```

(and `--model sonnet` into `onboard01_{mcp,init}_sonnet`). The frame's process stops for a review after its first stage (the spec and the golden); `--rounds 3` answers "Approved. Continue." up to three times, so the run reaches the simulation. Score each on
`answers/01_energy.review.md`; record the batch in `../results/onboard01_<date>.md`.

## The cloud case (by hand, in claude.ai/code)

The harness runs locally, with Waveflow installed. The case it cannot run is a cloud session on a
researcher's repository, where Waveflow is **not** installed and there is no MCP server: there, the
`AGENTS.md` section and `waveflow-start` must get the agent to clone and install Waveflow itself.

1. Make a new GitHub repository (private is fine) holding `01_energy.md` and `given_01/AGENTS.md`
   and `given_01/CLAUDE.md` at its root.
2. Locally, in a clone of it: `waveflow init --no-mcp`, then commit everything it wrote.
3. Open the repository in claude.ai/code and send the same message (`MSG` above).
4. Record: whether it loaded `waveflow-start`, whether it cloned and installed Waveflow, how many
   turns before its first Waveflow file, and score the result on the same sheet. Export the session
   (or copy its transcript) into `../results/`.
