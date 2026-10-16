// streamutils.cpp — legacy compatibility shim for Vitis HLS < 2025.1.
//
// In Vitis 2025.1 and later the AXI-stream utilities are provided by the
// vendor headers directly and this file is no longer required.  For older
// toolchain versions include this file in your Vitis HLS project alongside
// streamutils_hls.h so that the out-of-context (C-simulation) build can
// find the required symbol definitions.

#include "streamutils_hls.h"

namespace streamutils {

const char* tlast_status_info::names[tlast_status_info::count] = {
	"no_tlast",
	"tlast_at_end",
	"tlast_early",
};

} // namespace streamutils
