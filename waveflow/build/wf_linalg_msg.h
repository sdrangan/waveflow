// wf_linalg_msg.h -- the framed messages of the linear-algebra units: replies and drains.
//
// A message is a header burst (LinalgHeader, waveflow/linalg/message.py) followed by its payload
// burst.  A unit answers every request with a reply that carries the request's tag, operation and
// dimensions and a status; a request it rejects is answered with no payload, and its own payload
// is drained by the header's length.  Nothing is clamped.
#ifndef WF_LINALG_MSG_H
#define WF_LINALG_MSG_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_linalg_header.h"

namespace wf_linalg {

// The status of a reply (waveflow.linalg.message.Status).
enum Status { OK = 0, BAD_OP = 1, BAD_DIMS = 2, BAD_LENGTH = 3, BAD_SEQUENCE = 4 };

// Read and discard the n payload words of a rejected request.
template <int WBW>
static void drain(hls::stream<streamutils::framed_word<WBW> >& s_in, ap_uint<32> n) {
#pragma HLS INLINE off
DRAIN:
    for (ap_uint<32> i = 0; i < n; ++i) {
#pragma HLS PIPELINE II = 1
        s_in.read();
    }
}

// Write the reply header to a request: its tag, operation and dimensions, with this status and a
// payload of length words to follow (0 for a rejected request).
template <int WBW>
static void reply(hls::stream<streamutils::framed_word<WBW> >& s_out, const LinalgHeader& req,
                  int status, ap_uint<32> length) {
#pragma HLS INLINE
    LinalgHeader r = req;
    r.status = status;
    r.length = length;
    r.nfollow = 0;
    r.template write_framed_stream<WBW>(s_out);
}

}  // namespace wf_linalg

#endif  // WF_LINALG_MSG_H
