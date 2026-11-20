# Trigger prompts: waveflow-device

Does the skill load when it should, and stay out when it should not? Each line is one blind run's
**first message**, with the skill installed and only the stub spec (`../trigger_stub.md`) in the folder:

```bash
waveflow blind-test --prompt evaluation/skill_tests/trigger_stub.md --skills waveflow/skills/waveflow-device \
    --folder ../waveflow_blind_tests/trig_device_01 --message "<prompt>" --no-approve --timeout 0.1
```

The **Skills** section of `summary.md` says whether `waveflow-device` was loaded, and at which call.

## Should load

1. With Waveflow, my kernel needs to read a table of coefficients from DDR before each job. How?
2. Using Waveflow, put my FFT kernel behind the AXI bus so the ARM can send it frames and read results.
3. My Waveflow design writes its output buffer to memory: how does the host know the data has landed?
4. In Waveflow, how do I give the host a register to set my kernel's threshold, and an interrupt?
5. Two Waveflow kernels on a shared bus: one must write into the other's queue without overrunning it.
6. What goes into a Waveflow device's HLS top, and what is RTL beside it?

## Should not load

1. Using Waveflow, define the message schema for a radar detection report: range bin, Doppler bin,
   power.
2. Write the Vitis HLS body for my Waveflow kernel's sample loop at II=1.
3. Write the pysim model of a Waveflow FreeRunMod that adds two input streams element by element.
4. How does AXI4 burst addressing work?
5. How do I install Vivado 2025.1?

(1 to 3 are near misses: Waveflow, but the schema, HLS-body and kernel skills' ground.)
