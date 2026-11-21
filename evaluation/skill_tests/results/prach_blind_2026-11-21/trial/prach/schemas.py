"""schemas.py -- every message of the PRACH searcher, declared once (DataList / IntField / EnumField).

Standalone (no relative imports) so ``waveflow_validate_schema`` can load it.  The C++ headers are
generated from these classes by ``prach_build.py`` (``DataSchemaStep(..., framed=True)``).
"""
from __future__ import annotations

from enum import IntEnum

from waveflow.hw.dataschema import DataList, EnumField, IntField

INCLUDE_DIR = "include"

U8 = IntField.specialize(bitwidth=8, signed=False, include_dir=INCLUDE_DIR)
U16 = IntField.specialize(bitwidth=16, signed=False, include_dir=INCLUDE_DIR)
U40 = IntField.specialize(bitwidth=40, signed=False, include_dir=INCLUDE_DIR)
U48 = IntField.specialize(bitwidth=48, signed=False, include_dir=INCLUDE_DIR)

class StatusCode(IntEnum):
    OK = 0
    TLAST_EARLY = 1      # the roots or a window ended early: fatal
    BAD_PARAM = 2        # a command field out of range: fatal
    BAD_ROOT = 3         # u outside 1..138, or u * u_inv != 1 mod 139: fatal
    TLAST_LATE = 4       # the command, the roots or a window has no TLAST where it should end: fatal


StatusCodeField = EnumField.specialize(enum_type=StatusCode)


class PrachCmd(DataList):
    """One occasion: the antennas and repetitions that follow, where the PRACH sits, its preambles."""
    elements = {
        "tx_id":   {"schema": U16, "description": "the sender's id, echoed in Resp and Status"},
        "n_ant":   {"schema": U8,  "description": "antennas, 1 to 4"},
        "n_sym":   {"schema": U8,  "description": "repetitions (FFT windows per antenna), 1 to 12"},
        "k_start": {"schema": U16, "description": "FFT bin of PRACH subcarrier 0, 0 to 1023"},
        "n_cs":    {"schema": U8,  "description": "N_CS, 0 to 139 (0: one preamble per root)"},
        "n_pre":   {"schema": U8,  "description": "preambles searched, 1 to 64"},
        "n_roots": {"schema": U8,  "description": "roots that follow: ceil(n_pre / per_root)"},
        "thr_q8":  {"schema": U16, "description": "threshold on peak / mean, times 256"},
    }


class FwdHdr(DataList):
    """front -> extract, ahead of the windows: the command, and how many windows go to the FFT
    (0 after a header or root error).  The roots follow when nframes > 0."""
    elements = {
        "tx_id":   {"schema": U16, "description": "the command's id"},
        "n_ant":   {"schema": U8,  "description": "antennas"},
        "n_sym":   {"schema": U8,  "description": "repetitions"},
        "k_start": {"schema": U16, "description": "first PRACH bin"},
        "n_cs":    {"schema": U8,  "description": "N_CS"},
        "n_pre":   {"schema": U8,  "description": "preambles"},
        "n_roots": {"schema": U8,  "description": "roots"},
        "thr_q8":  {"schema": U16, "description": "threshold"},
        "nframes": {"schema": U16, "description": "windows sent to fft1024: n_sym * n_ant, or 0"},
    }


class FwdTrl(DataList):
    """front -> extract, after the job's last window: how the job ended."""
    elements = {
        "tx_id": {"schema": U16,             "description": "the command's id"},
        "code":  {"schema": StatusCodeField, "description": "OK or the fatal error front found"},
    }


class ExtHdr(DataList):
    """extract -> correlate: what correlate and detect need.  When code is OK the roots and Y
    (n_ant x 140 bins, bin 139 zero) follow, each a burst."""
    elements = {
        "tx_id":   {"schema": U16,             "description": "the command's id"},
        "n_ant":   {"schema": U8,              "description": "antennas"},
        "n_cs":    {"schema": U8,              "description": "N_CS"},
        "n_pre":   {"schema": U8,              "description": "preambles"},
        "n_roots": {"schema": U8,              "description": "roots"},
        "thr_q8":  {"schema": U16,             "description": "threshold"},
        "exp":     {"schema": U8,              "description": "the block exponent of Y"},
        "code":    {"schema": StatusCodeField, "description": "OK, or the error to report"},
    }


class CorHdr(DataList):
    """correlate -> detect, ahead of the job's n_roots * n_ant frames on fft256 (none unless OK)."""
    elements = {
        "tx_id":   {"schema": U16,             "description": "the command's id"},
        "n_ant":   {"schema": U8,              "description": "antennas"},
        "n_cs":    {"schema": U8,              "description": "N_CS"},
        "n_pre":   {"schema": U8,              "description": "preambles"},
        "n_roots": {"schema": U8,              "description": "roots"},
        "thr_q8":  {"schema": U16,             "description": "threshold"},
        "exp":     {"schema": U8,              "description": "the block exponent of Y"},
        "code":    {"schema": StatusCodeField, "description": "OK, or the error to report"},
    }


class Resp(DataList):
    """The header of an occasion's detections on m_out."""
    elements = {
        "tx_id": {"schema": U16, "description": "echo of the command's tx_id"},
        "n_det": {"schema": U8,  "description": "detection records that follow (a burst; none if 0)"},
        "exp":   {"schema": U8,  "description": "block exponent: powers are in units of 2^(2 exp)"},
    }


class DetRec(DataList):
    """One detected preamble: one word."""
    elements = {
        "preamble": {"schema": U8,  "description": "preamble index, 0 to 63"},
        "n_peak":   {"schema": U8,  "description": "the peak's index on the 256-point profile"},
        "delay":    {"schema": U8,  "description": "the peak's offset into its window: (n_peak - n_start) mod 256"},
        "peak":     {"schema": U40, "description": "the peak of the power-delay profile"},
        "noise":    {"schema": U48, "description": "the profile's sum less +-2 samples round every window's peak"},
        "n_noise":  {"schema": U16, "description": "the samples in that sum (mean noise = noise / n_noise)"},
    }


class Status(DataList):
    """The last command seen, on m_status: one per command, after its results."""
    elements = {
        "tx_id":  {"schema": U16,             "description": "the command it reports on"},
        "status": {"schema": StatusCodeField, "description": "OK, or the fatal error"},
    }


SCHEMA_CLASSES = [StatusCodeField, PrachCmd, FwdHdr, FwdTrl, ExtHdr, CorHdr, Resp, DetRec, Status]
