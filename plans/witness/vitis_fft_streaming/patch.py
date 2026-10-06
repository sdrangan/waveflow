import pathlib
p = pathlib.Path("lib/vitis_fft/hls_ssr_fft_streaming_data_commutor.hpp")
s = p.read_text()
old = """    for (int t = 0; t < t_L / t_R + delay_factor * (t_R - 1) * t_PF; t++) {
#pragma HLS PIPELINE II = 1 rewind"""
new = """    // WAVEFLOW PATCH: run until this call has emitted one frame (L/R words), not for a fixed
    // L/R + fill/drain window -- the drain then overlaps the next frame's input.
    int wf_n_out = 0;
    while (wf_n_out < t_L / t_R) {
#pragma HLS PIPELINE II = 1"""
assert s.count(old) == 1
i = s.index(old)
s = s[:i] + new + s[i + len(old):]
end = s.index("#if 0", i)
body = s[i:end]
w = "                p_sampleOut.write(temp_output);\n"
assert body.count(w) == 1
body = body.replace(w, w + "                wf_n_out++;\n")
s = s[:i] + body + s[end:]
p.write_text(s)
print("patched")
