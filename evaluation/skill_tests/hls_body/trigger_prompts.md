# Trigger prompts: waveflow-hls-body

Does the skill load when it should, and stay out when it should not? Each line is one blind run's
**first message**, with the skill installed and only the stub spec (`../trigger_stub.md`) in the folder:

```bash
waveflow blind-test --prompt evaluation/skill_tests/trigger_stub.md --skills waveflow/skills/waveflow-hls-body \
    --folder ../waveflow_blind_tests/trig_body_01 --message "<prompt>" --no-approve --timeout 0.1
```

The **Skills** section of `summary.md` says whether `waveflow-hls-body` was loaded, and at which call.

## Should load

1. Write the HLS body of my Waveflow kernel: read a header, scale 256 int16 samples, write them out.
2. My Waveflow kernel's sample loop only reaches II=4. How should I restructure it?
3. In a Waveflow hls::task, how do I keep a running count across commands?
4. Review this Waveflow kernel body for deadlocks: it writes a request on one stream and reads the
   reply on another.
5. With Waveflow, process four 16-bit samples per 64-bit word in my HLS loop.
6. Convert my Waveflow kernel's state machine into the standard loop shape.
7. Write the Vitis HLS C++ for a Waveflow FIR kernel that keeps its filter history between packets.
8. Which pragmas does a Waveflow kernel body need to run at one bus word per clock?

## Should not load

1. What is the difference between ap_fixed and ap_ufixed?
2. Explain what a Vivado timing report's WNS means.
3. Using Waveflow, define the command header for a kernel: an opcode, a tx_id and a length.
4. Using Waveflow, write the Python golden model of a moving-average filter.
5. How do I install Vitis HLS 2025.1 on Linux?

(3 and 4 are near misses: Waveflow, but the schema skill's ground and the Python model, not an HLS
body. 3 should load `waveflow-schema`.)
