// w5.cpp -- witness 5: the scalar issue loop and the two-task form (plans/simdcore_records.md D2).
//
// scalar_task: an RV32I(+M) subset on the real encodings (lui, op-imm, op, branches, jal, ecall) and
// the OP-V instructions it forwards: vsetvli (it computes vl, e32, VLMAX 8), and every other OP-V
// word goes to the vector task with rs1's value and vl.  vmv.x.s blocks on the return stream, read only
// after the producing instruction was pushed (reference-hls-request-response-deadlock).
// vector_task: a toy vector unit -- vadd.vv, vmv.v.x, vredsum.vs, vmv.x.s at e32 -- one instruction an
// invocation; the register file is static state.
#include <ap_int.h>
#include <hls_stream.h>
#include <hls_task.h>

typedef ap_uint<32> u32;
typedef ap_uint<72> vq_t;  // ins[31:0] xs[63:32] vl[71:64]
#define IMEM 256
#define VLMAX 8

void scalar_task(hls::stream<u32> &prog, hls::stream<vq_t> &vq, hls::stream<u32> &ret,
                 hls::stream<u32> &res) {
    u32 imem[IMEM];
    u32 regs[32];
    int n = prog.read();
LOAD:
    for (int i = 0; i < n; i++) {
#pragma HLS pipeline II = 1
#pragma HLS loop_tripcount max = IMEM
        imem[i] = prog.read();
    }
INIT:
    for (int r = 0; r < 32; r++) regs[r] = 0;
    u32 pc = 0;
    ap_uint<8> vl = 0;
    bool done = false;
RUN:
    while (!done) {
#ifdef SC_SII2
#pragma HLS pipeline II = 2
#else
#pragma HLS pipeline II = 1
#endif
        u32 ins = imem[pc.range(9, 2)];
        ap_uint<7> opc = ins.range(6, 0);
        ap_uint<5> rd = ins.range(11, 7), rs1 = ins.range(19, 15), rs2 = ins.range(24, 20);
        ap_uint<3> f3 = ins.range(14, 12);
        ap_uint<7> f7 = ins.range(31, 25);
        ap_int<32> a = (rs1 == 0) ? u32(0) : regs[rs1];
        ap_int<32> b = (rs2 == 0) ? u32(0) : regs[rs2];
        ap_int<12> immi = ins.range(31, 20);
        ap_int<13> immb;
        immb[12] = ins[31]; immb[11] = ins[7]; immb.range(10, 5) = ins.range(30, 25);
        immb.range(4, 1) = ins.range(11, 8); immb[0] = 0;
        ap_int<21> immj;
        immj[20] = ins[31]; immj.range(19, 12) = ins.range(19, 12); immj[11] = ins[20];
        immj.range(10, 1) = ins.range(30, 21); immj[0] = 0;
        u32 npc = pc + 4;
        bool we = false;
        u32 val = 0;
        switch (opc) {
        case 0x37: we = true; val = ins & 0xFFFFF000; break;  // lui
        case 0x13: {                                       // op-imm
            we = true;
            ap_uint<5> sh = ins.range(24, 20);
            switch (f3) {
            case 0: val = a + immi; break;
            case 1: val = u32(a) << sh; break;
            case 2: val = a < ap_int<32>(immi); break;
            case 4: val = a ^ ap_int<32>(immi); break;
            case 5: val = ins[30] ? u32(a >> sh) : u32(u32(a) >> sh); break;
            case 6: val = a | ap_int<32>(immi); break;
            case 7: val = a & ap_int<32>(immi); break;
            default: we = false;
            }
            break;
        }
        case 0x33: {  // op, and mul (f7 1)
            we = true;
            if (f7 == 1) val = u32(ap_int<64>(a) * ap_int<64>(b));
            else switch (f3) {
            case 0: val = ins[30] ? u32(a - b) : u32(a + b); break;
            case 1: val = u32(a) << ap_uint<5>(b.range(4, 0)); break;
            case 2: val = a < b; break;
            case 4: val = a ^ b; break;
            case 6: val = a | b; break;
            case 7: val = a & b; break;
            default: we = false;
            }
            break;
        }
        case 0x63: {  // branches
            bool tk = false;
            switch (f3) {
            case 0: tk = a == b; break;
            case 1: tk = a != b; break;
            case 4: tk = a < b; break;
            case 5: tk = a >= b; break;
            default: tk = false;
            }
            if (tk) npc = pc + immb;
            break;
        }
        case 0x6F: we = true; val = pc + 4; npc = pc + immj; break;  // jal
        case 0x73: done = true; break;                               // ecall: job done
        case 0x57: {                                                 // OP-V
            if (f3 == 7) {  // vsetvli, e32 only
                vl = (u32(a) > VLMAX) ? ap_uint<8>(VLMAX) : ap_uint<8>(u32(a));
                we = true;
                val = vl;
            } else {
                vq_t e;
                e.range(31, 0) = ins;
                e.range(63, 32) = u32(a);
                e.range(71, 64) = vl;
                vq.write(e);
                if (f3 == 2 && ins.range(31, 26) == 0x10) {  // vmv.x.s: wait for the result
                    we = true;
                    val = ret.read();
                }
            }
            break;
        }
        default: break;
        }
        if (we && rd != 0) regs[rd] = val;
        pc = npc;
    }
    res.write(regs[14]);
}

void vector_task(hls::stream<vq_t> &vq, hls::stream<u32> &ret) {
    static u32 vr[32][VLMAX];
    vq_t e = vq.read();
    u32 ins = e.range(31, 0), xs = e.range(63, 32);
    ap_uint<8> vl = e.range(71, 64);
    ap_uint<5> vd = ins.range(11, 7), vs1 = ins.range(19, 15), vs2 = ins.range(24, 20);
    ap_uint<3> f3 = ins.range(14, 12);
    ap_uint<6> f6 = ins.range(31, 26);
    if (f3 == 2 && f6 == 0x10) {  // vmv.x.s
        ret.write(vr[vs2][0]);
    } else if (f3 == 2 && f6 == 0) {  // vredsum.vs
        u32 s = vr[vs1][0];
    RED:
        for (int i = 0; i < VLMAX; i++) {
#pragma HLS pipeline II = 1
            if (i < vl) s += vr[vs2][i];
        }
        vr[vd][0] = s;
    } else {
    EW:
        for (int i = 0; i < VLMAX; i++) {
#pragma HLS pipeline II = 1
            if (i < vl) {
                if (f3 == 0 && f6 == 0) vr[vd][i] = vr[vs2][i] + vr[vs1][i];  // vadd.vv
                else if (f3 == 4 && f6 == 0x17) vr[vd][i] = xs;               // vmv.v.x
            }
        }
    }
}

void w5_top(hls::stream<u32> &prog, hls::stream<u32> &res) {
#pragma HLS interface mode = ap_ctrl_none port = return
    hls_thread_local hls::stream<vq_t, 8> vq;
    hls_thread_local hls::stream<u32, 2> ret;
    hls_thread_local hls::task ts(scalar_task, prog, vq, ret, res);
    hls_thread_local hls::task tv(vector_task, vq, ret);
}
