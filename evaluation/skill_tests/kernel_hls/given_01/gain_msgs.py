"""The gain_stats kernel's messages (given: do not change this file).

s_in:     GainCmd, then ``n`` samples (Samp)
m_out:    GainResp, then ``n`` results (Out)
m_status: GainStatus, one per command
"""
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.fixpoint import FixedField
from waveflow.utils.fixputils import OMode, QMode

INC = "include"

U16 = IntField.specialize(bitwidth=16, signed=False)
U32 = IntField.specialize(bitwidth=32, signed=False)

#: A gain in [-4, 4): ap_fixed<16, 3>.
Gain = FixedField.specialize(16, 3, include_dir=INC)
#: An input sample in [-1, 1): ap_fixed<16, 1>.
Samp = FixedField.specialize(16, 1, include_dir=INC)
#: A result in [-2, 2): ap_fixed<16, 2, AP_RND, AP_SAT>.
Out = FixedField.specialize(16, 2, q_mode=QMode.AP_RND, o_mode=OMode.AP_SAT, include_dir=INC)


class GainCmd(DataList):
    elements = {
        "tx_id": {"schema": U16, "description": "echoed in the response and the status"},
        "n": {"schema": U16, "description": "samples that follow, 1 to 4096"},
        "gain": {"schema": Gain, "description": "the gain applied to every sample"},
    }


class GainResp(DataList):
    elements = {
        "tx_id": {"schema": U16, "description": "the command's tx_id"},
        "n": {"schema": U16, "description": "results that follow"},
    }


class GainStatus(DataList):
    elements = {
        "tx_id": {"schema": U16, "description": "the command's tx_id"},
        "nsat": {"schema": U16, "description": "results of this command that saturated"},
        "total": {"schema": U32, "description": "samples processed since reset, this command's included"},
    }


SCHEMA_CLASSES = [GainCmd, GainResp, GainStatus]
ELEM_TYPES = [Samp, Out]
