Write and test a Vitis kernel:

## Overview

The kernel performs a 2D rotation on data with a given rotation angle

## Kernel interfaces

- in_stream:  width = WORD_BW
- out_stream: width = WORD_BW

where WORD_BW is a programmable parameter

## Protocol

- Host sends a command header with a transaction id, len, cos_theta and sin_theta.  cos_theta and sin_theta are in Q10.8  (10 bits total including sign, 8 bits fractional)
- Host then sends data interleaved [x[0], y[0], x[1], y[1], ..., x[len-1], y[len-1]].  All data in Q16.8
- Kernel performs the rotation and outputs [x1[0], y1[0], x1[1], y1[1], ..., x1[len-1], y1[len-1]]
- Halts on error and sets a status

## Evaluation
- Create a python model and run on different vectors with different lengths and angles
- Measure error in fixed point model relative to floating point and create an report, say fixp_mse.json
- Create test vectors from the python model
- Create kernel and run C sim, C synthesis, RTL sim and verify bit exact 
- From RTL co-sim extract timing and print VCD diagram.  Measure timing for a two different len values and WORD_BW = 32 and 64 
- Create a report, report.md, with a summary of the evaluation results including the figures.  This summary should be readable by an outside agent without looking at the code
- In report.md, list every assumption you made where this spec was silent
