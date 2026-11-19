# Trigger prompts: waveflow-schema

Does the skill load when it should, and stay out when it should not?  Each line is one blind run's
**first message**, with the skill installed and only the stub spec (`../trigger_stub.md`) in the folder:

```bash
waveflow blind-test --prompt evaluation/skill_tests/trigger_stub.md --skills waveflow/skills/waveflow-schema \
    --folder ../waveflow_blind_tests/trig_load_01 --message "<prompt>" --no-approve --timeout 0.1
```

A run only has to get as far as its first few tool calls: the **Skills** section of `summary.md` says
whether `waveflow-schema` was loaded, and at which call.  Score: loaded / not loaded, and the call.

## Should load

1. Using Waveflow, define the command header for a kernel: an opcode (READ, WRITE, RESET), a 16-bit
   transaction id, a 32-bit address and a 12-bit length.
2. I need the Python and C++ for a status message with a uint16 tx_id and an error code -- Waveflow
   project.
3. With Waveflow, how should I pack four signed 10-bit samples into a 64-bit stream word?
4. Write the Waveflow schema for an IQ sample: two signed fixed<16, 1> values.
5. In my Waveflow kernel, read a header off an AXI4-Stream and then 256 float32 samples.
6. Generate the C++ header for my Waveflow DataList.
7. My Waveflow model has to decode words coming back from the RTL into a response message. How?
8. A Waveflow stream carries three kinds of message -- START, DATA, END -- of different lengths. Define
   them.
9. Write test vectors from the Waveflow golden model for the Vitis C testbench.
10. What word layout does Waveflow use for a header with a 16-bit field followed by an array of 4
    floats, at 64 bits?

## Should not load

1. Explain what TLAST means on an AXI4-Stream.
2. Write a Python function that computes a moving average with numpy.
3. Using Waveflow, set the clock of my pysim simulation to 250 MHz.
4. How do I install Vitis HLS 2025.1 on Windows?
5. Using Waveflow, add a register map with a start bit to my kernel.

(3 and 5 are deliberately near misses: Waveflow, but not data types.  A load there is a description
that is too broad, not a failure of the run.)
