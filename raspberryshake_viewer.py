#!/usr/bin/env python3
"""
Raspberry Shake Real-Time Waveform Viewer  (SEEDLINK edition)
=============================================================
Displays live EHZ, EHN, EHE channels from a Raspberry Shake via SEEDLINK.

Features:
  * Auto-discovers RS via rs.local / 192.168.1.2 / 192.168.1.3
  * SSH clock-sync pre-flight: checks and fixes RS date before starting
  * 3-channel scrolling waveform with per-channel and global amplitude controls
  * SEEDLINK TCP stream -- reliable, ordered, no RS-side UDP config needed
  * Auto-reconnect with on-screen amber warning banner when connection drops
  * Channels time-aligned from MiniSEED record timestamps
  * Rotatable 3D particle-motion view (needs PyOpenGL)
  * --demo mode: built-in synthetic Shake served over local SEEDLINK

Requirements:
    pip install PyQt6 pyqtgraph numpy paramiko PyOpenGL

Run:
    python raspberryshake_viewer.py

Optional CLI args:
    --rs-host       RS hostname/IP        (default: auto-detect)
    --port          SEEDLINK port         (default: 18000)
    --window        Seconds of data       (default: 60)
    --network       SEEDLINK network code (default: AM)
    --station       SEEDLINK station code (default: R8049)
    --ssh-user      RS SSH username       (default: myshake)
    --ssh-pass      RS SSH password       (default: earthday2023)
    --skip-preflight  Skip discovery and clock-sync dialog
    --demo          Simulate a Raspberry Shake (synthetic quakes, no hardware)
"""

import sys
import argparse
import socket
import threading
import time
import datetime
from collections import deque

import numpy as np
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QSizePolicy, QStatusBar,
    QGroupBox, QDialog, QTextEdit, QDialogButtonBox, QProgressBar,
    QComboBox, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread, QEvent
from PyQt6.QtGui import QFont, QColor, QPalette
import pyqtgraph as pg


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RS_CANDIDATE_HOSTS  = ["192.168.1.2", "192.168.1.3", "rs.local"]  # IPs first — avoids slow mDNS on Windows
RS_SEEDLINK_PORT    = 18000
RS_SSH_PORT         = 22
RS_SSH_USER_DEFAULT = "myshake"
RS_SSH_PASS_DEFAULT = "earthday2023"
RS_NETWORK_DEFAULT  = "AM"
RS_STATION_DEFAULT  = "R8049"   # your station code from AM_R8049
RS_LOCATION_CODE    = "00"      # location code used in SELECT command
MAX_CLOCK_DIFF_SECS = 60
RECONNECT_DELAY     = 5     # seconds between reconnect attempts

CHANNELS = ["EHZ", "EHN", "EHE"]
CHANNEL_COLORS = {
    "EHZ": "#00E5FF",   # cyan   - vertical
    "EHN": "#FF6B35",   # orange - physically East (sensor labelled N on board)
    "EHE": "#69FF47",   # green  - physically North (sensor labelled E on board)
}

# Physical label shown in the UI — swapped to reflect true ground motion direction
CHANNEL_LABELS = {
    "EHZ": "EHZ",
    "EHN": "EHE",   # board says N, actually measures East motion
    "EHE": "EHN",   # board says E, actually measures North motion
}
SAMPLE_RATE     = 100
BUFFER_FACTOR   = 2
DEFAULT_Y_RANGE = 1e9   # default shared amplitude range (+/- counts)
SCALE_WINDOW    = 5     # seconds of recent data used for auto Y-range


# ---------------------------------------------------------------------------
# Pre-flight worker
# ---------------------------------------------------------------------------

class PreflightWorker(QObject):
    log      = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, ssh_user, ssh_pass):
        super().__init__()
        self.ssh_user = ssh_user
        self.ssh_pass = ssh_pass

    def run(self):
        rs_host = self._discover()
        if rs_host is None:
            self.finished.emit(False, "RS not found on network")
            return
        ok, result = self._sync_clock(rs_host)
        self.finished.emit(ok, rs_host if ok else result)

    def _try_port(self, host, port, timeout=2.0):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            r = s.connect_ex((host, port))
            s.close()
            return r == 0
        except Exception:
            return False

    def _probe_parallel(self, hosts, port, timeout=2.0):
        """
        Probe all hosts simultaneously in threads.
        Returns the first host that responds, or None.
        Numeric IPs resolve instantly; rs.local may be slow on Windows
        but runs concurrently so it does not block the IP probes.
        """
        import queue
        result_q = queue.Queue()

        def probe(host):
            if self._try_port(host, port, timeout):
                result_q.put(host)

        threads = [threading.Thread(target=probe, args=(h,), daemon=True)
                   for h in hosts]
        for t in threads:
            t.start()

        # Wait up to timeout + 0.5 s for any thread to report success
        try:
            return result_q.get(timeout=timeout + 0.5)
        except queue.Empty:
            return None

    def _discover(self):
        self.log.emit(
            "Searching for Raspberry Shake "
            f"({', '.join(RS_CANDIDATE_HOSTS)})..."
        )

        # Pass 1: probe SEEDLINK port on all hosts in parallel
        host = self._probe_parallel(RS_CANDIDATE_HOSTS, RS_SEEDLINK_PORT)
        if host:
            self.log.emit(
                f"<span style='color:#69FF47'>"
                f"Found RS at <b>{host}</b> (SEEDLINK port responded)</span>"
            )
            return host

        # Pass 2: fallback — probe SSH port in parallel
        self.log.emit(
            "    SEEDLINK port not found -- trying SSH port as fallback..."
        )
        host = self._probe_parallel(RS_CANDIDATE_HOSTS, RS_SSH_PORT)
        if host:
            self.log.emit(
                f"<span style='color:#69FF47'>"
                f"Found RS at <b>{host}</b> (SSH port responded)</span>"
            )
            return host

        self.log.emit(
            "<span style='color:#FF6B35'>"
            "No Raspberry Shake found. Check cables and power.</span>"
        )
        return None

    def _sync_clock(self, host):
        try:
            import paramiko
        except ImportError:
            self.log.emit(
                "<span style='color:#FFB347'>"
                "paramiko not installed -- skipping clock check.<br>"
                "Install with: <b>pip install paramiko</b></span>"
            )
            return True, host

        self.log.emit(f"<br>Connecting via SSH to check RS clock ({host})...")
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(host, port=RS_SSH_PORT,
                           username=self.ssh_user, password=self.ssh_pass,
                           timeout=8, banner_timeout=8)
        except Exception as e:
            self.log.emit(
                f"<span style='color:#FFB347'>SSH connect failed: {e}<br>"
                "Skipping clock sync -- SEEDLINK may still work.</span>"
            )
            return True, host

        try:
            _, stdout, _ = client.exec_command("date -u +'%Y-%m-%d %H:%M:%S'")
            rs_dt = datetime.datetime.strptime(
                stdout.read().decode().strip(), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=datetime.timezone.utc)
        except Exception as e:
            self.log.emit(f"<span style='color:#FFB347'>Could not read RS clock: {e}</span>")
            client.close()
            return True, host

        laptop_dt = datetime.datetime.now(datetime.timezone.utc)
        diff = abs((laptop_dt - rs_dt).total_seconds())

        self.log.emit(f"    Laptop UTC : {laptop_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        self.log.emit(f"    RS UTC     : {rs_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        self.log.emit(f"    Difference : {diff:.1f} s")

        if diff <= MAX_CLOCK_DIFF_SECS:
            self.log.emit("<span style='color:#69FF47'>Clocks are in sync.</span>")
            client.close()
            return True, host

        self.log.emit(
            f"<span style='color:#FFB347'>Clock drift is {diff:.0f} s -- fixing...</span>"
        )
        new_time = laptop_dt.strftime("%d %b %Y %H:%M:%S")
        try:
            stdin, stdout, stderr = client.exec_command(
                f'sudo date --set "{new_time}"', get_pty=True
            )
            stdin.write(self.ssh_pass + "\n")
            stdin.flush()
            out = stdout.read().decode().strip()
            if out:
                self.log.emit(f"    RS: {out}")
            self.log.emit("<span style='color:#69FF47'>RS clock updated.</span>")
        except Exception as e:
            self.log.emit(f"<span style='color:#FF6B35'>Failed to set clock: {e}</span>")

        client.close()
        return True, host


# ---------------------------------------------------------------------------
# Pre-flight dialog
# ---------------------------------------------------------------------------

class PreflightDialog(QDialog):
    def __init__(self, ssh_user, ssh_pass, parent=None):
        super().__init__(parent)
        self.rs_host  = None
        self.ssh_user = ssh_user
        self.ssh_pass = ssh_pass
        self.setWindowTitle("Raspberry Shake -- Pre-flight Check")
        self.setMinimumSize(560, 340)
        self.setModal(True)
        self.setStyleSheet("""
            QDialog   { background:#0D1117; color:#E6EDF3; }
            QTextEdit { background:#0D1117; color:#9CA3AF;
                        border:1px solid #2D333B; border-radius:4px;
                        font-family:'Courier New'; font-size:10px; }
            QLabel    { background:transparent; }
        """)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Setting up Raspberry Shake connection...")
        title.setFont(QFont("Georgia", 12, QFont.Weight.Bold))
        title.setStyleSheet("color:#E6EDF3;")
        layout.addWidget(title)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, stretch=1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(4)
        self.progress.setStyleSheet("""
            QProgressBar { background:#1C2128; border:none; border-radius:2px; }
            QProgressBar::chunk { background:#00E5FF; border-radius:2px; }
        """)
        layout.addWidget(self.progress)

        btn_box = QDialogButtonBox()
        skip = btn_box.addButton("Skip & Continue", QDialogButtonBox.ButtonRole.AcceptRole)
        skip.setStyleSheet("""
            QPushButton { background:#1C2128; color:#888; border:1px solid #2D333B;
                          border-radius:3px; padding:4px 14px; }
            QPushButton:hover { background:#2D333B; color:#DDD; }
        """)
        skip.clicked.connect(self._on_skip)
        layout.addWidget(btn_box)

        self.thread = QThread()
        self.worker = PreflightWorker(self.ssh_user, self.ssh_pass)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log_view.append)
        self.worker.finished.connect(self._on_finished)
        self.thread.start()

    def _on_finished(self, success, host_or_err):
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.thread.quit()

        if success:
            self.rs_host = host_or_err
            # Only auto-close if there were no warnings in the log.
            # If SSH failed or steps were skipped, keep the dialog open
            # so the user can read what happened, then click Continue.
            log_text = self.log_view.toPlainText().lower()
            has_warnings = any(w in log_text for w in [
                "failed", "skipping", "not installed", "could not", "error"
            ])
            if has_warnings:
                self.log_view.append(
                    "<br><b style='color:#FFB347'>Completed with warnings.</b>"
                    "  Review the messages above, then click <b>Continue</b>."
                )
                self._show_continue_button()
            else:
                self.log_view.append(
                    "<br><b style='color:#69FF47'>All good!</b>"
                    "  Starting waveform display..."
                )
                QTimer.singleShot(1400, self.accept)
        else:
            self.log_view.append(
                f"<br><b style='color:#FF6B35'>Error:</b> {host_or_err}"
                "<br>Check connections, then click <b>Skip &amp; Continue</b>."
            )

    def _show_continue_button(self):
        """Replace the Skip button label with Continue and restyle it green."""
        for btn in self.findChildren(QPushButton):
            if "Skip" in btn.text():
                btn.setText("Continue")
                btn.setStyleSheet("""
                    QPushButton { background:#1C4A1C; color:#69FF47;
                                  border:1px solid #2D6B2D;
                                  border-radius:3px; padding:4px 18px;
                                  font-weight:bold; }
                    QPushButton:hover { background:#2D6B2D; color:#9FFF9F; }
                """)
                break

    def _on_skip(self):
        if self.thread.isRunning():
            self.thread.quit()
        self.rs_host = None
        self.accept()


# ---------------------------------------------------------------------------
# SEEDLINK listener  (raw TCP — no external library required)
# ---------------------------------------------------------------------------
#
# SEEDLINK protocol summary:
#   Client sends ASCII commands, server replies with "OK" or "ERROR".
#   After "END" command the server streams 512-byte MiniSEED packets,
#   each prefixed with an 8-byte SEEDLINK header "SL" + 6-byte sequence.
#   The MiniSEED payload starts at byte 8 and is always 512 bytes long.
#
# Commands used:
#   HELLO                       - get server ID (sanity check)
#   SELECT EH?                  - subscribe to all EH* channels (wildcard)
#   DATA                        - request real-time data (no time range)
#   END                         - start streaming
# ---------------------------------------------------------------------------

# Minimal Steim-1 and Steim-2 decoder + MiniSEED header parser
# (avoids obspy / seedlinkrecord dependency entirely)

import struct as _struct

def _decode_miniseed(raw):
    """
    Parse a 512-byte MiniSEED record.
    Returns (channel_code: str, start_time: float, samples: list[int]),
    or (None, None, []) on error. start_time is UTC epoch seconds.
    Handles Steim-1 (encoding 10) and Steim-2 (encoding 11).

    MiniSEED fixed header layout (big-endian):
      0- 5  sequence number (ASCII)
      6      data header/quality indicator
      7      reserved
      8-12   station (5 chars)
      13-14  location (2 chars)
      15-17  channel (3 chars)
      18-19  network (2 chars)
      20-29  start time (BTIME: year, day-of-year, h, m, s, -, 0.0001 s)
      30-31  num_samples (uint16)
      32-33  sample rate factor (int16)
      34-35  sample rate multiplier (int16)
      36     activity flags  (bit 0x02 = time correction applied)
      37     IO flags
      38     data quality flags
      39     num_blockettes
      40-43  time correction (int32, 0.0001 s)
      44-45  data offset (uint16)  <-- byte offset to first data sample
      46-47  first blockette offset (uint16)
    """
    try:
        # Confirmed correct offsets from live RS packet hex dump:
        #   station [8:13], location [13:15], channel [15:18], network [18:20]
        #   num_samp [30:32], data_off [44:46], blkt_off [46:48]
        channel  = raw[15:18].decode("ascii").strip()
        num_samp = _struct.unpack_from(">H", raw, 30)[0]
        data_off = _struct.unpack_from(">H", raw, 44)[0]
        blkt_off = _struct.unpack_from(">H", raw, 46)[0]

        year, doy, hh, mm, ss, _, frac = _struct.unpack_from(">HHBBBBH", raw, 20)
        start = (
            datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc)
            + datetime.timedelta(days=doy - 1, hours=hh, minutes=mm,
                                 seconds=ss, microseconds=frac * 100)
        ).timestamp()
        if not raw[36] & 0x02:   # correction not yet applied to start time
            start += _struct.unpack_from(">i", raw, 40)[0] * 1e-4

        # Default to Steim-2 (confirmed encoding=11 on RS)
        encoding = 11

        # Walk blockette chain to confirm encoding from Blockette 1000
        off = blkt_off
        for _ in range(10):
            if off == 0 or off + 8 > 512:
                break
            blkt_type = _struct.unpack_from(">H", raw, off)[0]
            next_blkt = _struct.unpack_from(">H", raw, off + 2)[0]
            if blkt_type == 1000:
                encoding = raw[off + 4]
            elif blkt_type == 1001:   # extra microseconds of start time
                start += _struct.unpack_from(">b", raw, off + 5)[0] * 1e-6
            if next_blkt == 0 or next_blkt == off:
                break
            off = next_blkt

        if num_samp == 0 or data_off == 0 or data_off >= 512:
            return None, None, []

        payload = raw[data_off:]

        if encoding == 10:
            samples = _decode_steim1(payload, num_samp)
        elif encoding == 11:
            samples = _decode_steim2(payload, num_samp)
        else:
            return None, None, []

        return channel, start, samples
    except Exception:
        return None, None, []


def _decode_steim1(data, num_samp):
    """Decode Steim-1 compressed data."""
    samples = []
    x0 = xn = None
    frames = len(data) // 64
    for f in range(frames):
        frame = data[f*64:(f+1)*64]
        ctrl  = _struct.unpack_from(">I", frame, 0)[0]
        for w in range(1, 16):   # word 0 is the control word itself
            code = (ctrl >> (30 - 2*w)) & 0x3
            word = _struct.unpack_from(">i", frame, w*4)[0]
            if f == 0 and w == 1:
                x0 = word
                continue
            if f == 0 and w == 2:
                xn = word
                continue
            if code == 0:
                continue
            elif code == 1:   # 4 x 8-bit
                diffs = [(word >> (24-8*i)) & 0xFF for i in range(4)]
                diffs = [d if d < 128 else d - 256 for d in diffs]
                samples.extend(diffs)
            elif code == 2:   # 2 x 16-bit
                diffs = [((word >> 16) & 0xFFFF), word & 0xFFFF]
                diffs = [d if d < 32768 else d - 65536 for d in diffs]
                samples.extend(diffs)
            elif code == 3:   # 1 x 32-bit
                samples.append(word)

    return _integrate(x0, xn, samples, num_samp)


def _integrate(x0, xn, diffs, num_samp):
    """
    Rebuild samples from Steim differences. diffs[0] links to the previous
    record, so x0 is the first sample and integration starts at diffs[1].
    The record is dropped if the result does not end at xn (corrupt data).
    """
    if x0 is None or len(diffs) < num_samp:
        return []
    out = [x0]
    val = x0
    for d in diffs[1:num_samp]:
        val += d
        out.append(val)
    if val != xn:
        return []
    return out


def _sign_extend(value, bits):
    """Sign-extend a value from `bits` width to Python int."""
    sign_bit = 1 << (bits - 1)
    return (value & (sign_bit - 1)) - (value & sign_bit)


def _decode_steim2(data, num_samp):
    """
    Decode Steim-2 compressed data.
    Uses Python arbitrary-precision ints throughout to avoid overflow.
    """
    samples = []
    x0 = xn = None
    frames  = len(data) // 64

    for f in range(frames):
        frame = data[f*64:(f+1)*64]
        ctrl  = _struct.unpack_from(">I", frame, 0)[0]  # control word

        for w in range(1, 16):   # word 0 is the control word itself
            code = (ctrl >> (30 - 2*w)) & 0x3
            word = _struct.unpack_from(">I", frame, w*4)[0]  # unsigned 32-bit

            # Frame 0 words 1/2 hold x0 (forward integration constant) and xn
            if f == 0 and w == 1:
                x0 = _sign_extend(word, 32)
                continue
            if f == 0 and w == 2:
                xn = _sign_extend(word, 32)   # last sample — integrity check
                continue

            if code == 0:
                continue   # unused word

            elif code == 1:
                # 4 differences, 8 bits each
                for i in range(4):
                    samples.append(_sign_extend((word >> (24 - 8*i)) & 0xFF, 8))

            elif code == 2:
                dnib = (word >> 30) & 0x3
                if dnib == 1:
                    # 1 difference, 30 bits
                    samples.append(_sign_extend(word & 0x3FFFFFFF, 30))
                elif dnib == 2:
                    # 2 differences, 15 bits each
                    samples.append(_sign_extend((word >> 15) & 0x7FFF, 15))
                    samples.append(_sign_extend(word & 0x7FFF, 15))
                elif dnib == 3:
                    # 3 differences, 10 bits each
                    for i in range(3):
                        samples.append(_sign_extend((word >> (20 - 10*i)) & 0x3FF, 10))

            elif code == 3:
                dnib = (word >> 30) & 0x3
                if dnib == 0:
                    # 5 differences, 6 bits each
                    for i in range(5):
                        samples.append(_sign_extend((word >> (24 - 6*i)) & 0x3F, 6))
                elif dnib == 1:
                    # 6 differences, 5 bits each
                    for i in range(6):
                        samples.append(_sign_extend((word >> (25 - 5*i)) & 0x1F, 5))
                elif dnib == 2:
                    # 7 differences, 4 bits each (low 28 bits)
                    for i in range(7):
                        samples.append(_sign_extend((word >> (24 - 4*i)) & 0xF, 4))

    return _integrate(x0, xn, samples, num_samp)


class ChannelAligner:
    """
    Time-aligns the three channels using each record's start time.

    Samples are keyed by absolute sample index (epoch seconds x SAMPLE_RATE),
    so the display always pulls the *same instant* from every channel, no
    matter in what order or how late each channel's records arrive.
    Correct alignment matters for particle motion: a 0.1 s inter-channel
    offset on a 5 Hz wave turns linear motion into a spurious ellipse.

    The SEEDLINK thread calls push(); the display timer calls pull().
    """

    MAX_GAP_FILL   = 10 * SAMPLE_RATE   # bridge gaps up to 10 s with last value
    MAX_BACKLOG    = 30 * SAMPLE_RATE   # beyond this, jump ahead to recent data
    CATCHUP_PERIOD = 10 * SAMPLE_RATE   # samples between latency re-evaluations
    LATENCY_MARGIN = SAMPLE_RATE // 5   # keep 0.2 s of jitter headroom

    def __init__(self, channels):
        self.channels = list(channels)
        self._lock = threading.Lock()
        self._has_pulled = False   # survives reset(): resume keeps scrolling
        self.reset()

    def reset(self):
        with self._lock:
            self._data  = {ch: deque() for ch in self.channels}
            self._start = {ch: None for ch in self.channels}  # index of _data[ch][0]
            self._last  = {ch: 0 for ch in self.channels}     # last value pulled
            self._cursor = None          # next sample index to display
            self._catchup = 0            # surplus samples to drain early
            self._min_backlog = None     # lowest backlog seen this period
            self._period_left = self.CATCHUP_PERIOD

    def push(self, ch, start_time, samples):
        n0 = int(round(start_time * SAMPLE_RATE))
        with self._lock:
            data = self._data[ch]
            if self._start[ch] is None:
                self._start[ch] = n0
                data.extend(samples)
                return
            expected = self._start[ch] + len(data)
            delta = n0 - expected
            if abs(delta) <= 1:            # contiguous (allow rounding jitter)
                data.extend(samples)
            elif delta < 0:                # overlap / duplicate: keep new part
                data.extend(samples[-delta:])
            elif delta <= self.MAX_GAP_FILL:
                fill = data[-1] if data else self._last[ch]
                data.extend([fill] * delta)
                data.extend(samples)
            else:                          # long gap: restart this channel
                data.clear()
                self._start[ch] = n0
                data.extend(samples)

    def _backlog(self):
        """Samples available from the cursor on the slowest channel.
        Only valid after _seek(), when every channel starts at the cursor."""
        return min(len(self._data[ch]) for ch in self.channels)

    def pull(self, n):
        """
        Return {ch: list} of n samples per channel (none until data first
        arrives), all for the same instants. When the slowest channel has no data yet, every channel
        repeats its last value together (display latency grows instead of
        channels drifting apart); surplus latency is drained later.
        """
        with self._lock:
            if self._cursor is None:
                if any(s is None for s in self._start.values()):
                    # Before any data ever arrived, draw nothing (avoids a
                    # fake jump from 0 to the sensor offset); after a resume,
                    # keep scrolling with the last values.
                    n_hold = n if self._has_pulled else 0
                    return {ch: [self._last[ch]] * n_hold for ch in self.channels}
                self._cursor = max(self._start.values())

            # Drop data older than the cursor. If a channel restarted ahead of
            # the cursor (long gap), skip all channels past the hole.
            self._seek(max(self._cursor, *self._start.values()))

            backlog = self._backlog()
            if backlog > self.MAX_BACKLOG:
                self._seek(self._cursor + backlog - self.LATENCY_MARGIN)
                backlog = self._backlog()

            # Track the minimum backlog; anything above the margin is excess
            # latency that can be drained by pulling one extra sample per tick.
            if self._min_backlog is None or backlog < self._min_backlog:
                self._min_backlog = backlog
            self._period_left -= n
            if self._period_left <= 0:
                self._catchup = max(0, self._min_backlog - self.LATENCY_MARGIN)
                self._min_backlog = None
                self._period_left = self.CATCHUP_PERIOD

            # Catch up gently: skip at most one sample per pull (~25 sps)
            if self._catchup > 0 and backlog > n:
                self._advance()
                self._catchup -= 1

            out = {ch: [] for ch in self.channels}
            for _ in range(n):
                if self._backlog() > 0:
                    self._advance()
                for ch in self.channels:
                    out[ch].append(self._last[ch])
            return out

    def _seek(self, cursor):
        """Move the cursor forward, discarding every channel's older samples."""
        for ch in self.channels:
            data = self._data[ch]
            for _ in range(min(max(0, cursor - self._start[ch]), len(data))):
                self._last[ch] = data.popleft()
            self._start[ch] = max(self._start[ch], cursor)
        self._cursor = cursor

    def _advance(self):
        """Consume the sample at the cursor from every channel."""
        self._has_pulled = True
        for ch in self.channels:
            self._last[ch] = self._data[ch].popleft()
            self._start[ch] += 1
        self._cursor += 1


class SeedlinkSignals(QObject):
    status_changed     = pyqtSignal(str)
    connection_changed = pyqtSignal(bool)


class SeedlinkListener(threading.Thread):
    """
    Raw TCP SEEDLINK client. No external library needed.
    Speaks the SEEDLINK protocol directly, decodes MiniSEED in-process.
    Auto-reconnects with amber banner on disconnect.
    """

    HEADER_LEN  = 8    # "SL" + 6-byte sequence number
    RECORD_LEN  = 512  # fixed MiniSEED record size

    def __init__(self, host, port, network, station, location, buffers, aligner, window_secs):
        super().__init__(daemon=True)
        self.host         = host
        self.port         = port
        self.network      = network
        self.station      = station
        self.location     = location   # e.g. "00"
        self.buffers      = buffers
        self.aligner      = aligner       # ChannelAligner drained by display timer
        self.maxlen       = window_secs * SAMPLE_RATE * BUFFER_FACTOR
        self.running      = True
        self.signals      = SeedlinkSignals()
        self.last_packet  = {ch: None for ch in CHANNELS}
        self.packet_count = 0
        self._connected   = False
        self._sock        = None

    def run(self):
        while self.running:
            try:
                self._connect_and_stream()
            except Exception as e:
                if self.running:
                    self._set_connected(False)
                    self.signals.status_changed.emit(
                        f"SEEDLINK disconnected: {e} "
                        f"-- retrying in {RECONNECT_DELAY}s..."
                    )
                    time.sleep(RECONNECT_DELAY)

    def _send_cmd(self, sock, cmd):
        """Send a SEEDLINK command and return the server reply line."""
        sock.sendall((cmd + "\r\n").encode())
        reply = b""
        while b"\r" not in reply and b"\n" not in reply:
            chunk = sock.recv(64)
            if not chunk:
                raise ConnectionError("Server closed connection during handshake")
            reply += chunk
        return reply.decode(errors="ignore").strip()

    def _connect_and_stream(self):
        self.signals.status_changed.emit(
            f"SEEDLINK -- connecting to {self.host}:{self.port}..."
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((self.host, self.port))
        self._sock = sock

        # ── Handshake ──────────────────────────────────────────────────
        hello = self._send_cmd(sock, "HELLO")
        self.signals.status_changed.emit(
            f"SEEDLINK server: {hello.splitlines()[0][:60]}"
        )

        # Handshake using exact station + location + channel as confirmed:
        #   STATION R8049 AM  ->  SELECT 00EH?  ->  DATA  ->  END
        reply = self._send_cmd(sock, f"STATION {self.station} {self.network}")
        if "ERROR" in reply.upper():
            raise ConnectionError(
                f"STATION {self.station} {self.network} rejected: {reply}"
            )

        selector = f"{self.location}EH?"   # e.g. "00EH?"
        reply = self._send_cmd(sock, f"SELECT {selector}")
        if "ERROR" in reply.upper():
            raise ConnectionError(f"SELECT {selector} rejected: {reply}")

        self._send_cmd(sock, "DATA")
        sock.sendall(b"END\r\n")

        # Switch to non-blocking stream reads
        sock.settimeout(15)
        self._set_connected(True)
        self.signals.status_changed.emit(
            f"SEEDLINK LIVE -- streaming from {self.host}:{self.port}"
        )

        # ── Streaming loop ────────────────────────────────────────────
        buf = b""
        while self.running:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Server closed stream")
            buf += chunk

            # Each packet = 8-byte SL header + 512-byte MiniSEED record
            while len(buf) >= self.HEADER_LEN + self.RECORD_LEN:
                header = buf[:self.HEADER_LEN]
                if header[:2] != b"SL":
                    # Re-sync: scan forward for next "SL" marker
                    idx = buf.find(b"SL", 1)
                    if idx == -1:
                        buf = b""
                    else:
                        buf = buf[idx:]
                    break
                record = buf[self.HEADER_LEN: self.HEADER_LEN + self.RECORD_LEN]
                buf    = buf[self.HEADER_LEN + self.RECORD_LEN:]
                self._ingest(record)

        sock.close()

    def _ingest(self, raw):
        if not self.running:   # stopped listener must not feed a reset aligner
            return
        ch, start, samples = _decode_miniseed(raw)
        if ch is None or not samples:
            return
        ch_key = ch[-3:] if len(ch) >= 3 else ch
        if ch_key not in self.buffers:
            return
        # Align by record start time — display timer drains at fixed rate
        self.aligner.push(ch_key, start, samples)
        prev = self.packet_count
        self.last_packet[ch_key]  = time.time()
        self.packet_count        += 1
        if prev == 0:
            self.signals.status_changed.emit(
                f"SEEDLINK LIVE -- first record: {ch}  ({len(samples)} samples)"
            )

    def _set_connected(self, state):
        if state != self._connected:
            self._connected = state
            self.signals.connection_changed.emit(state)

    def stop(self):
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Demo mode: synthetic Raspberry Shake served over a local SEEDLINK socket
# ---------------------------------------------------------------------------
#
# --demo runs a fake SEEDLINK server in-process and points the viewer at it,
# so the real pipeline is exercised end to end: TCP handshake, SL framing,
# Steim-2 MiniSEED decoding, timestamp alignment, display, particle motion.
# ---------------------------------------------------------------------------

# Steim-2 packings, densest first: (count, bits, code, dnib)
_STEIM2_PACKINGS = [
    (7, 4, 3, 2), (6, 5, 3, 1), (5, 6, 3, 0),
    (4, 8, 1, None),
    (3, 10, 2, 3), (2, 15, 2, 2), (1, 30, 2, 1),
]
_STEIM2_DATA_WORDS = 13 + 6 * 15   # 7 frames; frame 0 spends 2 words on x0/xn


def _encode_steim2_record(seq, network, station, location, channel,
                          start_idx, samples, prev):
    """
    Pack as many samples as fit into one 512-byte Steim-2 MiniSEED record.
    start_idx is the absolute sample index (epoch seconds x SAMPLE_RATE) of
    samples[0]; prev is the last sample of the previous record (or None).
    Returns (record_bytes, samples_used).
    """
    diffs = [samples[0] - (samples[0] if prev is None else prev)]
    diffs += [samples[i] - samples[i - 1] for i in range(1, len(samples))]

    words, codes, i = [], [], 0
    while i < len(diffs) and len(words) < _STEIM2_DATA_WORDS:
        for count, bits, code, dnib in _STEIM2_PACKINGS:
            chunk = diffs[i:i + count]
            lim = 1 << (bits - 1)
            if len(chunk) == count and all(-lim <= d < lim for d in chunk):
                break
        else:
            raise ValueError("difference too large for Steim-2")
        mask = (1 << bits) - 1
        word = 0 if dnib is None else dnib << 30
        for k, d in enumerate(chunk):
            word |= (d & mask) << (bits * (count - 1 - k))
        words.append(word)
        codes.append(code)
        i += count
    used = i

    # Lay words into frames: frame 0 = ctrl, x0, xn, 13 data; others 15 data
    payload = b""
    w = 0
    for f in range(7):
        frame_words = [0] * 16
        frame_codes = [0] * 16
        first = 3 if f == 0 else 1
        for slot in range(first, 16):
            if w < len(words):
                frame_words[slot] = words[w]
                frame_codes[slot] = codes[w]
                w += 1
        if f == 0:
            frame_words[1] = samples[0] & 0xFFFFFFFF
            frame_words[2] = samples[used - 1] & 0xFFFFFFFF
        ctrl = 0
        for slot, code in enumerate(frame_codes):
            ctrl |= code << (30 - 2 * slot)
        frame_words[0] = ctrl
        payload += _struct.pack(">16I", *frame_words)

    secs, rem = divmod(start_idx, SAMPLE_RATE)   # integer math: exact timestamp
    t = datetime.datetime.fromtimestamp(secs, datetime.timezone.utc)
    header = _struct.pack(
        ">6scc5s2s3s2sHHBBBBHHhhBBBBiHH",
        b"%06d" % (seq % 1_000_000), b"D", b" ",
        station.encode().ljust(5), location.encode().ljust(2),
        channel.encode().ljust(3), network.encode().ljust(2),
        t.year, t.timetuple().tm_yday, t.hour, t.minute, t.second, 0,
        rem * (10000 // SAMPLE_RATE),  # 0.0001 s units
        used, SAMPLE_RATE, 1,          # num samples, rate factor, multiplier
        0, 0, 0, 1,                    # flags, number of blockettes
        0, 64, 48,                     # time correction, data offset, blockette offset
    )
    blockette_1000 = _struct.pack(">HHBBBB", 1000, 0, 11, 1, 9, 0)  # Steim-2, BE, 2^9
    return header + blockette_1000 + b"\0" * 8 + payload, used


class DemoQuakes:
    """
    Synthetic ground motion (East, North, Up in counts) from local quakes.

    Each quake comes from a random direction and produces three phases with
    textbook particle motion:
      P         linear, along the ray (radial + vertical)
      S (SH)    linear, horizontal and transverse to the ray
      Rayleigh  retrograde ellipse in the radial-vertical plane
    Plus a slow microseism and broadband noise (added by the server).
    """

    FIRST_QUAKE_SECS = 8      # first quake soon after start
    MEAN_INTERVAL    = 40     # then roughly every 40 s (+/- 8 s)

    def __init__(self, seed=None):
        self._rng   = np.random.default_rng(seed)
        self._lock  = threading.Lock()
        self._quakes = []
        self._next_auto = time.time() + self.FIRST_QUAKE_SECS

    def _new_quake(self, t_p):
        rng = self._rng
        return dict(
            t_p=t_p,
            baz=rng.uniform(0, 2 * np.pi),            # direction to the source
            inc=np.radians(rng.uniform(30, 55)),      # P incidence from vertical
            sp=rng.uniform(3, 7),                     # S-P time (s)
            amp=3000 * rng.uniform(0.6, 2.5),         # P peak (counts)
        )

    def trigger(self):
        """Schedule a quake whose P wave arrives in about one second."""
        with self._lock:
            self._quakes.append(self._new_quake(time.time() + 1.0))

    def next_arrival(self):
        with self._lock:
            upcoming = [q["t_p"] for q in self._quakes if q["t_p"] > time.time()]
            return min(upcoming + [self._next_auto])

    def motion(self, t):
        """Ground motion at epoch times t (array) -> (east, north, up)."""
        with self._lock:
            while self._next_auto < t[-1] + 60:
                self._quakes.append(self._new_quake(self._next_auto))
                self._next_auto += self.MEAN_INTERVAL + self._rng.uniform(-8, 8)
            quakes = [q for q in self._quakes if q["t_p"] - 1 < t[-1]
                      and t[0] < q["t_p"] + 60]

        # Microseism: ~0.2 Hz ocean-wave hum, different on each component
        e = 400 * np.sin(2 * np.pi * 0.17 * t) + 250 * np.sin(2 * np.pi * 0.23 * t + 1.0)
        n = 350 * np.sin(2 * np.pi * 0.19 * t + 2.0) + 200 * np.sin(2 * np.pi * 0.13 * t)
        z = 300 * np.sin(2 * np.pi * 0.21 * t + 0.5)

        def pulse(dt, f, tau):
            """Sinusoid at f Hz under a fast-rise, exponential-decay envelope."""
            dt = np.maximum(dt, 0)
            return np.sin(2 * np.pi * f * dt) * (dt / tau) * np.exp(1 - dt / tau)

        for q in quakes:
            # Radial unit vector points away from the source
            r_e, r_n = -np.sin(q["baz"]), -np.cos(q["baz"])
            dt = t - q["t_p"]
            p = q["amp"] * pulse(dt, 5.0, 0.8)
            e += p * r_e * np.sin(q["inc"])
            n += p * r_n * np.sin(q["inc"])
            z += p * np.cos(q["inc"])

            s = 2.5 * q["amp"] * pulse(dt - q["sp"], 3.0, 1.2)
            e += s * -r_n        # transverse: radial rotated 90 degrees
            n += s * r_e

            dr = dt - 1.8 * q["sp"]
            env = 3.0 * q["amp"] * np.exp(-((dr - 4.0) / 2.5) ** 2) * (dr > 0)
            phase = 2 * np.pi * 1.2 * dr
            radial = -env * np.sin(phase)          # retrograde ellipse
            e += radial * r_e
            n += radial * r_n
            z += 1.5 * env * np.cos(phase)
        return e, n, z


class DemoSeedlinkServer(threading.Thread):
    """
    Minimal SEEDLINK server on 127.0.0.1 streaming synthetic EHZ/EHN/EHE.

    Mimics a real Shake: ~DC offsets, records flushed when full or after
    MAX_RECORD_SECS, and each channel delivered with a different delay so
    the viewer's timestamp alignment is exercised.
    """

    MAX_RECORD_SECS = 1.0
    CHANNEL_LAG     = {"EHZ": 0.0, "EHN": 0.35, "EHE": 0.7}
    DC_OFFSET       = {"EHZ": 16500, "EHN": -8200, "EHE": 3100}
    NOISE_COUNTS    = 120

    def __init__(self, network=RS_NETWORK_DEFAULT, station=RS_STATION_DEFAULT,
                 location=RS_LOCATION_CODE, seed=None):
        super().__init__(daemon=True)
        self.network, self.station, self.location = network, station, location
        self.quakes = DemoQuakes(seed)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))   # any free port
        self._sock.listen()
        self.port = self._sock.getsockname()[1]
        # Physical direction -> stream channel (the board labels are swapped)
        physical = {label: ch for ch, label in CHANNEL_LABELS.items()}
        self._ch_e, self._ch_n, self._ch_z = physical["EHE"], physical["EHN"], physical["EHZ"]

    def run(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            f = conn.makefile("rb")
            while True:
                line = f.readline().strip().upper()
                if not line:
                    return
                if line == b"HELLO":
                    conn.sendall(b"SeedLink v3.1 (Raspberry Shake demo)\r\nDEMO\r\n")
                elif line == b"END":
                    break
                else:
                    conn.sendall(b"OK\r\n")
            self._stream(conn)
        except OSError:
            pass   # viewer disconnected (pause / quit)
        finally:
            conn.close()

    def _stream(self, conn):
        rng = np.random.default_rng()
        next_idx = int(time.time() * SAMPLE_RATE)
        pending  = {ch: [] for ch in CHANNELS}
        start    = {ch: next_idx for ch in CHANNELS}
        prev     = {ch: None for ch in CHANNELS}
        outbox   = []    # (send_time, record)
        seq      = 0
        while True:
            now = time.time()
            now_idx = int(now * SAMPLE_RATE)
            if now_idx > next_idx:
                t = np.arange(next_idx, now_idx) / SAMPLE_RATE
                e, n, z = self.quakes.motion(t)
                for ch, x in ((self._ch_e, e), (self._ch_n, n), (self._ch_z, z)):
                    x = x + self.DC_OFFSET[ch] + rng.normal(0, self.NOISE_COUNTS, len(t))
                    pending[ch].extend(np.round(x).astype(int).tolist())
                next_idx = now_idx

            for ch in CHANNELS:
                while pending[ch]:
                    rec, used = _encode_steim2_record(
                        seq, self.network, self.station, self.location, ch,
                        start[ch], pending[ch], prev[ch],
                    )
                    full = used < len(pending[ch])
                    stale = now - start[ch] / SAMPLE_RATE >= self.MAX_RECORD_SECS
                    if not (full or stale):
                        break
                    outbox.append((now + self.CHANNEL_LAG[ch], rec))
                    prev[ch] = pending[ch][used - 1]
                    del pending[ch][:used]
                    start[ch] += used
                    seq += 1

            outbox.sort(key=lambda item: item[0])
            while outbox and outbox[0][0] <= now:
                _, rec = outbox.pop(0)
                conn.sendall(b"SL%06X" % (seq % 0x1000000) + rec)
            time.sleep(0.05)

    def stop(self):
        self._sock.close()


# ---------------------------------------------------------------------------
# Waveform panel
# ---------------------------------------------------------------------------

class WaveformPanel(QFrame):
    def __init__(self, channel, color, window_secs):
        super().__init__()
        self.channel        = channel
        self.color          = color
        self.window_samples = window_secs * SAMPLE_RATE
        self.scale          = 1.0
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "WaveformPanel { background:#0D1117; border:1px solid #222; border-radius:6px; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        lbl = QLabel(CHANNEL_LABELS.get(channel, channel))
        lbl.setFixedWidth(38)
        lbl.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color:{color}; background:transparent;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)

        pg.setConfigOptions(antialias=True)
        self.plot_widget = pg.PlotWidget(background="#0D1117")
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.showGrid(x=False, y=True, alpha=0.15)
        self.plot_widget.getAxis("bottom").hide()
        self.plot_widget.getAxis("left").setStyle(tickFont=QFont("Courier New", 8))
        self.plot_widget.getAxis("left").setPen(pg.mkPen("#333"))
        self.plot_widget.setMouseEnabled(x=False, y=False)
        # Auto-range stays ON until first real data arrives (_initial_range flag).
        # After that _refresh disables it and takes over with shared scaling.
        self.plot_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.curve = self.plot_widget.plot(pen=pg.mkPen(color=color, width=1.2))
        self.plot_widget.addLine(y=0, pen=pg.mkPen("#333", style=Qt.PenStyle.DashLine))
        layout.addWidget(self.plot_widget, stretch=1)

        amp = QVBoxLayout()
        amp.setSpacing(2)
        amp.setContentsMargins(0, 0, 0, 0)
        self.amp_label = QLabel("1.0x")
        self.amp_label.setFont(QFont("Courier New", 8))
        self.amp_label.setStyleSheet("color:#666; background:transparent;")
        self.amp_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.amp_label.setFixedWidth(36)
        btn_up = self._make_btn("^")
        btn_dn = self._make_btn("v")
        btn_up.clicked.connect(self._amp_up)
        btn_dn.clicked.connect(self._amp_dn)
        amp.addStretch()
        amp.addWidget(btn_up)
        amp.addWidget(self.amp_label)
        amp.addWidget(btn_dn)
        amp.addStretch()
        layout.addLayout(amp)

    def _make_btn(self, text):
        btn = QPushButton(text)
        btn.setFixedSize(28, 22)
        btn.setFont(QFont("Arial", 8))
        btn.setStyleSheet("""
            QPushButton { background:#1C2128; color:#888;
                          border:1px solid #2D333B; border-radius:3px; }
            QPushButton:hover   { background:#2D333B; color:#CCC; }
            QPushButton:pressed { background:#444; }
        """)
        return btn

    def _amp_up(self):
        self.scale = min(self.scale * 2.0, 10000.0)
        self._refresh_label()

    def _amp_dn(self):
        self.scale = max(self.scale / 2.0, 0.001)
        self._refresh_label()

    def _refresh_label(self):
        s = self.scale
        if   s < 0.01: t = f"{s:.3f}x"
        elif s < 1:    t = f"{s:.2f}x"
        elif s < 10:   t = f"{s:.1f}x"
        else:          t = f"{s:.0f}x"
        self.amp_label.setText(t)

    def prepare_data(self, buffer):
        """
        Step 1 of 2 — CPU work only, no screen writes.
        Converts buffer to display arrays and computes the recent peak.
        Returns (x, data, peak) ready for commit_data().
        """
        if not buffer:
            return None, None, 1.0

        data = np.array(buffer, dtype=np.int64)
        n    = min(len(data), self.window_samples)
        data = data[-n:].astype(np.float64)   # most recent n samples, raw counts

        # Right-align: newest sample at x = window_samples-1
        x = np.arange(self.window_samples - n, self.window_samples)

        # Peak from recent window only so spikes scroll out quickly
        recent_n = SCALE_WINDOW * SAMPLE_RATE
        recent   = data[-recent_n:] if len(data) > recent_n else data
        peak     = float(max(abs(recent.max()), abs(recent.min()), 1))

        return x, data, peak

    def commit_data(self, x, data):
        """
        Step 2 of 2 — screen write.
        Called for all channels together so they render in the same frame.
        """
        if x is None:
            return
        self.curve.setData(x, data)
        self.plot_widget.setXRange(0, self.window_samples - 1, padding=0)

    def set_y_range(self, raw_peak):
        """
        Set Y range to raw_peak / self.scale.
        Larger scale  ->  smaller Y range  ->  waveform appears zoomed in.
        Smaller scale ->  larger  Y range  ->  waveform appears zoomed out.
        """
        display_range = (raw_peak / self.scale) * 1.1
        self.plot_widget.setYRange(-display_range, display_range, padding=0)


# ---------------------------------------------------------------------------
# Connection banner
# ---------------------------------------------------------------------------

class ConnectionBanner(QLabel):
    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFont(QFont("Arial", 9))
        self.setFixedHeight(24)
        self.set_connected(True)

    def set_connected(self, connected):
        if connected:
            self.hide()
        else:
            self.setText(
                "  SEEDLINK connection lost -- attempting to reconnect..."
            )
            self.setStyleSheet(
                "background:#7C5500; color:#FFD966; border-radius:4px; padding:0 8px;"
            )
            self.show()


# ---------------------------------------------------------------------------
# Particle motion (3D)
# ---------------------------------------------------------------------------

class ParticleMotionWindow(QWidget):
    """
    Rotatable 3D trace of ground motion: x = East, y = North, z = Up.

    Reads the same time-aligned display buffers as the waveform panels.
    Each channel is high-passed identically (trailing 1 s moving-average
    removal) to strip the DC offset and drift without introducing any
    phase difference between channels, then all three share one scale
    so the shape of the motion is not distorted.

    Depth cues: the trail's shadows are projected onto the floor and the two
    walls *behind* it (re-chosen as the camera moves), a drop line ties the
    current point to its floor shadow, a slow auto-rotation adds motion
    parallax, and a wide field of view strengthens perspective.

    Requires PyOpenGL (pyqtgraph.opengl); the caller handles ImportError.
    """

    TRAIL_CHOICES  = [2, 5, 10]            # seconds of motion shown
    TRAIL_DEFAULT  = 5
    HIGHPASS_SECS  = 1                     # moving-average window
    TRAIL_COLOR    = (1.0, 0.85, 0.40)     # amber
    SHADOW_ALPHA   = 0.35                  # shadow opacity relative to trail
    SCALE_DECAY    = 0.97                  # per-frame shrink of display scale
    ROTATE_DEG     = 0.12                  # auto-rotation per frame (~3.6 deg/s)
    ROTATE_RESUME  = 2.0                   # seconds after a drag before resuming
    CAMERA_FOV     = 75                    # degrees (pyqtgraph default is 60)
    CAMERA_DIST    = 3.8

    def __init__(self, buffers, parent=None):
        import pyqtgraph.opengl as gl   # raises ImportError without PyOpenGL

        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window)
        self.setWindowTitle("Raspberry Shake -- Particle Motion")
        self.resize(680, 620)
        self.buffers    = buffers
        self.trail_secs = self.TRAIL_DEFAULT
        self._scale     = 0.0
        self._walls     = None   # (x, y, z) of the current back walls / floor
        self._dragging  = False
        self._resume_at = 0.0    # auto-rotation resumes after this time

        # Map physical directions to stream channels (board labels are swapped)
        physical = {label: ch for ch, label in CHANNEL_LABELS.items()}
        self.ch_e = physical["EHE"]
        self.ch_n = physical["EHN"]
        self.ch_z = physical["EHZ"]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)

        bar = QHBoxLayout()
        self.info = QLabel("Waiting for data...")
        self.info.setFont(QFont("Courier New", 9))
        self.info.setStyleSheet("color:#888; background:transparent;")
        bar.addWidget(self.info)
        bar.addStretch()
        bar.addSpacing(16)   # keep the info text off the controls when narrow

        trail_lbl = QLabel("Trail:")
        trail_lbl.setStyleSheet("color:#888; background:transparent;")
        bar.addWidget(trail_lbl)
        self.trail_box = QComboBox()
        for secs in self.TRAIL_CHOICES:
            self.trail_box.addItem(f"{secs} s", secs)
        self.trail_box.setCurrentIndex(self.TRAIL_CHOICES.index(self.TRAIL_DEFAULT))
        self.trail_box.currentIndexChanged.connect(
            lambda i: setattr(self, "trail_secs", self.trail_box.itemData(i))
        )
        bar.addWidget(self.trail_box)

        reset_btn = QPushButton("Reset View")
        reset_btn.setStyleSheet("""
            QPushButton { background:#1C2128; color:#999; border:1px solid #2D333B;
                          border-radius:3px; padding:2px 10px; }
            QPushButton:hover { background:#2D333B; color:#DDD; }
        """)
        reset_btn.clicked.connect(self._reset_view)

        self.rotate_btn = QPushButton("Auto-rotate")
        self.rotate_btn.setCheckable(True)
        self.rotate_btn.setChecked(True)
        self.rotate_btn.setStyleSheet("""
            QPushButton { background:#1C2128; color:#999; border:1px solid #2D333B;
                          border-radius:3px; padding:2px 10px; }
            QPushButton:hover { background:#2D333B; color:#DDD; }
            QPushButton:checked { background:#3B341A; color:#FFD966;
                                  border:1px solid #6B5E2D; }
        """)
        bar.addWidget(self.rotate_btn)
        bar.addWidget(reset_btn)
        layout.addLayout(bar)

        self.view = gl.GLViewWidget()
        self.view.setBackgroundColor("#0D1117")
        self.view.installEventFilter(self)   # pause auto-rotation while dragging
        layout.addWidget(self.view, stretch=1)

        # Bounding cube [-1, 1]^3 with grids on the floor and two back walls
        corners = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)],
                           dtype=float)
        edges = [(a, b) for a in range(8) for b in range(a + 1, 8)
                 if np.sum(corners[a] != corners[b]) == 1]
        self.view.addItem(gl.GLLinePlotItem(
            pos=np.array([corners[i] for e in edges for i in e]),
            color=(1, 1, 1, 0.12), width=1, antialias=True, mode="lines",
        ))
        self.wall_grids = []
        for _ in range(3):   # floor, x-wall, y-wall; placed in _place_walls()
            grid = gl.GLGridItem()
            grid.setSize(2, 2)
            grid.setSpacing(0.25, 0.25)
            grid.setColor((255, 255, 255, 22))
            self.view.addItem(grid)
            self.wall_grids.append(grid)

        # Axes colored like the matching waveform panel, labeled at both ends
        for vec, ch, pos_lbl, neg_lbl in [
            ((1, 0, 0), self.ch_e, "E", "W"),
            ((0, 1, 0), self.ch_n, "N", "S"),
            ((0, 0, 1), self.ch_z, "Up", "Down"),
        ]:
            v = np.array(vec, dtype=float)
            color = QColor(CHANNEL_COLORS[ch])
            self.view.addItem(gl.GLLinePlotItem(
                pos=np.array([-v, v]), color=color, width=1.5, antialias=True
            ))
            for p, text in [(v * 1.12, pos_lbl), (-v * 1.12, neg_lbl)]:
                self.view.addItem(gl.GLTextItem(
                    pos=p, text=text, color=color, font=QFont("Arial", 10)
                ))

        # Shadows of the trail on floor / x-wall / y-wall, and of the head
        self.shadows = []
        for _ in range(3):
            item = gl.GLLinePlotItem(
                pos=np.zeros((2, 3)), width=1.5, antialias=True, mode="line_strip"
            )
            self.view.addItem(item)
            self.shadows.append(item)
        self.head_shadows = gl.GLScatterPlotItem(
            pos=np.zeros((3, 3)), size=5, color=(1, 1, 1, 0.45)
        )
        self.view.addItem(self.head_shadows)
        self.drop_line = gl.GLLinePlotItem(
            pos=np.zeros((2, 3)), color=(1, 1, 1, 0.45), width=1, antialias=True
        )
        self.view.addItem(self.drop_line)

        self.trail = gl.GLLinePlotItem(
            pos=np.zeros((2, 3)), width=2, antialias=True, mode="line_strip"
        )
        self.view.addItem(self.trail)
        self.head = gl.GLScatterPlotItem(
            pos=np.zeros((1, 3)), size=9, color=(1, 1, 1, 1)
        )
        self.view.addItem(self.head)
        self._reset_view()
        self._place_walls()

        self._timer = QTimer(self)
        self._timer.setInterval(33)   # ~30 fps is plenty for a 3D trail
        self._timer.timeout.connect(self._update)

    def _reset_view(self):
        self.view.setCameraParams(fov=self.CAMERA_FOV, distance=self.CAMERA_DIST,
                                  elevation=22, azimuth=-60)

    def eventFilter(self, obj, event):
        if obj is self.view:
            if event.type() == QEvent.Type.MouseButtonPress:
                self._dragging = True
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._dragging = False
                self._resume_at = time.time() + self.ROTATE_RESUME
        return super().eventFilter(obj, event)

    def _place_walls(self):
        """Put the floor and two walls on the far side of the cube from the
        camera, so shadows are always behind the trail, never in front."""
        cam = self.view.cameraPosition()
        walls = (-1.0 if cam.x() > 0 else 1.0,
                 -1.0 if cam.y() > 0 else 1.0,
                 -1.0 if cam.z() >= 0 else 1.0)
        if walls == self._walls:
            return
        self._walls = walls
        wx, wy, wz = walls
        floor, xwall, ywall = self.wall_grids
        floor.resetTransform()
        floor.translate(0, 0, wz)
        xwall.resetTransform()
        xwall.rotate(90, 0, 1, 0)        # grid plane XY -> YZ
        xwall.translate(wx, 0, 0)
        ywall.resetTransform()
        ywall.rotate(90, 1, 0, 0)        # grid plane XY -> XZ
        ywall.translate(0, wy, 0)

    def showEvent(self, event):
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _highpassed(self, ch, m, w):
        """Last m-w+1 samples of channel ch minus a trailing w-sample mean."""
        x = np.array(self.buffers[ch], dtype=np.float64)[-m:]
        c = np.concatenate(([0.0], np.cumsum(x)))
        mean = (c[w:] - c[:-w]) / w
        return x[w - 1:] - mean

    def _update(self):
        if (self.rotate_btn.isChecked() and not self._dragging
                and time.time() >= self._resume_at):
            self.view.orbit(self.ROTATE_DEG, 0)
        self._place_walls()

        n = self.trail_secs * SAMPLE_RATE
        w = self.HIGHPASS_SECS * SAMPLE_RATE
        avail = min(len(self.buffers[ch]) for ch in (self.ch_e, self.ch_n, self.ch_z))
        if avail < w + 2:
            for item in [self.trail, self.drop_line, *self.shadows]:
                item.setData(pos=np.zeros((2, 3)))
            self.head.setData(pos=np.zeros((1, 3)))
            self.head_shadows.setData(pos=np.zeros((3, 3)))
            self.info.setText("Waiting for data...")
            return
        m = min(n + w - 1, avail)
        pts = np.column_stack([
            self._highpassed(self.ch_e, m, w),
            self._highpassed(self.ch_n, m, w),
            self._highpassed(self.ch_z, m, w),
        ])

        # Shared scale for all axes; grows instantly, shrinks slowly
        peak = float(np.abs(pts).max())
        self._scale = max(peak, self._scale * self.SCALE_DECAY, 1.0)
        pts /= self._scale

        # Fade the trail from transparent (oldest) to opaque (newest)
        colors = np.empty((len(pts), 4))
        colors[:, :3] = self.TRAIL_COLOR
        colors[:, 3] = np.linspace(0.05, 1.0, len(pts))
        self.trail.setData(pos=pts, color=colors)
        self.head.setData(pos=pts[-1:])

        # Shadows: the same points flattened onto each back wall
        shadow_colors = colors.copy()
        shadow_colors[:, 3] *= self.SHADOW_ALPHA
        head_shadows = []
        for axis, item in zip((2, 0, 1), self.shadows):   # floor, x-wall, y-wall
            flat = pts.copy()
            flat[:, axis] = self._walls[axis]
            item.setData(pos=flat, color=shadow_colors)
            head_shadows.append(flat[-1])
        self.head_shadows.setData(pos=np.array(head_shadows))
        self.drop_line.setData(pos=np.array([pts[-1], head_shadows[0]]))
        self.info.setText(
            f"Last {len(pts) / SAMPLE_RATE:.0f} s  |  high-pass ~0.5 Hz"
            f"  |  full scale ±{self._scale:,.0f} counts"
        )


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, rs_host, port, network, station, window_secs, demo=None):
        super().__init__()
        self.rs_host     = rs_host or "unknown"
        self.window_secs = window_secs
        self.demo        = demo      # DemoSeedlinkServer in --demo mode, else None
        self.setWindowTitle(
            "Raspberry Shake -- Live Waveform Viewer" + ("  [DEMO]" if demo else "")
        )
        self.resize(1100, 660)
        self.setMinimumSize(700, 420)
        self.setStyleSheet(
            "QMainWindow, QWidget { background-color:#0D1117; color:#E6EDF3; }"
        )

        maxlen = window_secs * SAMPLE_RATE * BUFFER_FACTOR
        self.buffers = {ch: deque(maxlen=maxlen) for ch in CHANNELS}
        # SEEDLINK thread pushes time-stamped records here; display timer pulls
        # exactly samples_per_tick time-aligned samples per channel per tick.
        self.aligner = ChannelAligner(CHANNELS)
        self.panels  = {}
        self._streaming    = False   # True while SEEDLINK is connected
        self._paused       = False   # True while gap-filling zeros into buffer
        self._auto_scale   = True    # True = auto Y range; False = fixed range
        self._locked_peak  = DEFAULT_Y_RANGE  # peak captured when Lock Scale clicked
        self._initial_range = True   # True until first real data frame is drawn
        # Keep connection params so Pause/Resume can disconnect and reconnect
        self._rs_host    = rs_host
        self._rs_port    = port
        self._rs_network = network
        self._rs_station = station
        self._gap_timer  = None    # QTimer that pumps zeros while paused
        self._pm_window  = None    # ParticleMotionWindow, created on demand

        self._build_ui()
        self._start_seedlink(rs_host, port, network, station)
        self._start_timer()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 6)
        root.setSpacing(6)

        header = QHBoxLayout()
        # DEMO badge goes first so it can't be truncated when the header is tight
        title = QLabel(
            ("<span style='color:#FFB347;'>DEMO</span>&nbsp;&nbsp;" if self.demo else "")
            + f"Raspberry Shake  --  Real-Time Seismograph"
            + ("" if self.demo else
               f"   <span style='font-size:10px; color:#444;'>({self.rs_host})</span>")
        )
        title.setFont(QFont("Georgia", 13, QFont.Weight.Bold))
        title.setStyleSheet("color:#E6EDF3; background:transparent;")
        header.addWidget(title)
        header.addStretch()

        gbox = QGroupBox("Global Amplitude")
        gbox.setStyleSheet("""
            QGroupBox { color:#666; font-size:9px; border:1px solid #2D333B;
                        border-radius:4px; margin-top:6px; padding:4px; }
            QGroupBox::title { subcontrol-origin:margin; left:8px; }
        """)
        gb = QHBoxLayout(gbox)
        gb.setContentsMargins(6, 2, 6, 2)
        gb.setSpacing(4)
        for label, fn in [
            ("^ All", lambda: self._global_amp(True)),
            ("v All", lambda: self._global_amp(False)),
            ("Reset", self._global_reset),
        ]:
            b = self._ctrl_btn(label)
            b.clicked.connect(fn)
            gb.addWidget(b)
        header.addWidget(gbox)

        # Lock Scale toggle button
        self.scale_btn = QPushButton("  Lock Scale  ")
        self.scale_btn.setFixedHeight(28)
        self.scale_btn.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        self.scale_btn.setStyleSheet("""
            QPushButton {
                background: #1A1A3B; color: #A0A0FF;
                border: 1px solid #2D2D6B; border-radius: 4px; padding: 0 14px;
            }
            QPushButton:hover   { background: #252560; color: #C0C0FF; }
            QPushButton:pressed { background: #303080; }
        """)
        self.scale_btn.clicked.connect(self._toggle_scale_lock)
        header.addWidget(self.scale_btn)

        # Clear button
        clear_btn = QPushButton("  Clear  ")
        clear_btn.setFixedHeight(28)
        clear_btn.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        clear_btn.setStyleSheet("""
            QPushButton {
                background: #1A2A3B; color: #69CFFF;
                border: 1px solid #2D4A6B; border-radius: 4px; padding: 0 14px;
            }
            QPushButton:hover   { background: #224060; color: #9FDFFF; }
            QPushButton:pressed { background: #2A5080; }
        """)
        clear_btn.clicked.connect(self._clear_screen)
        header.addWidget(clear_btn)

        # Particle motion (3D) button
        pm_btn = QPushButton("  Particle Motion  ")
        pm_btn.setFixedHeight(28)
        pm_btn.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        pm_btn.setStyleSheet("""
            QPushButton {
                background: #3B341A; color: #FFD966;
                border: 1px solid #6B5E2D; border-radius: 4px; padding: 0 14px;
            }
            QPushButton:hover   { background: #5C5022; color: #FFE699; }
            QPushButton:pressed { background: #7A6A2A; }
        """)
        pm_btn.clicked.connect(self._show_particle_motion)
        header.addWidget(pm_btn)

        # Demo only: trigger a synthetic quake on demand
        if self.demo:
            quake_btn = QPushButton("  Quake!  ")
            quake_btn.setFixedHeight(28)
            quake_btn.setFont(QFont("Arial", 9, QFont.Weight.Bold))
            quake_btn.setStyleSheet("""
                QPushButton {
                    background: #3B2A1A; color: #FFB347;
                    border: 1px solid #6B4A2D; border-radius: 4px; padding: 0 14px;
                }
                QPushButton:hover   { background: #5C4020; color: #FFD080; }
                QPushButton:pressed { background: #7A5528; }
            """)
            quake_btn.clicked.connect(self.demo.quakes.trigger)
            header.addWidget(quake_btn)

        # Pause / Resume button
        self.stop_btn = QPushButton("  Pause  ")
        self.stop_btn.setFixedHeight(28)
        self.stop_btn.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        self.stop_btn.setStyleSheet("""
            QPushButton {
                background: #3B1A1A; color: #FF6B6B;
                border: 1px solid #6B2D2D; border-radius: 4px; padding: 0 14px;
            }
            QPushButton:hover   { background: #5C2222; color: #FF9999; }
            QPushButton:pressed { background: #7A2A2A; }
        """)
        self.stop_btn.clicked.connect(self._toggle_stream)
        header.addWidget(self.stop_btn)

        root.addLayout(header)

        self.banner = ConnectionBanner()
        root.addWidget(self.banner)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#2D333B;")
        root.addWidget(sep)

        for ch in CHANNELS:
            panel = WaveformPanel(ch, CHANNEL_COLORS[ch], self.window_secs)
            self.panels[ch] = panel
            root.addWidget(panel, stretch=1)

        self.status_bar = QStatusBar()
        self.status_bar.setStyleSheet(
            "QStatusBar { background:#0D1117; color:#555; "
            "font-family:'Courier New'; font-size:9px; }"
        )
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Initialising...")

    def _ctrl_btn(self, text):
        btn = QPushButton(text)
        btn.setFixedHeight(24)
        btn.setFont(QFont("Arial", 8))
        btn.setStyleSheet("""
            QPushButton { background:#1C2128; color:#999; border:1px solid #2D333B;
                          border-radius:3px; padding:0 8px; }
            QPushButton:hover   { background:#2D333B; color:#DDD; }
            QPushButton:pressed { background:#444; }
        """)
        return btn

    def _global_amp(self, up):
        for p in self.panels.values():
            p._amp_up() if up else p._amp_dn()

    def _global_reset(self):
        for p in self.panels.values():
            p.scale = 1.0
            p.amp_label.setText("1.0x")

    def _toggle_scale_lock(self):
        """Toggle between auto-scaling and the range frozen at click time."""
        # _locked_peak is kept current by _refresh while in auto mode,
        # so it holds exactly the live range at the moment the user clicks Lock.
        self._auto_scale = not self._auto_scale
        if self._auto_scale:
            self.scale_btn.setText("  Lock Scale  ")
            self.scale_btn.setStyleSheet("""
                QPushButton {
                    background: #1A1A3B; color: #A0A0FF;
                    border: 1px solid #2D2D6B; border-radius: 4px; padding: 0 14px;
                }
                QPushButton:hover   { background: #252560; color: #C0C0FF; }
                QPushButton:pressed { background: #303080; }
            """)
        else:
            self.scale_btn.setText("  Auto Scale  ")
            self.scale_btn.setStyleSheet("""
                QPushButton {
                    background: #3B2A1A; color: #FFB347;
                    border: 1px solid #6B4A2D; border-radius: 4px; padding: 0 14px;
                }
                QPushButton:hover   { background: #5C4020; color: #FFD080; }
                QPushButton:pressed { background: #7A5528; }
            """)

    def _show_particle_motion(self):
        if self._pm_window is None:
            try:
                self._pm_window = ParticleMotionWindow(self.buffers, parent=self)
            except ImportError:
                QMessageBox.warning(
                    self, "Particle Motion",
                    "The 3D view needs PyOpenGL.\n\nInstall it with:\n"
                    "    pip install PyOpenGL"
                )
                return
        self._pm_window.show()
        self._pm_window.raise_()
        self._pm_window.activateWindow()

    def _clear_screen(self):
        """Wipe display buffers — traces reset to blank; stream continues."""
        for buf in self.buffers.values():
            buf.clear()
        for panel in self.panels.values():
            panel.curve.setData([])

    def _toggle_stream(self):
        if not self._paused:
            # ── PAUSE ─────────────────────────────────────────────────────
            # 1. Stop SEEDLINK — no more real samples arrive
            self.listener.stop()
            self._streaming = False
            self._paused    = True

            # 2. Gap-fill: _tick already runs continuously; while _paused=True
            #    it injects zeros instead of pulling from the aligner.
            pass  # nothing extra needed — _tick handles it

            self.stop_btn.setText("  Resume  ")
            self.stop_btn.setStyleSheet("""
                QPushButton {
                    background: #1A3B1A; color: #69FF47;
                    border: 1px solid #2D6B2D; border-radius: 4px; padding: 0 14px;
                }
                QPushButton:hover   { background: #2A5C2A; color: #9FFF9F; }
                QPushButton:pressed { background: #2A7A2A; }
            """)
            self.status_bar.showMessage(
                "PAUSED -- gap being drawn. Click Resume to reconnect live stream."
            )
            self.banner.set_connected(False)
        else:
            # ── RESUME ────────────────────────────────────────────────────
            # 1. Flush stale aligned data so we don't replay backlog on resume
            self.aligner.reset()
            self._paused = False

            # 2. Reconnect SEEDLINK — live data follows the gap seamlessly
            # Do NOT re-arm _initial_range — keep the scale that was active at pause
            self._start_seedlink(
                self._rs_host, self._rs_port,
                self._rs_network, self._rs_station
            )
            self.stop_btn.setText("  Pause  ")
            self.stop_btn.setStyleSheet("""
                QPushButton {
                    background: #3B1A1A; color: #FF6B6B;
                    border: 1px solid #6B2D2D; border-radius: 4px; padding: 0 14px;
                }
                QPushButton:hover   { background: #5C2222; color: #FF9999; }
                QPushButton:pressed { background: #7A2A2A; }
            """)

    def _tick(self):
        """
        Called every 40 ms (25 Hz). Advances every channel by exactly
        samples_per_tick = 4 samples, keeping all three channels in lockstep.

        Live:   pulls 4 time-aligned samples per channel from the aligner.
                Between RS bursts all channels hold their last value together
                so scroll speed stays constant; any surplus latency built up
                that way is drained gradually (see ChannelAligner).

        Paused: injects 4 zeros per channel — identical scroll speed, flat line.
        """
        samples_per_tick = max(1, SAMPLE_RATE // 25)   # 4 samples @ 25 Hz = 100 sps

        if self._paused:
            for buf in self.buffers.values():
                buf.extend([0] * samples_per_tick)
            return

        batch = self.aligner.pull(samples_per_tick)
        for ch, buf in self.buffers.items():
            buf.extend(batch[ch])

    def _start_seedlink(self, host, port, network, station):
        self.listener = SeedlinkListener(
            host=host, port=port, network=network, station=station,
            location=RS_LOCATION_CODE,
            buffers=self.buffers, aligner=self.aligner,
            window_secs=self.window_secs,
        )
        self.listener.signals.status_changed.connect(self.status_bar.showMessage)
        self.listener.signals.connection_changed.connect(self.banner.set_connected)
        self.listener.start()
        self._streaming = True

    def _start_timer(self):
        # Data tick: pull aligned samples at 25 Hz (4 samples/tick = 100 sps)
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(40)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        # Display refresh: redraw plots at 60 fps
        self._draw_timer = QTimer(self)
        self._draw_timer.setInterval(16)
        self._draw_timer.timeout.connect(self._refresh)
        self._draw_timer.start()

    def _refresh(self):
        # Always refresh — gap zeros keep the trace scrolling while paused

        now    = time.time()
        active = []
        peaks  = []

        # Phase 1: CPU — prepare all three channels (no screen writes yet)
        prepared = {}
        for ch, panel in self.panels.items():
            x, data, peak = panel.prepare_data(self.buffers[ch])
            prepared[ch] = (x, data)
            peaks.append(peak)
            t = self.listener.last_packet.get(ch)
            if t and (now - t) < 10:
                active.append(ch)

        # Phase 2: GPU — commit all three channels simultaneously
        for ch, panel in self.panels.items():
            panel.commit_data(*prepared[ch])

        # Shared Y range across all panels for direct inter-component comparison.
        # In auto mode: scale to recent peak with no artificial floor — fully adaptive.
        # In locked mode: fixed at DEFAULT_Y_RANGE so spikes can't blow up the scale.
        # On the very first frame that has real (non-zero) data, let pyqtgraph
        # auto-range once to land on a sensible scale, then take over.
        if self._initial_range:
            real_peak = max(peaks) if peaks else 0.0
            if real_peak > 1.0:   # non-trivial data has arrived
                for panel in self.panels.values():
                    panel.plot_widget.enableAutoRange(axis="y", enable=True)
                    panel.plot_widget.enableAutoRange(axis="y", enable=False)
                self._locked_peak   = real_peak
                self._initial_range = False
            # don't call set_y_range yet — let pyqtgraph own it this frame
            return

        if self._auto_scale:
            shared_peak = max(peaks) if peaks else 1.0
            self._locked_peak = shared_peak   # keep up to date for Lock Scale snapshot
        else:
            shared_peak = self._locked_peak   # frozen at the moment Lock was clicked
        for panel in self.panels.values():
            panel.set_y_range(shared_peak)

        pkts = self.listener.packet_count
        if self.listener._connected:
            if active:
                demo_info = ""
                if self.demo:
                    wait = self.demo.quakes.next_arrival() - time.time()
                    demo_info = f"   |  DEMO: next quake in ~{max(0, wait):.0f} s"
                self.status_bar.showMessage(
                    f"SEEDLINK LIVE  --  Channels: {', '.join(active)}"
                    f"   |  Records received: {pkts}{demo_info}"
                )
            else:
                self.status_bar.showMessage(
                    f"SEEDLINK connected -- waiting for first record...   |  Records: {pkts}"
                )
        else:
            self.status_bar.showMessage(
                f"SEEDLINK -- connecting to {self.rs_host}:{self.listener.port}..."
            )

    def closeEvent(self, event):
        self._tick_timer.stop()
        self._draw_timer.stop()
        self.listener.stop()
        if self._pm_window is not None:
            self._pm_window.close()
        if self.demo:
            self.demo.stop()
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Raspberry Shake SEEDLINK waveform viewer")
    parser.add_argument("--rs-host",        default=None)
    parser.add_argument("--port",           default=RS_SEEDLINK_PORT, type=int)
    parser.add_argument("--window",         default=60, type=int)
    parser.add_argument("--network",        default=RS_NETWORK_DEFAULT)
    parser.add_argument("--station",        default=RS_STATION_DEFAULT)
    parser.add_argument("--ssh-user",       default=RS_SSH_USER_DEFAULT)
    parser.add_argument("--ssh-pass",       default=RS_SSH_PASS_DEFAULT)
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--demo", action="store_true",
                        help="simulate a Raspberry Shake with synthetic quakes "
                             "(no hardware needed)")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,        QColor("#0D1117"))
    palette.setColor(QPalette.ColorRole.WindowText,    QColor("#E6EDF3"))
    palette.setColor(QPalette.ColorRole.Base,          QColor("#0D1117"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#161B22"))
    palette.setColor(QPalette.ColorRole.Text,          QColor("#E6EDF3"))
    palette.setColor(QPalette.ColorRole.Button,        QColor("#1C2128"))
    palette.setColor(QPalette.ColorRole.ButtonText,    QColor("#E6EDF3"))
    app.setPalette(palette)

    demo = None
    port = args.port
    rs_host = args.rs_host
    if args.demo:
        demo = DemoSeedlinkServer(network=args.network, station=args.station)
        demo.start()
        rs_host, port = "127.0.0.1", demo.port
    elif not args.skip_preflight:
        dlg = PreflightDialog(args.ssh_user, args.ssh_pass)
        dlg.exec()
        if dlg.rs_host:
            rs_host = dlg.rs_host

    if not rs_host:
        rs_host = "192.168.1.2"  # known RS IP from seedlink string

    win = MainWindow(
        rs_host=rs_host,
        port=port,
        network=args.network,
        station=args.station,
        window_secs=args.window,
        demo=demo,
    )
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
