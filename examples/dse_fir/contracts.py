"""Executable FIR experiment ontology; transports do not own these semantics."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class Candidate(Contract):
    ntap: Literal[8, 16, 32, 64, 128] = 32
    samp_w: Literal[8, 12, 16, 24] = 16
    samp_i: int = Field(default=2, ge=1, le=8)
    unroll_lane: bool = False
    mem_dwidth: Literal[32] = 32

    @model_validator(mode="before")
    @classmethod
    def strict_integer_parameters(cls, value):
        # Pydantic's Literal[int] accepts equal floats even in strict mode.
        if isinstance(value, dict) and any(k in value and type(value[k]) is not int
                                          for k in ("ntap", "samp_w", "samp_i", "mem_dwidth")):
            raise ValueError("integer parameters must be integers, not booleans or floats")
        return value


class Evaluation(Contract):
    f_pass: float = Field(default=0.20, gt=0, lt=0.5)
    f_stop: float = Field(default=0.28, gt=0, lt=0.5)
    nsamp: int = Field(default=2048, ge=1024, le=16384)
    seed: int = Field(default=0, ge=0, le=2**32 - 2)
    input_peak: float = Field(default=0.5, gt=0, le=0.9)
    nfft: int = Field(default=8192, ge=2048, le=65536)

    @model_validator(mode="after")
    def ordered_bands(self):
        if self.f_stop <= self.f_pass:
            raise ValueError("require f_pass < f_stop")
        return self


class Constraints(Contract):
    min_passband_sndr_db: float = 20.0
    min_throughput: float = Field(default=1.0, gt=0)
    max_top_dsp: int = Field(default=48, ge=0)
    max_top_lut: int = Field(default=53200, ge=0)


class Budget(Contract):
    pysim: int = Field(default=64, ge=0, le=10000)
    predict_resource: int = Field(default=64, ge=0, le=10000)
    synth: int = Field(default=8, ge=0, le=10000)
    rtlsim: int = Field(default=2, ge=0, le=10000)


class Experiment(Contract):
    evaluation: Evaluation = Field(default_factory=Evaluation)
    constraints: Constraints = Field(default_factory=Constraints)
    budget: Budget = Field(default_factory=Budget)
    objective: Literal["maximize_stopband_rej_db"] = "maximize_stopband_rej_db"
    platform: Literal["zynq7020_bfm_100mhz"] = "zynq7020_bfm_100mhz"


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()
