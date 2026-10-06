# Raspberry Shake Live Waveform Viewer

A lightweight, OS-independent desktop app for real-time seismograph display from a [Raspberry Shake](https://raspberryshake.org/) via SEEDLINK.

Built for Earth Day outreach at the [Center for Earthquake Research and Information (CERI)](https://www.memphis.edu/ceri/), University of Memphis.

![Three-channel waveform display](rshake_icon_preview.png)

---

## Features

- **Three-channel live display** — EHZ (vertical), EHE (east), EHN (north)
- **SEEDLINK over TCP** — reliable, ordered stream; no RS-side UDP configuration needed
- **Auto-discovery** — finds the RS at `192.168.1.2`, `192.168.1.3`, or `rs.local` in parallel
- **SSH clock sync** — checks and fixes RS clock drift before streaming starts
- **Smooth scrolling** — staging deque architecture decouples bursty SEEDLINK delivery from the 60 fps display
- **Shared amplitude scaling** — all three channels use the same Y range for direct comparison
- **Lock Scale** — freeze the Y range before touching the RS so handling spikes don't blow up the scale
- **Pause / Resume** — SEEDLINK disconnects on pause; a flat gap scrolls through the plot; live signal resumes seamlessly after the gap
- **Clear** — wipe all traces while staying connected
- **Windows Desktop shortcut** — one-click `build_and_install.bat` bundles everything into a standalone `.exe` with a custom icon

---

## Requirements

- Python 3.9+
- Raspberry Shake connected to the same local network (router)

```bash
pip install PyQt6 pyqtgraph numpy paramiko
```

---

## Run

```bash
python rshake_viewer.py
```

On first launch a pre-flight dialog:
1. Discovers the RS on the network
2. SSHs in and checks the RS clock against your machine's UTC
3. Fixes clock drift automatically if > 60 s
4. Connects the SEEDLINK stream and starts displaying

---

## Windows Desktop Shortcut

Put these four files in the same folder:

```
rshake_viewer.py
build_and_install.bat
rshake_icon.ico
```

Double-click `build_and_install.bat`. It will:
- Install all Python dependencies
- Build a standalone `RaspberryShakeViewer.exe` via PyInstaller
- Place a shortcut with the custom icon on your Desktop

> **Note:** Windows Defender may show a SmartScreen warning ("unknown publisher") on first run. Click **More info → Run anyway** — this is normal for locally built executables.

> **Note:** If `python` on your PATH is Inkscape's bundled Python (or another app's), the script uses the Windows Python Launcher `py -3` to find the right installation.

---

## Controls

| Control | Action |
|---------|--------|
| **▲ / ▼** per channel | Zoom that channel in / out (×2 per click) |
| **▲ All / ▼ All** | Zoom all channels together |
| **Reset** | Return all channels to 1× |
| **Lock Scale** | Freeze Y range at the current value (turns amber) |
| **Auto Scale** | Return to adaptive scaling |
| **Clear** | Wipe traces; stream continues |
| **Pause** | Disconnect SEEDLINK; flat gap scrolls through plot |
| **Resume** | Reconnect; live signal follows the gap |

---

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--rs-host` | auto | RS hostname or IP |
| `--port` | `18000` | SEEDLINK port |
| `--window` | `60` | Seconds of data visible |
| `--network` | `AM` | SEEDLINK network code |
| `--station` | `R8049` | SEEDLINK station code |
| `--ssh-user` | `myshake` | RS SSH username |
| `--ssh-pass` | `earthday2023` | RS SSH password |
| `--skip-preflight` | off | Skip discovery + clock sync |

---

## Network Setup

Connect the laptop and RS to the same router. The RS will get `192.168.1.2` or `192.168.1.3`; the app probes both in parallel so no manual IP entry is needed.

The RS runs a SEEDLINK server on port 18000 by default — no forwarding rules needed.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Pre-flight can't find RS | Check ethernet cables and RS power; try `ping rs.local` |
| SSH clock sync fails | Verify `--ssh-pass`; RS must be reachable on port 22 |
| "Connected — waiting for first record…" | Normal for a few seconds; SEEDLINK buffers before sending |
| Amber banner stays on | RS may have rebooted; app reconnects automatically |
| Waveforms flat after launch | Wait ~5 s for initial auto-range to kick in |

---

## License

MIT
