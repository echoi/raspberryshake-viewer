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

Requirements:
    pip install PyQt6 pyqtgraph numpy paramiko

Run:
    python rshake_viewer.py

Optional CLI args:
    --rs-host       RS hostname/IP        (default: auto-detect)
    --port          SEEDLINK port         (default: 18000)
    --window        Seconds of data       (default: 60)
    --network       SEEDLINK network code (default: AM)
    --station       SEEDLINK station code (default: wildcard R???0)
    --ssh-user      RS SSH username       (default: myshake)
    --ssh-pass      RS SSH password       (default: earthday2023)
    --skip-preflight  Skip discovery and clock-sync dialog
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
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread
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
    Returns (channel_code: str, samples: list[int]) or (None, []) on error.
    Handles Steim-1 (encoding 10) and Steim-2 (encoding 11).

    MiniSEED fixed header layout (big-endian):
      0- 5  sequence number (ASCII)
      6      data header/quality indicator
      7      reserved
      8-12   station (5 chars)
      13-14  location (2 chars)
      15-17  channel (3 chars)
      18-19  network (2 chars)
      20-21  num_samples (uint16)
      24-27  start time (various)
      28     sample rate factor (int16)
      30     sample rate multiplier (int16)
      32     activity flags
      33     IO flags
      34     data quality flags
      35     num_blockettes
      36-39  time correction
      40-41  data offset (uint16)  <-- byte offset to first data sample
      42-43  first blockette offset (uint16)
    """
    try:
        # Confirmed correct offsets from live RS packet hex dump:
        #   station [8:13], location [13:15], channel [15:18], network [18:20]
        #   num_samp [30:32], data_off [44:46], blkt_off [46:48]
        channel  = raw[15:18].decode("ascii").strip()
        num_samp = _struct.unpack_from(">H", raw, 30)[0]
        data_off = _struct.unpack_from(">H", raw, 44)[0]
        blkt_off = _struct.unpack_from(">H", raw, 46)[0]

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
                break
            if next_blkt == 0 or next_blkt == off:
                break
            off = next_blkt

        if num_samp == 0 or data_off == 0 or data_off >= 512:
            return None, []

        payload = raw[data_off:]

        if encoding == 10:
            samples = _decode_steim1(payload, num_samp)
        elif encoding == 11:
            samples = _decode_steim2(payload, num_samp)
        else:
            return None, []

        return channel, samples
    except Exception:
        return None, []


def _decode_steim1(data, num_samp):
    """Decode Steim-1 compressed data."""
    samples = []
    x0 = None
    frames = len(data) // 64
    for f in range(frames):
        frame = data[f*64:(f+1)*64]
        ctrl  = _struct.unpack_from(">I", frame, 0)[0]
        for w in range(15):
            code = (ctrl >> (30 - 2*w)) & 0x3
            word = _struct.unpack_from(">i", frame, (w+1)*4)[0]
            if f == 0 and w == 0:
                x0 = _struct.unpack_from(">i", frame, 4)[0]
                continue
            if f == 0 and w == 1:
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

    # Integrate differences
    if x0 is None:
        return []
    out, val = [], x0
    out.append(val)
    for d in samples[:num_samp-1]:
        val += d
        out.append(val)
    return out[:num_samp]


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
    x0      = None
    frames  = len(data) // 64

    for f in range(frames):
        frame = data[f*64:(f+1)*64]
        ctrl  = _struct.unpack_from(">I", frame, 0)[0]  # control word

        for w in range(15):
            code = (ctrl >> (30 - 2*w)) & 0x3
            word = _struct.unpack_from(">I", frame, (w+1)*4)[0]  # unsigned 32-bit

            # Frame 0 words 0/1 hold x0 (forward integration constant) and xn
            if f == 0 and w == 0:
                x0 = _struct.unpack_from(">i", frame, 4)[0]   # signed
                continue
            if f == 0 and w == 1:
                continue   # xn (last sample) — not needed for forward decode

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
                    # 7 differences, 4 bits each
                    for i in range(7):
                        samples.append(_sign_extend((word >> (28 - 4*i)) & 0xF, 4))

    if x0 is None:
        return []

    # Integrate differences using Python ints (no overflow risk)
    out = [x0]
    val = x0
    for d in samples[:num_samp - 1]:
        val += d
        out.append(val)
    return out[:num_samp]


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

    def __init__(self, host, port, network, station, location, buffers, staging, staging_lock, window_secs):
        super().__init__(daemon=True)
        self.host         = host
        self.port         = port
        self.network      = network
        self.station      = station
        self.location     = location   # e.g. "00"
        self.buffers      = buffers
        self.staging      = staging       # {ch: deque} drained by display timer
        self.staging_lock = staging_lock  # protects staging across threads
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

        # ── Streaming loop with diagnostics ───────────────────────────
        import os, pathlib
        log_path = pathlib.Path.home() / "rshake_diag.txt"
        diag_done = False   # only dump diagnostics once

        buf = b""
        while self.running:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Server closed stream")
            buf += chunk

            # Diagnostic: dump first 600 bytes received to a file
            if not diag_done and len(buf) >= 64:
                diag_done = True
                with open(log_path, "w") as lf:
                    lf.write(f"=== RS SEEDLINK raw bytes (first {min(600,len(buf))}) ===\n")
                    raw_sample = buf[:600]
                    # hex dump
                    for i in range(0, len(raw_sample), 16):
                        row = raw_sample[i:i+16]
                        hex_part = " ".join(f"{b:02X}" for b in row)
                        asc_part = "".join(chr(b) if 32<=b<127 else "." for b in row)
                        lf.write(f"  {i:04X}  {hex_part:<47}  {asc_part}\n")
                    lf.write(f"\nFirst 8 bytes (SL header?): {raw_sample[:8]}\n")
                    lf.write(f"Starts with SL: {raw_sample[:2] == b'SL'}\n")
                    lf.write(f"Total bytes in buffer: {len(buf)}\n")
                self.signals.status_changed.emit(
                    f"Diagnostic log written to: {log_path}"
                )

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
        ch, samples = _decode_miniseed(raw)
        if ch is None or not samples:
            return
        ch_key = ch[-3:] if len(ch) >= 3 else ch
        if ch_key not in self.buffers:
            return
        # Write to staging deque under lock — display timer drains at fixed rate
        with self.staging_lock:
            self.staging[ch_key].extend(samples)
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
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, rs_host, port, network, station, window_secs):
        super().__init__()
        self.rs_host     = rs_host or "unknown"
        self.window_secs = window_secs
        self.setWindowTitle("Raspberry Shake -- Live Waveform Viewer")
        self.resize(1100, 660)
        self.setMinimumSize(700, 420)
        self.setStyleSheet(
            "QMainWindow, QWidget { background-color:#0D1117; color:#E6EDF3; }"
        )

        maxlen = window_secs * SAMPLE_RATE * BUFFER_FACTOR
        self.buffers = {ch: deque(maxlen=maxlen) for ch in CHANNELS}
        # Staging deques: SEEDLINK thread appends here; display timer drains
        # exactly samples_per_tick samples per channel per tick for smooth scroll.
        # maxlen = 2 seconds of samples so a burst never causes unbounded lag.
        staging_maxlen = SAMPLE_RATE * 2
        self.staging = {ch: deque(maxlen=staging_maxlen) for ch in CHANNELS}
        self._staging_lock = threading.Lock()   # protect cross-thread access
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
        title = QLabel(
            f"Raspberry Shake  --  Real-Time Seismograph"
            f"   <span style='font-size:10px; color:#444;'>({self.rs_host})</span>"
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

    def _clear_screen(self):
        """Wipe display buffers and staging deques — traces reset to blank."""
        with self._staging_lock:
            for buf in self.buffers.values():
                buf.clear()
            for stg in self.staging.values():
                stg.clear()
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
            #    it injects zeros instead of draining the staging queue.
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
            # 1. Flush stale staging data so we don't replay backlog on resume
            with self._staging_lock:
                for stg in self.staging.values():
                    stg.clear()
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

        Live:   takes up to 4 samples from each staging deque.
                If staging has fewer than 4 (between RS bursts), pads with
                the last known value so scroll speed stays constant.
                If staging has built up a backlog (e.g. after resume), it
                drains at the normal 4/tick rate — lag clears within seconds.

        Paused: injects 4 zeros per channel — identical scroll speed, flat line.
        """
        samples_per_tick = max(1, SAMPLE_RATE // 25)   # 4 samples @ 25 Hz = 100 sps

        if self._paused:
            for buf in self.buffers.values():
                buf.extend([0] * samples_per_tick)
            return

        with self._staging_lock:
            for ch, buf in self.buffers.items():
                stg = self.staging[ch]
                batch = []
                for _ in range(samples_per_tick):
                    if stg:
                        batch.append(stg.popleft())
                    else:
                        # Pad with last known value to keep scroll steady
                        batch.append(buf[-1] if buf else 0)
                buf.extend(batch)

    def _start_seedlink(self, host, port, network, station):
        self.listener = SeedlinkListener(
            host=host, port=port, network=network, station=station,
            location=RS_LOCATION_CODE,
            buffers=self.buffers, staging=self.staging,
            staging_lock=self._staging_lock,
            window_secs=self.window_secs,
        )
        self.listener.signals.status_changed.connect(self.status_bar.showMessage)
        self.listener.signals.connection_changed.connect(self.banner.set_connected)
        self.listener.start()
        self._streaming = True

    def _start_timer(self):
        # Data tick: drain staging queues at 25 Hz (4 samples/tick = 100 sps)
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
                self.status_bar.showMessage(
                    f"SEEDLINK LIVE  --  Channels: {', '.join(active)}"
                    f"   |  Records received: {pkts}"
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

    rs_host = args.rs_host
    if not args.skip_preflight:
        dlg = PreflightDialog(args.ssh_user, args.ssh_pass)
        dlg.exec()
        if dlg.rs_host:
            rs_host = dlg.rs_host

    if not rs_host:
        rs_host = "192.168.1.2"  # known RS IP from seedlink string

    win = MainWindow(
        rs_host=rs_host,
        port=args.port,
        network=args.network,
        station=args.station,
        window_secs=args.window,
    )
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
