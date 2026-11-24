# Trigger prompts: waveflow-shared-kernel

Does the skill load when it should, and stay out when it should not? Each line is one blind run's
**first message**, with **every** shipped skill installed (so `waveflow-pipeline`, its nearest
neighbour, competes for the same prompts) and only the stub spec (`../trigger_stub.md`) in the folder:

```bash
waveflow blind-test --prompt evaluation/skill_tests/trigger_stub.md --skills waveflow/skills \
    --folder ../waveflow_blind_tests/trig_shared_01 --message "<prompt>" --no-approve --timeout 0.1
```

The **Skills** section of `summary.md` says which skills were loaded, and at which call.

## Should load

1. With Waveflow, four sensor channels each need an FFT of every block, but I can afford only one FFT.
   How do I share it?
2. In my Waveflow design a stage's output has to go back through the same matrix engine a second time.
   How do I wire the loop without deadlocking?
3. My Waveflow composite has a mux in front of one kernel, fed by two clients, and it hangs once both
   are busy.
4. Several Waveflow kernels send requests to one accelerator and the answers come back out of order.
   How does each get its own answer?

## Should not load

1. With Waveflow, split my FIR kernel into a decimator stage and a filter stage that run concurrently.
2. Two stages of my Waveflow design both need the same input stream. How do I give each a copy?
3. Using Waveflow, define the message schema for a radar detection report: range bin, Doppler bin,
   power.
4. What is a round-robin arbiter in a NoC router?

(1 and 2 are the pipeline skill's own prompts: near misses with stages but no shared unit and no cycle.)

## Results

2026-11-21, one run each, Claude Code's default model (Opus 5.5): every should-load prompt loaded
`waveflow-shared-kernel` at its first tool call; should-not 1 loaded `waveflow-start` and
`waveflow-pipeline`, 2 `waveflow-pipeline`, 3 `waveflow-schema`, 4 nothing. 8 of 8 right.
