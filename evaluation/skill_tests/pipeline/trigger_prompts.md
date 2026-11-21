# Trigger prompts: waveflow-pipeline

Does the skill load when it should, and stay out when it should not? Each line is one blind run's
**first message**, with the skill installed and only the stub spec (`../trigger_stub.md`) in the folder:

```bash
waveflow blind-test --prompt evaluation/skill_tests/trigger_stub.md --skills waveflow/skills/waveflow-pipeline \
    --folder ../waveflow_blind_tests/trig_pipeline_01 --message "<prompt>" --no-approve --timeout 0.1
```

The **Skills** section of `summary.md` says whether `waveflow-pipeline` was loaded, and at which call.

## Should load

1. With Waveflow, split my FIR kernel into a decimator stage and a filter stage that run concurrently.
2. Two stages of my Waveflow design both need the same input stream. How do I give each a copy?
3. My Waveflow composite hangs after the third job; one stage produces nothing for some commands.
4. How deep should the internal streams between my Waveflow stages be?
5. In a Waveflow pipeline, which stage should send the status for a command?

## Should not load

1. Using Waveflow, define the message schema for a radar detection report: range bin, Doppler bin,
   power.
2. Write the Vitis HLS body for my Waveflow kernel's sample loop at II=1.
3. With Waveflow, my kernel reads its input buffer from DDR. How?
4. What is a pipeline hazard in a CPU?
5. How do I install Vivado 2025.1?

(1 to 3 are near misses: Waveflow, but the schema, HLS-body and device skills' ground.)
