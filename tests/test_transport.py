"""Default transport: UDP, falling back to SEEDLINK while no UDP data arrives."""
import time

import pytest

import raspberryshake_viewer as rv
from helpers import wait_until


@pytest.fixture(autouse=True)
def quick_fallback(monkeypatch):
    """Shorten the 5 s fallback timer so these tests run in seconds."""
    monkeypatch.setattr(rv, "UDP_FALLBACK_SECS", 2)


@pytest.fixture
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def display_delay(win):
    return time.time() - win.aligner._cursor / rv.SAMPLE_RATE


def test_probe_does_not_feed_aligner():
    al = rv.ChannelAligner(rv.CHANNELS)
    probe = rv.UdpListener(port=0, aligner=al, probe=True)
    probe._ingest(b"{'EHZ', 1760000000.0, 1, 2, 3}")
    assert probe.packet_count == 1
    assert al._start["EHZ"] is None      # nothing pushed


def test_auto_falls_back_to_seedlink_when_no_udp(app):
    """Shake not set up for UDP: the viewer must still show data, via SEEDLINK."""
    demo = rv.DemoShake(seedlink=True, udp_port=None, seed=1)   # SEEDLINK only
    demo.start()
    win = rv.MainWindow("127.0.0.1", demo.port, "AM", "R8049", 60, demo=demo,
                        transport="auto", udp_port=rv._free_udp_port())
    try:
        assert win.active_transport == "udp"                     # tries UDP first
        assert wait_until(lambda: win.active_transport == "seedlink", 5)
        assert win._probe is not None                            # still listening for UDP
        assert wait_until(lambda: win.aligner._cursor is not None, 10)
        assert win.banner.isVisibleTo(win) and "No UDP data" in win.banner.text()
    finally:
        win.close()


def test_auto_prefers_udp_and_recovers_both_ways(app):
    """UDP present -> stays on UDP. UDP stops -> SEEDLINK. UDP returns -> UDP,
    with the low delay back."""
    udp_port = rv._free_udp_port()
    demo = rv.DemoShake(seedlink=True, udp_port=udp_port, seed=2)
    demo.start()
    win = rv.MainWindow("127.0.0.1", demo.port, "AM", "R8049", 60, demo=demo,
                        transport="auto", udp_port=udp_port)
    try:
        # 1. Stays on UDP well past the fallback timer, at low delay
        assert wait_until(lambda: win.aligner._cursor is not None, 5)
        assert not wait_until(lambda: win.active_transport != "udp", 4)
        assert display_delay(win) < 1.0

        # 2. UDP goes quiet -> SEEDLINK
        demo.udp.stop()
        assert wait_until(lambda: win.active_transport == "seedlink", 5)
        assert wait_until(lambda: win.aligner._cursor is not None, 10)

        # 3. UDP comes back -> switch back to UDP, low delay again
        demo.udp = rv.DemoUdpSender(udp_port, quakes=demo.quakes)
        demo.udp.start()
        assert wait_until(lambda: win.active_transport == "udp", 3)
        assert win._probe is None and win.banner.notice == ""
        assert wait_until(lambda: win.aligner._cursor is not None
                          and display_delay(win) < 1.0, 5)
    finally:
        win.close()


def test_udp_only_never_falls_back(app):
    win = rv.MainWindow("127.0.0.1", None, "AM", "R8049", 60,
                        transport="udp", udp_port=rv._free_udp_port())
    try:
        assert not wait_until(lambda: win.active_transport != "udp", 4)
        assert win._probe is None
    finally:
        win.close()


def test_seedlink_only_mode(app):
    demo = rv.DemoShake(seedlink=True, udp_port=None, seed=3)
    demo.start()
    win = rv.MainWindow("127.0.0.1", demo.port, "AM", "R8049", 60, demo=demo,
                        transport="seedlink", udp_port=rv._free_udp_port())
    try:
        assert win.active_transport == "seedlink" and win._probe is None
        assert wait_until(lambda: win.aligner._cursor is not None, 10)
        assert win.banner.notice == ""
    finally:
        win.close()
