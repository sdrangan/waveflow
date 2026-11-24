// g++ check of the hand-written bodies with the framework reader / writer bodies, run in dataflow
// order one command at a time.  Reads csim/in/{cmds,image}.bin, writes csim/out/{mem,done}.bin.
#include <cstdio>
#include <vector>
#include <fstream>
#include "streamutils_hls.h"
#include "vmul_issue_task.h"
#include "vmul_mul_task.h"
#include "mem_r_stream_framed_task.h"
#include "mem_w_stream_framed_done_task.h"
static const int W = 64;
static std::vector<uint64_t> load(const char* p) {
    std::ifstream f(p, std::ios::binary | std::ios::ate); size_t n = f.tellg() / 8; f.seekg(0);
    std::vector<uint64_t> v(n); f.read((char*)v.data(), n * 8); return v;
}
int main() {
    auto cmds = load("in/cmds.bin"), img = load("in/image.bin");
    const int cmd_words = VmulCmd::nwords<W>();
    std::vector<ap_uint<W> > mem(img.begin(), img.end());
    hls::stream<streamutils::axi4s_word<W> > s_cmd;
    hls::stream<streamutils::framed_word<W> > rx, ry, desc, x, y, wr;
    hls::stream<ap_uint<W> > done;
    const int ncmd = cmds.size() / cmd_words;
    for (size_t i = 0; i < cmds.size(); ++i) {
        streamutils::axi4s_word<W> w; w.data = cmds[i]; w.last = ((i + 1) % cmd_words) == 0; s_cmd.write(w);
    }
    for (int c = 0; c < ncmd; ++c) {
        vmul_issue_task<W>(s_cmd, rx, ry, desc);
        if (!rx.empty()) mem_r_stream_framed_task<W>(rx, mem.data(), x);
        if (!ry.empty()) mem_r_stream_framed_task<W>(ry, mem.data(), y);
        if (!desc.empty()) vmul_mul_task<W>(desc, x, y, wr);
        if (!wr.empty()) mem_w_stream_framed_done_task<W, 2>(wr, mem.data(), done);
    }
    int left = s_cmd.size() + rx.size() + ry.size() + desc.size() + x.size() + y.size() + wr.size();
    std::printf("commands %d, done words %zu, words left in internal streams/cmd %d\n", ncmd, done.size(), left);
    std::ofstream fm("out/mem.bin", std::ios::binary), fd("out/done.bin", std::ios::binary);
    for (auto& w : mem) { uint64_t v = w.to_uint64(); fm.write((char*)&v, 8); }
    while (!done.empty()) { uint64_t v = done.read().to_uint64(); fd.write((char*)&v, 8); }
    return 0;
}
