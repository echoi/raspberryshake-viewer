"""UDP transport: Raspberry Shake datacast packets, low-latency display."""
import time

import numpy as np

import raspberryshake_viewer as rv
from helpers import principal_axes, wait_until


def test_parse_packet():
    ch, start, samples = rv._parse_rs_udp(b"{'EHZ', 1557760183.330, 16450, 16466, -3}")
    assert (ch, start, samples) == ("EHZ", 1557760183.330, [16450, 16466, -3])


def test_parse_rejects_malformed():
    for bad in (b"", b"{}", b"{'EHZ'}", b"{'EHZ', notatime, 1}", b"{'EHZ', 1.0, 2.5}",
                b"\xff\xfe"):
        assert rv._parse_rs_udp(bad) == (None, None, [])


def test_listener_ignores_other_channels():
    """A Shake 4D also sends accelerometer channels (EN?); those are skipped."""
    al = rv.ChannelAligner(rv.CHANNELS)
    listener = rv.UdpListener(port=0, aligner=al)
    listener._ingest(b"{'ENZ', 1760000000.0, 1, 2, 3}")
    assert listener.packet_count == 0
    listener._ingest(b"{'EHZ', 1760000000.0, 1, 2, 3}")
    assert listener.packet_count == 1


def _run_udp_window():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    port = rv._free_udp_port()
    demo = rv.DemoUdpSender(port, seed=11)
    demo.quakes._next_auto = float("inf")
    demo.start()
    win = rv.MainWindow("127.0.0.1", None, "AM", "R8049", 60, demo=demo,
                        transport="udp", udp_port=port)
    return app, demo, win


def test_udp_display_delay_under_one_second():
    """The point of UDP: SEEDLINK's display delay is a whole record (5-7 s on a
    quiet Shake); with 0.25 s UDP packets it must stay under a second."""
    app, demo, win = _run_udp_window()
    try:
        assert wait_until(lambda: win.aligner._cursor is not None, 10)
        delays = []
        def sample():
            delays.append(time.time() - win.aligner._cursor / rv.SAMPLE_RATE)
            return len(delays) >= 40      # ~8 s of readings, 0.2 s apart
        deadline = time.time() + 15
        while not sample() and time.time() < deadline:
            wait_until(lambda: False, 0.2)
        assert max(delays[10:]) < 1.0, f"display delay up to {max(delays[10:]):.2f} s"
        assert win.listener._connected and win.listener.transport_name == "UDP"
    finally:
        win.close()


def test_udp_end_to_end_alignment():
    """Over UDP, a triggered quake's P wave read back from the display buffers
    is still linear along its ray (channels stay sample-aligned)."""
    app, demo, win = _run_udp_window()
    try:
        assert wait_until(lambda: win.aligner._cursor is not None, 10)
        demo.quakes.trigger()
        Q = demo.quakes._quakes[-1]
        p_end = Q["t_p"] + min(2.0, Q["sp"] - 0.2)
        assert wait_until(lambda: win.aligner._cursor / 100 >= p_end + 0.5, 15)

        lag = int(round((win.aligner._cursor / 100 - Q["t_p"]) * 100))
        span = int(round((p_end - Q["t_p"]) * 100))
        bufs = {ch: np.array(win.buffers[ch], dtype=float) for ch in rv.CHANNELS}
        physical = {label: ch for ch, label in rv.CHANNEL_LABELS.items()}
        pts = np.column_stack([
            bufs[physical[c]][-lag:-lag + span] for c in ("EHE", "EHN", "EHZ")
        ])
        tt = np.arange(len(pts))
        pts = pts - np.array([np.polyval(np.polyfit(tt, c, 2), tt) for c in pts.T]).T
        sv, vt = principal_axes(pts)
        r = np.array([-np.sin(Q["baz"]), -np.cos(Q["baz"])])
        ray = [r[0] * np.sin(Q["inc"]), r[1] * np.sin(Q["inc"]), np.cos(Q["inc"])]
        assert abs(vt[0] @ ray) > 0.99
        assert sv[1] / sv[0] < 0.15
    finally:
        win.close()


def test_display_keeps_real_time_when_drawing_is_slow():
    """Regression: on a slow machine, painting starved the 25 Hz data tick and
    the display consumed fewer than 100 samples/s, so the delay grew without
    bound (25 s after 75 s on real hardware). Simulate heavy painting by
    blocking the event loop most of the time; the delay must stay low."""
    from PyQt6.QtCore import QTimer
    app, demo, win = _run_udp_window()
    hog = QTimer()
    hog.timeout.connect(lambda: time.sleep(0.06))   # 60 ms busy of every 70 ms
    try:
        assert wait_until(lambda: win.aligner._cursor is not None, 10)
        hog.start(70)
        wait_until(lambda: False, 10)
        delay = time.time() - win.aligner._cursor / rv.SAMPLE_RATE
        assert delay < 1.5, f"display fell {delay:.1f} s behind under slow drawing"
    finally:
        hog.stop()
        win.close()
