// vmac_compute_impl.tpp
// Included from gen/vmac.hpp.  The generated header brings into scope, *before* this file:
//
//   * VmacCmd                  — the command struct (DataSchemaStep), incl. the OpCode enum.
//   * vmac_in_au / vmac_out_au — namespace aliases for the **generated ComplexField
//                                serialization** of the operand / output element types
//                                (ArrayUtilsStep over ComplexField.specialize(FixedField…)).
//     `vmac_in_au::value_type`  == std::complex<ap_fixed<DATA_BW, INT_BITS, …>>  (operand)
//     `vmac_out_au::value_type` == std::complex<ap_fixed<OUT_BW,  OUT_INT,  …>>  (writeback)
//   * complex_utils::{cmult,cadd,conj,cwiden,cx_requantize,cx_from_codes} — the complex toolkit
//                                (full-precision arithmetic + construct/widen/requantize).
//
// Hand-written body for the templated `vmac_impl::vmac_compute` hook — the VMAC datapath: one of
// three **complex element-wise** ops, with an optional row reduction
//
//     scalar_mult :  R[i, j] = alpha[i] · A[i, j]
//     inner_prod  :  R[i, j] = A[i, j] · conj(B[i, j])
//     sum         :  R[i, j] = A[i, j] + B[i, j]
//     reduce      :  Y[j] = Σ_i R[i, j]          (else Y[i, j] = R[i, j])
//
// over a row-major shared-memory image reached through the `m_axi` pointer.  This is the C++
// contract of `VmacAccel.vmac_compute` (whose Python body is the golden `VmacAccel.execute`);
// it is bit-identical to that golden by construction.
//
// The datapath stays **complex-typed end to end** — `std::complex<ap_fixed>` lanes, a complex
// scalar, `complex_utils` ops, a complex accumulator, and an `ap_fixed`-assignment requantize —
// no hand-split re/im.  Full precision is carried in the `ap_fixed` value (no rounding until the
// single requantize); the wide accumulator holds the value at fractional depth 2*F_in (>= the
// per-op F_in/2F_in the golden tracks), so requantizing its *value* to F_out = F_in reproduces
// the golden's quantize bit-for-bit (the intermediate fractional depth is irrelevant once the
// value is exact).
//
// ONE fixed-format kernel, configured at *run time* by `op` / `reduce`:
//   * Template params are **widths only** -> all ap_fixed types are compile-time (no dynamic types).
//   * `op` / `reduce` are **runtime** if/mux — loop-invariant (no II hit).
//
// Memory addressing: operand rows are read/written with the fused **element-lane** primitives
// (`read_array_lane` / `write_array_lane`) over a running word pointer — the canonical
// lane loop, one word/beat -> PF complex columns, advanced a constant per step (PF-packed rows
// are word-aligned; `elem_to_word` checks it).  `read_array_slice` is used **only** for the
// per-row alpha[i], a single element at an arbitrary element index (read once per row).

#include <cassert>

#include "complex_utils.hpp"

namespace vmac_impl {

// OpCode values (mirror examples/vmac/vmac_cmd.py OpCode; the generated `enum class OpCode`
// shares these, but the core takes a plain int so the s_axilite top can pass a register).
static const int VMAC_OP_SCALAR_MULT = 0;
static const int VMAC_OP_INNER_PROD = 1;
static const int VMAC_OP_SUM = 2;

// Map an *element* index/stride to its *word* index for PF-packed memory (PF elements/word):
// elem / PF — a shift when PF is a power of two (a static_assert enforces that, so a non-power-
// of-two config fails loud at compile time rather than silently synthesizing a real divider),
// plus a PF-alignment check — the per-row operand regions (bases + strides) must be word-aligned.
// (The per-row indirect alpha is genuinely element-addressed and is read via read_array_slice,
// not this aligned helper.)
template <int PF, typename T>
static inline T elem_to_word(T elem) {
#pragma HLS INLINE
    static_assert(PF > 0 && (PF & (PF - 1)) == 0,
                  "VMAC requires a power-of-two PF (MEM_BW / element_bits) so the element->word "
                  "map is a shift; a non-power-of-two PF would synthesize a real hardware divider.");
    assert(elem % PF == 0 && "VMAC operand region must be PF-aligned (word-packed rows)");
    return elem / PF;
}

// The datapath, taking the command as **typed scalars** (not the VmacCmd struct).  The
// synthesizable top calls this directly: a nested struct passed by value mis-decomposes
// through HLS's Array/Struct optimization at csynth (loop bounds fold to 0 -> the kernel is
// DCE'd), whereas scalar s_axilite args lower cleanly.  The fields keep their precise types
// (addresses ap_uint<MEM_AWIDTH>, strides signed, shape ap_uint<16>, the op an int, alpha a
// complex value) so the address arithmetic is sized by MEM_AWIDTH.  vmac_compute(cmd, mem)
// below is the thin struct-taking wrapper the csim conformance harness drives.
template <int MEM_BW, int MEM_AWIDTH, int DATA_BW, int INT_BITS, int ACC_BW, int OUT_BW,
          bool Q_RND, bool O_SAT, int MAX_COLS>
void vmac_compute_core(
    ap_uint<MEM_BW>* mem,
    int op, bool reduce, ap_uint<16> n_rows, ap_uint<16> n_cols,
    ap_uint<MEM_AWIDTH> a_addr, ap_int<MEM_AWIDTH> a_rs,
    ap_uint<MEM_AWIDTH> b_addr, ap_int<MEM_AWIDTH> b_rs,
    ap_uint<MEM_AWIDTH> y_addr, ap_int<MEM_AWIDTH> y_rs,
    bool al_direct, typename vmac_in_au::value_type alpha_imm,
    ap_uint<MEM_AWIDTH> al_addr, ap_int<MEM_AWIDTH> al_stride) {
#pragma HLS INLINE
    // Inline into the synthesizable top so the m_axi reads/writes belong to the top's gmem
    // port (kept as a separate module, the top would have "no outputs" and gmem would dangle).
    typedef typename vmac_in_au::value_type CX;     // std::complex<ap_fixed<DATA_BW, INT_BITS, …>>
    typedef typename vmac_out_au::value_type CXO;   // std::complex<ap_fixed<OUT_BW, OUT_INT, …>>
    typedef ap_fixed<DATA_BW + 1, INT_BITS + 1> RT_FX;    // right-term format (W+1, I+1): conj / widened alpha
    typedef std::complex<RT_FX> RT_CX;
    static const int F_IN = DATA_BW - INT_BITS;
    static const int OUT_INT = OUT_BW - F_IN;             // F_out = F_in (structural)
    static const ap_q_mode QMODE = Q_RND ? AP_RND : AP_TRN;
    static const ap_o_mode OMODE = O_SAT ? AP_SAT : AP_WRAP;
    typedef ap_fixed<OUT_BW, OUT_INT, QMODE, OMODE> REQ_FX;       // requantize target (structural Q/O)
    typedef ap_fixed<ACC_BW, ACC_BW - 2 * F_IN> ACC_FX;          // accumulator component, frac = 2*F_in
    typedef std::complex<ACC_FX> ACC_CX;
    static constexpr int PF = vmac_in_au::pf<MEM_BW>();          // complex columns / word (power of 2)

    const bool op_sum = (op == VMAC_OP_SUM);
    const bool op_scalar = (op == VMAC_OP_SCALAR_MULT);
    const bool need_b = (op != VMAC_OP_SCALAR_MULT);            // inner_prod / sum read B
    // ab_eq: B aliases A element-for-element (same base AND same row stride), so b_lane can be
    // filled by copying a_lane instead of issuing a second m_axi burst.  Loop-invariant (derived
    // from the command, no host flag); a RUNTIME flag, so HLS keeps ONE static II (worst case =
    // b-read present) and only predicates whether the B transaction fires — same cycle count,
    // B's read-bus beat suppressed.  Requiring equal stride keeps the copy bit-exact with the read.
    const bool ab_eq = need_b && (a_addr == b_addr) && (a_rs == b_rs);

    // per-column complex accumulators (reduce): summed over rows.
    ACC_CX acc[MAX_COLS];
#pragma HLS ARRAY_PARTITION variable=acc cyclic factor=PF dim=1
    if (reduce) {
        for (int j = 0; j < (int)n_cols; ++j) {
#pragma HLS PIPELINE II=1
            acc[j] = ACC_CX(0, 0);
        }
    }

    // running word indices: row base = addr/PF, advanced by row_stride/PF per row (both exact —
    // regions are PF-aligned; elem_to_word checks it).
    const ap_uint<MEM_AWIDTH> a_w0 = elem_to_word<PF>(a_addr), b_w0 = elem_to_word<PF>(b_addr);
    const ap_uint<MEM_AWIDTH> y_w0 = elem_to_word<PF>(y_addr);
    const ap_int<MEM_AWIDTH> a_rsw = elem_to_word<PF>(a_rs), b_rsw = elem_to_word<PF>(b_rs);
    const ap_int<MEM_AWIDTH> y_rsw = elem_to_word<PF>(y_rs);

    // outer loop over rows (running word pointers per operand), inner over contiguous columns
    // packed PF/word — the fused canonical lane loop.
    ap_uint<MEM_AWIDTH> a_row = a_w0, b_row = b_w0, y_row = y_w0;
    for (int i = 0; i < (int)n_rows; ++i) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=MAX_COLS
        // per-row alpha (scalar_mult): direct immediate, or the single element at al_addr +
        // i*al_stride — read once per row via the element-indexed slice (no e/PF in the kernel).
        CX alpha = alpha_imm;
        if (op_scalar && !al_direct) {
            const int e = (int)al_addr + i * (int)al_stride;
            vmac_in_au::read_array_slice<MEM_BW>(mem, e, e + 1, &alpha);
        }
        // alpha broadcast as the right-term for scalar_mult (widened losslessly to (W+1, I+1)).
        const RT_CX alpha_rt = RT_CX((RT_FX)alpha.real(), (RT_FX)alpha.imag());

        ap_uint<MEM_AWIDTH> a_w = a_row, b_w = b_row, y_w = y_row;
        for (int col0 = 0; col0 < (int)n_cols; col0 += PF) {
#pragma HLS PIPELINE II=1
            const int cols = ((int)n_cols - col0 < PF) ? ((int)n_cols - col0) : PF;
            CX a_lane[PF], b_lane[PF];
#pragma HLS ARRAY_PARTITION variable=a_lane complete dim=1
#pragma HLS ARRAY_PARTITION variable=b_lane complete dim=1
            vmac_in_au::read_array_lane<MEM_BW>(mem + a_w, a_lane, cols);
            if (need_b && !ab_eq)
                vmac_in_au::read_array_lane<MEM_BW>(mem + b_w, b_lane, cols);
            else if (ab_eq)
                for (int k = 0; k < PF; ++k) b_lane[k] = a_lane[k];  // B aliases A: copy, no re-read

            CXO y_lane[PF];
#pragma HLS ARRAY_PARTITION variable=y_lane complete dim=1
            for (int k = 0; k < PF; ++k) {
#pragma HLS UNROLL
                const int j = col0 + k;
                if (j >= (int)n_cols) continue;

                // R = A + B (sum) or A * right_term (scalar_mult: alpha broadcast / inner_prod:
                // conj(B)) — one shared complex multiply for the two product ops.
                ACC_CX r;
                if (op_sum) {
                    r = complex_utils::cwiden<ACC_CX>(complex_utils::cadd(a_lane[k], b_lane[k]));
                } else {
                    const RT_CX right = op_scalar ? alpha_rt : complex_utils::conj(b_lane[k]);
                    r = complex_utils::cwiden<ACC_CX>(complex_utils::cmult(a_lane[k], right));
                }

                if (reduce)
                    acc[j] = complex_utils::cwiden<ACC_CX>(complex_utils::cadd(acc[j], r));
                else
                    y_lane[k] = complex_utils::cx_requantize<CXO, REQ_FX>(r);
            }
            if (!reduce)
                vmac_out_au::write_array_lane<MEM_BW>(y_lane, mem + y_w, cols);

            a_w += 1; b_w += 1; y_w += 1;
        }
        a_row += a_rsw; b_row += b_rsw; y_row += y_rsw;
    }

    // reduce writeback: one row of n_cols requantized results at the dst.
    if (reduce) {
        ap_uint<MEM_AWIDTH> y_w = y_w0;
        for (int col0 = 0; col0 < (int)n_cols; col0 += PF) {
#pragma HLS PIPELINE II=1
            const int cols = ((int)n_cols - col0 < PF) ? ((int)n_cols - col0) : PF;
            CXO y_lane[PF];
#pragma HLS ARRAY_PARTITION variable=y_lane complete dim=1
            for (int k = 0; k < PF; ++k) {
#pragma HLS UNROLL
                const int j = col0 + k;
                if (j >= (int)n_cols) continue;
                y_lane[k] = complex_utils::cx_requantize<CXO, REQ_FX>(acc[j]);
            }
            vmac_out_au::write_array_lane<MEM_BW>(y_lane, mem + y_w, cols);
            y_w += 1;
        }
    }
}

// Struct-taking wrapper — extracts the command's (typed) fields and calls the core.  Used by the
// csim conformance harness (where the by-value struct is fine); the synthesizable top calls
// vmac_compute_core directly to avoid the nested-struct csynth pitfall above.
template <int MEM_BW, int MEM_AWIDTH, int DATA_BW, int INT_BITS, int ACC_BW, int OUT_BW,
          bool Q_RND, bool O_SAT, int MAX_COLS>
void vmac_compute(VmacCmd cmd, ap_uint<MEM_BW>* mem) {
    typedef typename vmac_in_au::value_type CXIN;
    vmac_compute_core<MEM_BW, MEM_AWIDTH, DATA_BW, INT_BITS, ACC_BW, OUT_BW, Q_RND, O_SAT, MAX_COLS>(
        mem,
        (int)cmd.op, (bool)cmd.reduce, cmd.n_rows, cmd.n_cols,
        cmd.a.addr, cmd.a.row_stride,
        cmd.b.addr, cmd.b.row_stride,
        cmd.y.addr, cmd.y.row_stride,
        (bool)cmd.alpha.direct,
        complex_utils::cx_from_codes<CXIN>(cmd.alpha.imm),
        cmd.alpha.addr, cmd.alpha.stride);
}

}  // namespace vmac_impl
