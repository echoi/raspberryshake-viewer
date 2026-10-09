"""MiniSEED/Steim decoding, checked against obspy as the reference."""
import io
import struct

import numpy as np
import pytest
from obspy import read

import raspberryshake_viewer as rv
from helpers import obspy_records

START = 1_760_000_000.123456


@pytest.mark.parametrize("encoding", ["STEIM1", "STEIM2"])
@pytest.mark.parametrize("amp", [3, 7, 15, 31, 120, 500, 3000, 20000, 600000])
def test_matches_obspy(encoding, amp):
    """Every packing width, including the 7x4-bit one used by quiet signals."""
    rng = np.random.default_rng(amp)
    data = (np.cumsum(rng.integers(-amp, amp + 1, 3000)) + 16000).astype(np.int32)
    decoded = []
    for rec in obspy_records(data, START, encoding=encoding):
        ch, start, samples = rv._decode_miniseed(rec)
        ref = read(io.BytesIO(rec))[0]
        assert ch == "EHZ"
        assert samples == ref.data.tolist()
        assert start == pytest.approx(ref.stats.starttime.timestamp, abs=1e-6)
        decoded += samples
    assert decoded == data.tolist()


def test_corrupt_record_is_dropped():
    """A record that doesn't integrate to its stored last sample (xn) is rejected."""
    rec = bytearray(obspy_records(np.arange(500) * 37, START)[0])
    rec[64 + 4] ^= 0x01   # flip a bit in x0 (frame 0, word 1)
    _, _, samples = rv._decode_miniseed(bytes(rec))
    assert samples == []


def test_unsupported_encoding_is_dropped():
    rec = bytearray(obspy_records(np.arange(500), START)[0])
    off = struct.unpack_from(">H", rec, 46)[0]
    while struct.unpack_from(">H", rec, off)[0] != 1000:   # 1001 may come first
        off = struct.unpack_from(">H", rec, off + 2)[0]
    rec[off + 4] = 3      # encoding -> 32-bit int (unsupported)
    _, _, samples = rv._decode_miniseed(bytes(rec))
    assert samples == []
