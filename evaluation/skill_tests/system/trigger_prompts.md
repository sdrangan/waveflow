# Trigger prompts: waveflow-system

Does the skill load when it should, and stay out when it should not? Each line is one blind run's
**first message**, with the skill installed and only the stub spec (`../trigger_stub.md`) in the folder:

```bash
waveflow blind-test --prompt evaluation/skill_tests/trigger_stub.md --skills waveflow/skills/waveflow-system \
    --folder ../waveflow_blind_tests/trig_system_01 --message "<prompt>" --no-approve --timeout 0.1
```

The **Skills** section of `summary.md` says whether `waveflow-system` was loaded, and at which call.

## Should load

1. With Waveflow, I have two devices and a DDR memory: how do I put them on one bus with a host and
   simulate the whole thing?
2. Set up a Waveflow build script that runs my kernel's harness and my full system through RTL.
3. My Waveflow system test passes, but I suspect a device hangs halfway. How do I make the tests catch it?
4. What does Waveflow's `compare` step check, and what does it not prove?
5. In Waveflow, which parts of my system become RTL and which are the testbench?
6. Run my Waveflow design's DAG only through the steps that need no Vitis.

## Should not load

1. Using Waveflow, put my FFT kernel behind the AXI bus so the ARM can send it frames.
2. Split my Waveflow kernel into two stages that run concurrently.
3. Write the Vitis HLS body for my Waveflow kernel's sample loop at II=1.
4. How do I write a SystemVerilog testbench with UVM?
5. How do I install Vivado 2025.1?

(1 to 3 are near misses: Waveflow, but the device, pipeline and HLS-body skills' ground.)
