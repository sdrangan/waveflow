// vec_mult_tb.cpp — the csim twin check: does the hand-written C++ compute what the Python does?
//
// THE GAP THIS CLOSES.  pysim proves VecMult.run_iter correct; csynth proves vec_mult_task.h
// synthesizes.  Neither proves they compute the SAME thing, and for a design whose whole premise is
// a Python<->C++ twin that is the one claim worth checking.  Every rung can be green while the two
// bodies disagree.
//
// It calls the TASK FUNCTION directly rather than the generated `vec_mult` top.  The top is
// ap_ctrl_none with an hls::task, which never returns -- csim of it would spin forever.  The task
// body is the artifact under test anyway; the top is generated and carries no arithmetic.
//
// The stimulus and the expectation both come from data/, written by the pysim rung.  A testbench
// that computed its own expected output would be checking the C++ against itself.
#include <cstdio>
#include <cstdlib>
#include <vector>
#include "hls_stream.h"
#include <ap_int.h>
#include "vec_mult_params.h"
#include "vec_mult_task.h"

static std::vector<long> read_ints(const char *path) {
    std::vector<long> out;
    FILE *f = std::fopen(path, "r");
    if (!f) {
        std::printf("TB ERROR: cannot open %s\n", path);
        std::exit(2);
    }
    long v;
    while (std::fscanf(f, "%ld", &v) == 1) out.push_back(v);
    std::fclose(f);
    return out;
}

int main() {
    const std::vector<long> meta = read_ints(VM_DATA_DIR "/meta.txt");
    const std::vector<long> x = read_ints(VM_DATA_DIR "/x.txt");
    const std::vector<long> y = read_ints(VM_DATA_DIR "/y.txt");
    const std::vector<long> zexp = read_ints(VM_DATA_DIR "/z_expected.txt");

    const int n = (int)meta[0];
    const long tx_id = meta[1];
    const int LW = vm_au::lane_capacity<VM_DWID>();

    if ((int)x.size() != n || (int)y.size() != n || (int)zexp.size() != n) {
        std::printf("TB ERROR: vector length mismatch (n=%d)\n", n);
        return 2;
    }

    hls::stream<ap_uint<VM_DWID> > s_in, z_out;

    // [ cmd(tx_id, n) | x | y ] — packed by the same generated serializer the DUT reads with.
    VecCmd cmd;
    cmd.tx_id = tx_id;
    cmd.n = n;
    cmd.write_stream<VM_DWID>(s_in);
    for (int pass = 0; pass < 2; ++pass) {
        const std::vector<long> &src = (pass == 0) ? x : y;
        for (int i = 0; i < n; i += LW) {
            const int nlane = (n - i < LW) ? (n - i) : LW;
            ap_int<16> lane[64];
            for (int j = 0; j < nlane; ++j) lane[j] = (ap_int<16>)src[i + j];
            vm_au::write_stream_lane<VM_DWID>(lane, s_in, nlane);
        }
    }

    vec_mult_task<VM_DWID, VM_VLEN>(s_in, z_out);

    int bad = 0;
    for (int i = 0; i < n; i += LW) {
        const int nlane = (n - i < LW) ? (n - i) : LW;
        ap_int<16> lane[64];
        vm_au::read_stream_lane<VM_DWID>(z_out, lane, nlane);
        for (int j = 0; j < nlane; ++j) {
            const long got = (long)lane[j].to_int();
            if (got != zexp[i + j]) {
                if (bad < 10)
                    std::printf("TB MISMATCH at %d: got %ld expected %ld\n",
                                i + j, got, zexp[i + j]);
                ++bad;
            }
        }
    }

    VecResp resp;
    resp.read_stream<VM_DWID>(z_out);
    const long echoed = (long)resp.tx_id;
    if (echoed != tx_id) {
        std::printf("TB MISMATCH: response tx_id %ld != %ld\n", echoed, tx_id);
        ++bad;
    }
    if (!z_out.empty()) {
        std::printf("TB ERROR: %d word(s) left on z_out\n", (int)z_out.size());
        ++bad;
    }

    if (bad) {
        std::printf("WAVEFLOW_CSIM_FAIL: %d mismatch(es) over n=%d\n", bad, n);
        return 1;
    }
    std::printf("WAVEFLOW_CSIM_OK: n=%d LW=%d tx_id=%ld\n", n, LW, tx_id);
    return 0;
}
