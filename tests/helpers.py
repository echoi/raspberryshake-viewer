import io
import time

import numpy as np


def obspy_records(data, start, channel="EHZ", encoding="STEIM2"):
    """Encode samples with obspy and split into 512-byte MiniSEED records."""
    from obspy import Stream, Trace, UTCDateTime   # test-only dependency
    tr = Trace(np.asarray(data, dtype=np.int32),
               header=dict(network="AM", station="R8049", location="00",
                           channel=channel, sampling_rate=100,
                           starttime=UTCDateTime(start)))
    buf = io.BytesIO()
    Stream([tr]).write(buf, format="MSEED", reclen=512, encoding=encoding)
    raw = buf.getvalue()
    return [raw[i:i + 512] for i in range(0, len(raw), 512)]


def principal_axes(pts):
    """Singular values and principal directions of a centred point cloud."""
    _, sv, vt = np.linalg.svd(pts - pts.mean(axis=0))
    return sv, vt


def wait_until(cond, timeout):
    """Run the Qt event loop until cond() is true or timeout (s) passes."""
    from PyQt6.QtCore import QEventLoop, QTimer
    loop, deadline = QEventLoop(), time.time() + timeout
    timer = QTimer()
    timer.timeout.connect(lambda: loop.quit() if cond() or time.time() > deadline else None)
    timer.start(20)
    loop.exec()
    timer.stop()
    return cond()
