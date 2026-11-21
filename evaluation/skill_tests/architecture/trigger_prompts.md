# Trigger prompts: waveflow-architecture

Does the skill load when it should, and stay out when it should not? Each line is one blind run's
**first message**, with the skill installed and only the stub spec (`../trigger_stub.md`) in the folder:

```bash
waveflow blind-test --prompt evaluation/skill_tests/trigger_stub.md --skills waveflow/skills/waveflow-architecture \
    --folder ../waveflow_blind_tests/trig_architecture_01 --message "<prompt>" --no-approve --timeout 0.1
```

The **Skills** section of `summary.md` says whether `waveflow-architecture` was loaded, and at which call.

## Should load

1. I have a paper on least-squares channel estimation for OFDM. Can Waveflow turn it into hardware?
2. With Waveflow, how many FFT blocks would a 4-antenna radar receiver need at 2 GS/s, and would it
   fit on an RFSoC?
3. Make this beamformer an FPGA accelerator with Waveflow: w = R^-1 a, applied to 16 channels.
4. Before I write any Waveflow code: which parts of my polar decoder should be hardware and which
   software?
5. Using Waveflow, plan an accelerator for a Kalman filter tracker; I don't know hardware.

## Should not load

1. With Waveflow, write the HLS body of my FIR kernel's sample loop at II=1.
2. Using Waveflow, define the message schema for a radar detection report.
3. My Waveflow pipeline hangs after the third job; one stage produces nothing.
4. What is the computer architecture of a Cortex-A53?
5. Derive the MMSE estimator for a linear Gaussian model.

(1 to 3 are near misses: Waveflow, but the HLS-body, schema and pipeline skills' ground; 4 and 5 use
the words "architecture" and an algorithm without hardware.)
