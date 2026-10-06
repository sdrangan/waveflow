"""Patch 2: flatten fftStageKernelS2S's sub-FFT x butterfly loop nest into one II=1 loop.

The original nest re-enters the inner pipeline once per sub-FFT (64 times per frame in the third
stage at L=1024, 4 butterflies each), paying the pipeline fill every time; its `k--` on an empty
FIFO also blocks loop flattening.  One flat loop with a blocking read does the same work in order.
"""
import pathlib

p = pathlib.Path(__file__).parent / "lib/vitis_fft/hls_ssr_fft.hpp"
s = p.read_text()
start = s.index("void fftStageKernelS2S(")
i = s.index("L_FFTs_LOOP:", start)
j = s.index("    }     // sub ffts loop\n}", i) + len("    }     // sub ffts loop\n")
old = s[i:j]
assert "p_fftOutData_local.write(temp_super_sample_out);" in old

new = """    // WAVEFLOW PATCH: one flat II=1 loop over the frame's L/R words (was sub-FFT x butterfly nest).
L_FLAT_BFLYs_LOOP:
    for (int i = 0; i < no_of_ffts_in_stage * no_bflys_per_fft; i++) {
#pragma HLS PIPELINE II = 1
        const int k = i & (no_bflys_per_fft - 1);
        T_in X_of_ns[t_R];
        T_complexMulOutType complexExpMulOut[t_R];
        T_out bflyOutData[t_R];
        SuperSampleContainer<t_R, T_in> temp_super_sample_in = p_fftReOrderedInput.read();
        for (int n = 0; n < t_R; n++) {
#pragma HLS UNROLL
            X_of_ns[n] = temp_super_sample_in.superSample[n];
        }
        Butterfly<t_R> Butterfly_obj;
        Butterfly_obj.template calcButterFly<t_L, isFirstStage, t_scalingMode, transform_direction, butterfly_rnd_mode>(
            X_of_ns, bflyOutData, p_complexExpTable);
        twiddleFactorMulS2S<t_L, t_R, transform_direction, butterfly_rnd_mode>(
            bflyOutData, complexExpMulOut, p_twiddleTable, (k << (ssrFFTLog2<t_L / current_fft_length>::val)));
        SuperSampleContainer<t_R, T_out> temp_super_sample_out;
        for (int r = 0; r < t_R; r++) {
#pragma HLS UNROLL
            temp_super_sample_out.superSample[r] = complexExpMulOut[r];
        }
        p_fftOutData_local.write(temp_super_sample_out);
    }
"""
s = s[:i] + new + s[j:]
p.write_text(s)
print("patched fftStageKernelS2S")
