"""Waveform display: peak decimation keeps the shape at a bounded draw cost."""
import numpy as np

import raspberryshake_viewer as rv

decimate = rv.WaveformPanel._peak_decimate


def test_bounded_points_and_spikes_kept():
    rng = np.random.default_rng(0)
    data = rng.normal(0, 10, 6000)
    data[1234] = 5000          # a one-sample spike (a tap) must survive
    data[4321] = -7000
    x, y = decimate(data, 0, columns=860, end_index=6000)
    assert len(y) <= 2 * (860 + 2)
    assert y.max() == 5000 and y.min() == -7000
    assert len(x) == len(y) and np.all(np.diff(x) >= 0)


def test_newest_sample_always_drawn():
    data = np.zeros(6000)
    data[-1] = 999             # arrived this frame: must be on screen now
    for end in range(6000, 6010):
        x, y = decimate(data, 0, columns=860, end_index=end)
        assert y[-2:].max() == 999


def test_bins_stay_fixed_while_scrolling():
    """Same samples -> same bins, whatever has been appended since, so the
    decimated trace doesn't shimmer as it scrolls."""
    rng = np.random.default_rng(1)
    stream = rng.normal(0, 10, 7000)
    def bins_by_abs_index(end):
        x, y = decimate(stream[end - 6000:end], 0, columns=860, end_index=end)
        abs_x = x + (end - 6000)                 # back to absolute sample index
        return dict(zip(abs_x[::2], zip(y[::2], y[1::2])))
    a, b = bins_by_abs_index(6500), bins_by_abs_index(6537)
    common = sorted(set(a) & set(b))[1:-1]       # skip partial bins at both ends
    assert len(common) > 700
    assert all(a[i] == b[i] for i in common)


def test_short_buffers_not_decimated(monkeypatch):
    """Few samples (just started, or after Clear): draw them all."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    panel = rv.WaveformPanel("EHZ", "#00E5FF", 60)
    panel.resize(1000, 190)
    panel.show()
    app.processEvents()
    x, data, _ = panel.prepare_data(list(range(500)), end_index=500)
    assert len(data) == 500
    x, data, _ = panel.prepare_data(list(range(6000)), end_index=6000)
    assert len(data) < 2000
    panel.close()
