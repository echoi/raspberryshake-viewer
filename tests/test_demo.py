"""Demo mode: Steim-2 encoder, synthetic quake physics, end-to-end pipeline."""
import io
import time

import numpy as np
import pytest
from obspy import read

import raspberryshake_viewer as rv
from helpers import principal_axes


@pytest.mark.parametrize("amp", [3, 7, 15, 31, 127, 511, 16383, 500_000, 100_000_000])
def test_encoder_roundtrip(amp):
    rng = np.random.default_rng(amp)
    x = (np.cumsum(rng.integers(-amp, amp + 1, 3000)) + 16500).astype(np.int64)
    x = np.clip(x, -2**31, 2**31 - 1).tolist()
    start_idx = int(1_760_000_123.47 * 100)
    pos, prev, seq = 0, None, 0
    while pos < len(x):
        rec, used = rv._encode_steim2_record(seq, "AM", "R8049", "00", "EHZ",
                                             start_idx + pos, x[pos:], prev)
        assert len(rec) == 512 and used > 0
        expected_start = (start_idx + pos) / 100

        ref = read(io.BytesIO(rec))[0]          # independent decoder
        assert ref.data.tolist() == x[pos:pos + used]
        assert ref.stats.station == "R8049" and ref.stats.channel == "EHZ"
        assert ref.stats.sampling_rate == 100
        assert ref.stats.starttime.timestamp == pytest.approx(expected_start, abs=1e-6)

        ch, start, samples = rv._decode_miniseed(rec)
        assert samples == x[pos:pos + used]
        assert start == pytest.approx(expected_start, abs=1e-6)
        prev, pos, seq = x[pos + used - 1], pos + used, seq + 1


@pytest.fixture
def quake():
    """One triggered quake's motion with the microseism subtracted out."""
    q = rv.DemoQuakes(seed=7)
    q._next_auto = float("inf")
    q.trigger()
    Q = q._quakes[0]
    quiet = rv.DemoQuakes(seed=0)
    quiet._next_auto = float("inf")
    t = np.arange(int(Q["t_p"] * 100) - 100, int(Q["t_p"] * 100) + 6000) / 100
    e, n, z = (a - b for a, b in zip(q.motion(t), quiet.motion(t)))
    radial = np.array([-np.sin(Q["baz"]), -np.cos(Q["baz"])])

    def window(t0, t1):
        m = (t - Q["t_p"] >= t0) & (t - Q["t_p"] < t1)
        return np.column_stack([e[m], n[m], z[m]])
    return Q, radial, window


def test_p_wave_is_linear_along_ray(quake):
    Q, r, window = quake
    sv, vt = principal_axes(window(0, min(2.5, Q["sp"])))
    ray = [r[0] * np.sin(Q["inc"]), r[1] * np.sin(Q["inc"]), np.cos(Q["inc"])]
    assert sv[1] / sv[0] < 0.01
    assert abs(vt[0] @ ray) > 0.999


def test_s_wave_is_linear_and_transverse(quake):
    Q, r, window = quake
    sv, vt = principal_axes(window(Q["sp"], Q["sp"] + 2.5))
    assert sv[1] / sv[0] < 0.05
    assert abs(vt[0] @ [-r[1], r[0], 0]) > 0.95


def test_rayleigh_wave_is_retrograde_ellipse(quake):
    Q, r, window = quake
    t_peak = 1.8 * Q["sp"] + 4.0
    pts = window(t_peak - 1.5, t_peak + 1.5)
    sv, _ = principal_axes(pts)
    radial, up = pts[:, :2] @ r, pts[:, 2]
    assert sv[1] / sv[0] > 0.4                      # elliptical, not linear
    assert np.gradient(radial)[np.argmax(up)] < 0   # moving toward source at top


def test_quake_button_schedules_arrival():
    q = rv.DemoQuakes(seed=1)
    q._next_auto = float("inf")
    q.trigger()
    assert 0 < q.next_arrival() - time.time() <= 1.0


def test_end_to_end_demo_pipeline():
    """Demo server -> SEEDLINK -> decode -> align -> display buffers.

    A triggered quake's P wave, read back from the viewer's own buffers,
    must be linear along the ray. That only holds if every stage (including
    the per-channel delivery delays the server adds) keeps channels in step.
    """
    from PyQt6.QtCore import QEventLoop, QTimer
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    demo = rv.DemoSeedlinkServer(seed=11)
    demo.quakes._next_auto = float("inf")
    demo.start()
    win = rv.MainWindow("127.0.0.1", demo.port, "AM", "R8049", 60, demo=demo)
    try:
        def wait_until(cond, timeout):
            loop, deadline = QEventLoop(), time.time() + timeout
            timer = QTimer()
            timer.timeout.connect(
                lambda: loop.quit() if cond() or time.time() > deadline else None)
            timer.start(20)
            loop.exec()
            timer.stop()
            return cond()

        assert wait_until(lambda: win.aligner._cursor is not None, 15)
        demo.quakes.trigger()
        Q = demo.quakes._quakes[-1]
        p_end = Q["t_p"] + min(2.0, Q["sp"] - 0.2)
        assert wait_until(lambda: win.aligner._cursor / 100 >= p_end + 0.5, 20)

        # Locate the P window in the buffers using the displayed cursor time
        lag = int(round((win.aligner._cursor / 100 - Q["t_p"]) * 100))
        span = int(round((p_end - Q["t_p"]) * 100))
        bufs = {ch: np.array(win.buffers[ch], dtype=float) for ch in rv.CHANNELS}
        physical = {label: ch for ch, label in rv.CHANNEL_LABELS.items()}
        pts = np.column_stack([
            bufs[physical[c]][-lag:-lag + span] for c in ("EHE", "EHN", "EHZ")
        ])
        # Remove offset + slow microseism drift (the 3D view high-passes too)
        tt = np.arange(len(pts))
        pts = pts - np.array([np.polyval(np.polyfit(tt, c, 2), tt) for c in pts.T]).T
        sv, vt = principal_axes(pts)
        r = np.array([-np.sin(Q["baz"]), -np.cos(Q["baz"])])
        ray = [r[0] * np.sin(Q["inc"]), r[1] * np.sin(Q["inc"]), np.cos(Q["inc"])]
        assert abs(vt[0] @ ray) > 0.99
        assert sv[1] / sv[0] < 0.15    # noise remains, but clearly linear
    finally:
        win.close()
