import io

import numpy as np
from obspy import Stream, Trace, UTCDateTime


def obspy_records(data, start, channel="EHZ", encoding="STEIM2"):
    """Encode samples with obspy and split into 512-byte MiniSEED records."""
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
