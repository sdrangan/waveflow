# Trigger prompts: waveflow-kernel

Does the skill load when it should, and stay out when it should not? Each line is one blind run's
**first message**, with the skill installed and only the stub spec (`../trigger_stub.md`) in the folder:

```bash
waveflow blind-test --prompt evaluation/skill_tests/trigger_stub.md --skills waveflow/skills/waveflow-kernel \
    --folder ../waveflow_blind_tests/trig_kernel_01 --message "<prompt>" --no-approve --timeout 0.1
```

The **Skills** section of `summary.md` says whether `waveflow-kernel` was loaded, and at which call.

## Should load

1. With Waveflow, write a block that takes audio samples from another block and sends the host a peak
   level every 1024 samples.
2. My Waveflow module needs a setting the CPU can change while it runs. How do I add it?
3. Write the pysim model of a Waveflow FreeRunMod that adds two input streams element by element.
4. Should my Waveflow kernel's coefficients travel on the same stream as its data?
5. In Waveflow, how does a kernel tell the host that results are ready?
6. Review the ports of my Waveflow HwModule: it has an m_axi master and a register map in run_iter.
7. Using Waveflow, my block reads a lookup table from DDR and streams results to a DAC. Declare it.
8. Write a test for my Waveflow module that runs it in its harness.

## Should not load

1. Using Waveflow, define the message schema for a radar detection report: range bin, Doppler bin,
   power.
2. Write the Vitis HLS body for my Waveflow kernel's sample loop at II=1.
3. What is an AXI4-Stream TLAST?
4. How do I install Vivado 2025.1?
5. Explain the difference between a Moore and a Mealy state machine.

(1 and 2 are near misses: Waveflow, but the schema and HLS-body skills' ground. They should load
`waveflow-schema` and `waveflow-kernel-hls`.)
