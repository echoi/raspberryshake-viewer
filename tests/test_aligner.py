"""ChannelAligner: samples from all channels must line up by timestamp."""
import random

import numpy as np

import raspberryshake_viewer as rv

T0 = 1_760_000_000.0
N0 = int(T0 * 100)


def feeds_for(ch, rec_len, total, lag_ticks):
    """Records whose sample values equal their absolute sample index."""
    feeds = []
    for k in range(0, total, rec_len):
        idx = list(range(N0 + k, N0 + min(k + rec_len, total)))
        available = (k + rec_len) // 4 + lag_ticks   # 4 samples per tick
        feeds.append((available, ch, (N0 + k) / 100 + 0.002, idx))
    return feeds


def run(feeds, n_ticks, per_tick=4):
    al = rv.ChannelAligner(rv.CHANNELS)
    feeds = sorted(feeds, key=lambda f: f[0])
    out = {ch: [] for ch in rv.CHANNELS}
    i = 0
    for tick in range(n_ticks):
        while i < len(feeds) and feeds[i][0] <= tick:
            _, ch, start, samples = feeds[i]
            al.push(ch, start, samples)
            i += 1
        for ch, vals in al.pull(per_tick).items():
            out[ch] += vals
    return al, out


def test_channels_aligned_despite_different_records_and_latency():
    feeds = (feeds_for("EHZ", 350, 12000, 3) + feeds_for("EHN", 410, 12000, 40)
             + feeds_for("EHE", 280, 12000, 90))
    random.Random(1).shuffle(feeds)
    al, out = run(feeds, 3000)
    z, n, e = out["EHZ"], out["EHN"], out["EHE"]
    assert len(z) == len(n) == len(e) > 10000
    assert z == n == e                            # same instant on every channel
    assert all(b >= a for a, b in zip(z, z[1:]))  # never replayed or reordered
    # Surplus latency is drained back toward the jitter margin
    assert al._backlog() < 3 * rv.SAMPLE_RATE


def test_nothing_drawn_before_first_data():
    al = rv.ChannelAligner(rv.CHANNELS)
    assert al.pull(4) == {ch: [] for ch in rv.CHANNELS}


def test_overlap_trimmed_and_short_gap_filled():
    al = rv.ChannelAligner(rv.CHANNELS)
    for ch in rv.CHANNELS:
        al.push(ch, T0, list(range(N0, N0 + 100)))
    al.push("EHZ", T0 + 0.5, list(range(N0 + 50, N0 + 150)))    # overlap
    al.push("EHN", T0 + 1.0, list(range(N0 + 100, N0 + 120)))
    al.push("EHN", T0 + 1.4, list(range(N0 + 140, N0 + 150)))   # 0.2 s gap
    al.push("EHE", T0 + 1.0, list(range(N0 + 100, N0 + 150)))
    b = al.pull(150)
    assert b["EHZ"] == list(range(N0, N0 + 150))
    assert b["EHN"][100:120] == list(range(N0 + 100, N0 + 120))
    assert b["EHN"][120:140] == [N0 + 119] * 20                  # held value
    assert b["EHN"][140:150] == list(range(N0 + 140, N0 + 150))


def test_long_gap_jumps_ahead():
    al = rv.ChannelAligner(rv.CHANNELS)
    for ch in rv.CHANNELS:
        al.push(ch, T0, [1] * 100)
    al.pull(100)
    for ch in rv.CHANNELS:
        al.push(ch, T0 + 60, [2] * 50000)
    assert al.pull(4) == {ch: [2] * 4 for ch in rv.CHANNELS}
    assert al._backlog() <= rv.ChannelAligner.MAX_BACKLOG


def test_record_start_time_parsed_from_live_style_record():
    """Aligner keys on the decoder's start time; both must agree to the sample."""
    from helpers import obspy_records
    rec = obspy_records(np.arange(300) * 11, T0 + 0.37)[0]
    _, start, _ = rv._decode_miniseed(rec)
    assert int(round(start * rv.SAMPLE_RATE)) == N0 + 37
