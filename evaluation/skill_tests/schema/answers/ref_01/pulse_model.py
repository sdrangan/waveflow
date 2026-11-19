"""Reference answer, 01_pulse_report: the model, a function of the input words."""
import numpy as np

from waveflow.hw.arrayutils import read_array, write_array
from waveflow.hw.fixpoint import from_real

from pulse_msgs import SCHEMA, Done, Kind, Op, Peak, PulseCmd, Samp, Start, Tag, Thresh


def pulse_det_model(words_in, word_bw):
    out, pos = [], 0
    ncmd = PulseCmd.nwords_per_inst(word_bw)
    while True:
        cmd = PulseCmd().deserialize(words_in[pos:pos + ncmd], word_bw=word_bw)
        pos += ncmd
        if cmd.op == Op.END:
            break
        x = read_array(words_in[pos:], elem_type=Samp, word_bw=word_bw, shape=cmd.nsamp)
        pos += len(write_array(x.val, elem_type=Samp, word_bw=word_bw))
        # thresh is ufixed<16,4> (12 fractional bits), x is fixed<12,1> (11): align the stored ints
        x_al = x.val.astype(np.int64) << 1                     # to 12 fractional bits
        hits = np.nonzero(x_al > int(cmd.thresh))[0]           # int: plain-Python compare of stored ints
        out.append(Start(kind=Kind.START, tx_id=cmd.tx_id).serialize(word_bw=word_bw))
        for i in hits:
            out.append(Peak(kind=Kind.PEAK, index=i, value=x.val[i]).serialize(word_bw=word_bw))
        out.append(Done(kind=Kind.DONE, tx_id=cmd.tx_id, count=len(hits)).serialize(word_bw=word_bw))
    return np.concatenate(out) if out else np.zeros(0, dtype=np.uint64)


def decode(words, word_bw):
    msgs, i = [], 0
    while i < len(words):
        k = Kind(Tag().deserialize(words[i:i + 1], word_bw=word_bw).kind)
        cls = SCHEMA[k]
        n = cls.nwords_per_inst(word_bw)
        msgs.append(cls().deserialize(words[i:i + n], word_bw=word_bw))
        i += n
    return msgs


def stimulus(word_bw):
    t = Thresh(int(0.25 * 2**12))                              # stored integer of 0.25
    cmd = PulseCmd(op=Op.DETECT, tx_id=7, nsamp=5, thresh=t.val)
    samp = from_real([0.0, 0.5, -0.75, 0.25, 0.3125], Samp)
    end = PulseCmd(op=Op.END, tx_id=0, nsamp=0, thresh=0)
    return np.concatenate([cmd.serialize(word_bw=word_bw),
                           write_array(samp.val, elem_type=Samp, word_bw=word_bw),
                           end.serialize(word_bw=word_bw)])


if __name__ == "__main__":
    for W in (32, 64):
        msgs = decode(pulse_det_model(stimulus(W), W), W)
        got = [(type(m).__name__, {k: getattr(m, k) for k in m.elements if k != "kind"}) for m in msgs]
        print(W, got)
        assert [type(m) for m in msgs] == [Start, Peak, Peak, Done]
        assert msgs[1].index == 1 and msgs[2].index == 4 and msgs[3].count == 2
        assert msgs[1].value == 0.5 * 2**11 and msgs[2].value == 0.3125 * 2**11
    print("ok")
