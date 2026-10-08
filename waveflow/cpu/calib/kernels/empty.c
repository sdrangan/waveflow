/* empty.c -- an empty measured region: the cost of the markers themselves.
 *
 * WF_ROI_BEGIN immediately followed by WF_ROI_END.  The gem5 runner measures this once per core
 * configuration and subtracts it from every kernel point (94 cycles on HPI at 1.2 GHz, step 2 of
 * plans/cpu_model.md), because for the smallest scheduler operations it is most of the reading.
 *
 * No counters.   Twin: kernels/numeric.py (empty).
 */
#include "wf_kernel.h"

int main(void) {
    WF_ROI_BEGIN();
    WF_ROI_END();
    wf_json_begin("empty");
    wf_json_end();
    return 0;
}
