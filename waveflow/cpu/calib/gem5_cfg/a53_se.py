# a53_se.py -- gem5's starter_se.py with the HPI core's cache sizes overridable.
#
# A gem5 configuration script (run by gem5.opt, not imported by waveflow).  gem5 v25.1 has no
# command-line parameter override, and starter_se.py has no cache-size options, so this script
# replaces the HPI entry of starter_se's cpu_types with cache classes of the requested sizes and then
# runs starter_se's own main() -- every other argument and behaviour is starter_se's.
#
#   gem5.opt a53_se.py --l1i-size 32KiB --l1d-size 16KiB --l2-size 512KiB --cpu hpi ... <command>
#
# Mounted at /wfcfg by waveflow.cpu.calib.gem5.Gem5Runner; starter_se.py at /gem5/configs/example/arm.
import argparse
import sys

sys.path.insert(0, "/gem5/configs/example/arm")

ap = argparse.ArgumentParser(add_help=False)
ap.add_argument("--l1i-size", required=True)
ap.add_argument("--l1d-size", required=True)
ap.add_argument("--l2-size", required=True)
ours, rest = ap.parse_known_args(sys.argv[1:])
sys.argv = [sys.argv[0], *rest]

import starter_se  # type: ignore[import-not-found]  # after sys.path; sets up the config path
from common.cores.arm import HPI  # type: ignore[import-not-found]

L1I = type("WfL1I", (HPI.HPI_ICache,), {"size": ours.l1i_size})
L1D = type("WfL1D", (HPI.HPI_DCache,), {"size": ours.l1d_size})
L2 = type("WfL2", (HPI.HPI_L2,), {"size": ours.l2_size})
starter_se.cpu_types["hpi"] = (HPI.HPI, L1I, L1D, L2)

starter_se.main()
