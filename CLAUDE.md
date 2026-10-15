# CLAUDE.md

Project context for continuing work on this repo (any machine). The README covers user-facing features; this file covers how the code fits together, decisions already made, and what's still open.

## What this is

A PyQt6 + pyqtgraph desktop viewer for a Raspberry Shake 3D seismometer (station AM.R8049), used for outreach at CERI, University of Memphis. It shows three live channels, a 3D particle-motion view, and a `--demo` mode that needs no hardware. All app code is in `raspberryshake_viewer.py`. `build_and_install.bat` builds a Windows `.exe` with PyInstaller.

## Data path

```
Shake SEEDLINK :18000 (or DemoSeedlinkServer on 127.0.0.1:<ephemeral>)
  -> SeedlinkListener (thread): raw TCP, "SL"+6-hex header + 512-byte MiniSEED
  -> _decode_miniseed: header + Steim-1/2 -> (channel, start_time, samples)
  -> ChannelAligner.push (keyed by absolute sample index = epoch*100)
   or (default, "auto" transport -- UDP first, SEEDLINK as fallback):
Shake UDP datacast -> :8888 (or DemoUdpSender -> 127.0.0.1:<free port>)
  -> UdpListener (thread): "{'EHZ', <epoch>, n1, n2, ...}" ~25 samples/packet
  -> _parse_rs_udp -> ChannelAligner.push (same as above)
  -> MainWindow._tick, 25 Hz: aligner.pull(4) -> self.buffers (deques)
  -> MainWindow._refresh, 60 fps: WaveformPanel plots
  -> ParticleMotionWindow._update, 30 fps: reads the same buffers
```

## Invariants and gotchas

- **The board's channel labels are swapped.** Stream `EHN` measures physical East and `EHE` measures North (`CHANNEL_LABELS`). Get physical directions from the inverse of `CHANNEL_LABELS`, as `ParticleMotionWindow` and `DemoSeedlinkServer` do. Never hard-code the swap.
- **The MiniSEED header uses standard SEED offsets:** BTIME at 20, nsamp at 30, data offset at 44, blockette offset at 46. The start time includes the time correction (if activity flag 0x02 isn't set) and blockette 1001 microseconds. obspy puts blockette 1001 *before* 1000 when the start has sub-millisecond precision.
- **Steim decoding is verified against obspy.** Three bugs that were in the original code are fixed. Don't reintroduce them:
  1. The control code for data word `w` is `ctrl >> (30 - 2*w)`, for `w` in 1..15. Word 0 is the control word itself.
  2. `diffs[0]` links to the previous record. `x0` is the first sample, and integration starts at `diffs[1]`.
  3. Steim-2 7×4-bit values sit in the low 28 bits (`shift = 24 - 4*i`).
  `_integrate` drops a record whose result doesn't end at `xn`.
- **ChannelAligner rules:**
  - After `_seek()`, every channel's `_start` equals `_cursor`, and `_backlog()` is only valid then.
  - When the slowest channel has no data, *all* channels hold their last value together. Latency grows instead of channels drifting apart.
  - Extra latency is drained by skipping at most one sample per pull.
  - Nothing is emitted before the first data arrives (`_has_pulled`). After a Pause/Resume reset, the last values are held so scrolling continues.
- **Particle motion:**
  - All channels get the same filter (trailing 1 s moving-average removal, about a 0.5 Hz high-pass), so their relative phase is preserved.
  - All axes share one scale.
  - `pyqtgraph.opengl` is imported lazily. Without PyOpenGL, the button shows an install hint instead.
  - Depth cues: shadows on the floor and two walls, a drop line, auto-rotation and a 75° field of view. `_place_walls()` picks the walls on the far side from `view.cameraPosition()` each frame, and only moves the grids when the side changes. Shadows are the trail points with one coordinate pinned to that wall.
  - Auto-rotation pauses while the mouse is held on the view (event filter) and resumes `ROTATE_RESUME` seconds after release.
  - `CAMERA_DIST` 3.8 keeps the cube's near corner inside the view at a 75° field of view, down to about a 520×460 window. Re-check it if you change the field of view.
- **Screenshots of the 3D view:** `QWidget.grab()` doesn't capture `GLTextItem` labels. Use `view.grabFramebuffer()`.
- **Demo:**
  - `DemoQuakes` schedules quakes relative to the time the demo started (the first after about 8 s, then about every 40 s). `trigger()` adds one 1 s out.
  - The server flushes partial records after `MAX_RECORD_SECS` (1 s) and adds a delivery delay per channel (`CHANNEL_LAG`) to exercise the aligner.
- **Display delay is the transport, not the viewer.** Over SEEDLINK the Shake only sends full 512-byte records, which hold 5–7 s of quiet data, so the display runs 5–7 s behind; the aligner adds ~0.2 s (`LATENCY_MARGIN`). `--udp` gets ~0.4 s. Measured 2026-10-15 against fake servers: SEEDLINK quiet-site tap → screen 6.3 s, UDP 0.39 s. Don't try to "fix" SEEDLINK latency in the aligner: the samples don't exist on the laptop until the record arrives. The original code only looked faster because its 2 s staging deque discarded most of each record.
- **Listeners share one interface** (`signals`, `last_packet`, `packet_count`, `_connected`, `port`, `transport_name`, `stop()`). UDP "connected" means a packet arrived within `NO_DATA_SECS`.
- **Transport modes** (`MainWindow(transport=...)`, CLI default `auto`): `auto` starts UDP; `_check_fallback()` (250 ms timer) switches to SEEDLINK after `UDP_FALLBACK_SECS` (5 s) without UDP packets, and back to UDP as soon as a probe `UdpListener(probe=True)` (counts packets, never feeds the aligner) hears any. `--udp` / `--seedlink` force one transport, with no switching.
  - Every switch calls `aligner.reset()`: the transports run ~6 s apart in delay, so continuing from the old cursor would stall (UDP→SEEDLINK) or keep the old high delay (SEEDLINK→UDP).
  - Pause/Resume restarts from UDP in auto mode.
  - The fallback is shown in a blue `ConnectionBanner.set_notice()` row, not in the title, which is truncated at the default width. The amber banner is still the "source is down" warning.
  - `MainWindow(port=...)` is the SEEDLINK port; `udp_port` is separate. Tests must pass `transport=` explicitly (the default is `auto`, which waits for UDP first).
  - Demo: `DemoShake` serves SEEDLINK and/or UDP with one shared `DemoQuakes`, like a real Shake.
- **Python 3.9+** is the target (README). Avoid newer syntax such as `match`.

## Development

```bash
pip install -r requirements-dev.txt   # app deps + pytest + obspy (test-only reference)
pytest                                # 49 tests, ~40 s, headless (conftest sets QT_QPA_PLATFORM=offscreen)
python raspberryshake_viewer.py --demo
```

- The tests cover the decoder (vs obspy), the aligner, the demo encoder and physics, the UDP transport (parser, <1 s delay, alignment), and end-to-end runs (demo → buffers) for both transports.
- `tests/helpers.py` imports obspy lazily, so `test_udp.py` and most of `test_aligner.py` run without obspy.
- Most of the ~40 s is the tests that run a live window for several seconds: the UDP and end-to-end tests, and `test_transport.py`. `test_transport.py` monkeypatches `UDP_FALLBACK_SECS` to 2 s to keep it short. Disabling `_check_fallback` in a mutation check fails its two switching tests. The UDP delay test depends on timing; it passed 3/3 runs, but if it flakes on a slow machine, look there first.
- This machine (EC-DELL, WSL2) has sometimes had no network access mid-session. pip and git push fail then. The system `python3` (conda `my_env`) has the app deps but not pytest or obspy, so install dev requirements into a scratch venv rather than `my_env`.
- The 3D GL window isn't covered, because it can't render offscreen. Check it visually or with `grabFramebuffer()`.
- To show the tests are meaningful, a mutation that shifts one channel by 0.1 s in `ChannelAligner.push` fails three tests. Re-run that check when changing alignment logic.

## Workflow conventions

- The owner commits directly to `main` (no PR flow). Commit or push only when asked.
- Commit messages: a summary line, a wrapped body explaining *why*, and the `Co-Authored-By` trailer.

## Status (as of 2026-10-15)

Done: minor fixes, 3D particle motion, timestamp alignment, Steim decoder fixes, `--demo` mode with a Quake! button, pytest suite, 3D depth cues (shadows, drop line, auto-rotate, wider field of view), low-latency UDP transport (2026-10-15, commit 5ac1b9d; full suite passed 44/44, 3 runs in a row), UDP as the default with automatic SEEDLINK fallback and switch-back (2026-10-15).

Depth-cue ideas suggested but not built, if 3D still feels flat: brightness or color by distance from the camera, a shaded tube instead of a line, three flat side views next to the 3D window.

Never verified on real hardware or Windows. Check these first when you have the Shake:
- Live streaming on a real Shake over SEEDLINK: real record lengths decide the display delay (5–7 s expected on a quiet site). Watch for dropped records, which would mean the `xn` check is failing.
- UDP (the default) on a real Shake: the packet format (`{'EHZ', <epoch>, samples...}`) and the Shake's UDP setup steps in the README come from Raspberry Shake's documented datacast format, not from testing against hardware. Confirm the format, the delay (~0.4 s expected), and the Windows firewall prompt.
- The Windows `.exe` build: `--hidden-import pyqtgraph.opengl` and PyOpenGL were added to `build_and_install.bat` but haven't been tested. `--demo` on the outreach laptop tests everything after the network connection.

Known open items, not started:
- **Hard-coded values:** station `R8049`, location `00` (no `--location` flag) and SSH password `earthday2023` are built in. The clock sync sends that password to `sudo` over SSH.
- **Title cut off:** the header title is truncated at the default 1100 px width, which was true before. The DEMO badge is placed first so it stays visible.
- **No Lock Scale in 3D:** the particle-motion scale is auto only.
