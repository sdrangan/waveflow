"""The gain_stats kernel's golden model (given: the kernel must match it bit for bit).

``GainStats.run_iter`` is one command, as the kernel handles it in one firing; ``total`` is the
only state kept from one command to the next.  ``gain_stats_model`` runs a whole input stream.
Run this file to check the worked example at both word widths.
"""
from __future__ import annotations

import numpy as np

from waveflow.hw import fixpoint as fp
from waveflow.hw.arrayutils import get_nwords, read_array, write_array
from waveflow.hw.dataschema import DataArray
from waveflow.hw.fixpoint import FixedField
from waveflow.utils.fixputils import QMode

from gain_msgs import Gain, GainCmd, GainResp, GainStatus, Out, Samp

#: The product rounded as Out rounds it, with room for every product: a result saturated when
#: this differs from the Out value.  ap_fixed<19, 5, AP_RND>.
Rounded = FixedField.specialize(19, 5, q_mode=QMode.AP_RND)

TOTAL_MOD = 1 << 32


class GainStats:
    def __init__(self) -> None:
        self.total = 0                      # samples since reset; uint32, wraps

    def run_iter(self, words: np.ndarray, word_bw: int) -> tuple[list, list, int]:
        """One command from the front of *words*: (m_out words, m_status words, words used)."""
        cmd = GainCmd().deserialize(words, word_bw=word_bw)
        pos = GainCmd.nwords_per_inst(word_bw)
        x = read_array(words[pos:], elem_type=Samp, word_bw=word_bw, shape=cmd.n)
        pos += get_nwords(Samp, word_bw, cmd.n)

        # The gain as a one-element array: DataArray arithmetic takes no scalar operand.
        gain = DataArray.specialize(Gain, max_shape=(1,))(np.array([cmd.gain]))
        p = x * gain                                    # ap_fixed<32, 4>: exact
        y = fp.quantize(p, Out)                         # rounded, saturated
        nsat = int(np.count_nonzero(fp.ne(fp.quantize(p, Rounded), y)))   # int: a plain count

        self.total = (self.total + int(cmd.n)) % TOTAL_MOD   # int: a plain counter, wraps as uint32

        resp = GainResp()
        resp.tx_id = cmd.tx_id
        resp.n = cmd.n
        out = list(resp.serialize(word_bw=word_bw)) + list(write_array(y, word_bw=word_bw))

        st = GainStatus()
        st.tx_id = cmd.tx_id
        st.nsat = nsat
        st.total = self.total
        return out, list(st.serialize(word_bw=word_bw)), pos


def gain_stats_model(words_in, word_bw: int) -> tuple[list, list]:
    """Every command in *words_in*, in order: (m_out words, m_status words)."""
    words = np.asarray(words_in, dtype=np.uint64)       # plain list in: the word buffer
    k = GainStats()
    out, status, pos = [], [], 0
    while pos < len(words):
        o, s, used = k.run_iter(words[pos:], word_bw)
        out += o
        status += s
        pos += used
    return out, status


def make_input(cmds, word_bw: int) -> list:
    """Input words for [(tx_id, gain, samples), ...], gain and samples as reals."""
    words: list = []
    for tx_id, gain, samples in cmds:
        c = GainCmd()
        c.tx_id = tx_id
        c.n = len(samples)
        c.gain = fp.from_real([gain], Gain).val[0]
        words += list(c.serialize(word_bw=word_bw))
        words += list(write_array(fp.from_real(samples, Samp), word_bw=word_bw))
    return words


#: The worked example: two commands.
EXAMPLE = [
    (7, 3.0, [0.0, 0.5, -0.75, 0.25, 0.9]),         # -0.75 * 3 and 0.9 * 3 saturate
    (8, -0.5, [0.5, -1.0, 0.25]),                     # nothing saturates
]


def _check() -> None:
    for w in (32, 64):
        out, status = gain_stats_model(make_input(EXAMPLE, w), w)
        st = GainStatus.nwords_per_inst(w)
        decoded = [GainStatus().deserialize(np.asarray(status[i:i + st], dtype=np.uint64), word_bw=w)
                   for i in range(0, len(status), st)]
        assert [(s.tx_id, s.nsat, s.total) for s in decoded] == [(7, 2, 5), (8, 0, 8)], w
        r = GainResp.nwords_per_inst(w)
        y = read_array(np.asarray(out[r:], dtype=np.uint64), elem_type=Out, word_bw=w, shape=5)
        assert list(fp.to_real(y)) == [0.0, 1.5, -2.0, 0.75, 2 - 2 ** -14], (w, fp.to_real(y))
    print("gain_model: worked example passes at 32 and 64 bits")


if __name__ == "__main__":
    _check()
