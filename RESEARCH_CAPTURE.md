# Research-Grade CSI Capture for AI, ISAC, and ISCC

The original action CSV remains compatible with the existing cleaning code. Research mode adds a stable experiment ledger instead of embedding every field in every CSI row.

## What was missing

### AI and pattern recognition

The old collector saved an action folder and a timestamped file, but not the independent grouping keys needed to test generalization. Research mode records pseudonymous participant, session, date, room, device identities, geometry, action order, trial identity, fall semantics, and interference condition. `wave` is the demonstrated fall class unless `fall_type` explicitly says `incident_fall`; `move`, `static`, `none`, `front`, and `after` are non-falls.

### ISAC

The CSI row already contains RSSI, rate, MCS, bandwidth, noise floor, channel, AGC/FFT gain, receiver timestamp, CSI length, and receive-state fields. Research mode now summarizes effective sampling rate, sequence gaps, radio fields, RSSI/noise statistics, configured sending mode, and sending interval for each recording.

### ISCC

The firmware now emits one `SYSTEM_TELEMETRY` row per second with device uptime, total CSI callbacks, CSI print-queue drops, queue depth/high-water mark, current free heap, historical minimum free heap, actual send interval, and the compiled sending mode (`espnow_research`, `router_ping`, or `null_data`). In the default `espnow_research` mode it also reports validated research-frame count, application payload bytes, CRC/protocol errors, packet-sequence gaps, last sequence, and observed experiment hash. The host records serial-read-to-CSV-write pipeline latency and host/device recording-span skew. The manifest keeps configured and observed send settings separately so configuration drift is visible. Per-recording summaries include the number of telemetry samples, maximum observed queue depth, and minimum observed free heap. The queue high-water and minimum-free-heap counters are explicitly marked as since-boot values. Device callback/drop deltas use the nearest available 1 Hz telemetry snapshots, so they are approximate at recording boundaries, especially for short trials.

For research-transmitter sessions, set `send_mode` to `espnow_research` and make `transmitter_experiment_id` equal the ID configured with the transmitter's `research_tx` command. Session validation fails if no independently validated research frame is observed, the FNV-1a experiment hash differs, or no valid packet arrives inside the recording. Receiver application goodput is `8 * receiver_research_bytes_delta / recording_duration`; transmitter callback success is only local MAC status and must not be reported as delivery. The embedded `tx_timestamp_us` is transmitter uptime on an unsynchronized monotonic clock, so it does not by itself establish one-way latency.

These are not substitutes for synchronized one-way latency, radio energy, or application throughput. Measure those with clock synchronization, a loopback protocol, an external power monitor, and explicit byte counters when making end-to-end ISCC claims.

## Configure a session

Copy the example without putting names, e-mail addresses, or other direct identifiers in it:

```powershell
Copy-Item tools\config\experiment_config.example.json tools\config\experiment_config.json
```

Change at least `participant_id`, `session_id`, `date`, `room_id`, `receiver_id`, device placement, orientation, distance, sending mode, and sending interval. Use a new `session_id` whenever the participant, date, room, device geometry, radio configuration, or collection block changes materially.

Recommended session ID pattern:

```text
P001-R01-D01-S01
```

Do not infer these fields later from filenames.

## Build and flash the receiver firmware

The new telemetry fields require rebuilding and flashing `console-csi-v6`. The current dependency lock resolves `esp-radar 0.3.4` and ESP-IDF 5.5.5, so use ESP-IDF 5.5.5 (the radar component itself requires at least 5.4):

```powershell
idf.py build
idf.py -p COMx flash
```

The receiver adds only one telemetry line per second. It does not add fields to every CSI row.

## Start research capture

The GUI can be opened without hardware for layout inspection or parameter preparation:

```powershell
python esp_csi_tool.py
```

This starts offline mode. Commands are shown as ignored in the GUI log and no CSI files are collected. If `-p COMx` is supplied but the serial port cannot be opened, the GUI remains open and falls back to the same offline mode.

From `tools`:

```powershell
python esp_csi_tool.py -p COMx `
  --csi_output_type LLTF `
  --experiment-config config\experiment_config.json `
  --data-root data
```

The configured `send_interval_ms` is sent to the receiver at startup. Confirm the intended sender/router setup independently; metadata cannot prove hardware configuration by itself.

Output structure (grouped by location and local capture date):

```text
data/
  R01/
    2026-09-01/
      session_manifest.csv
      14-32-08-125_R01_P001_wave_1.csv
      14-32-08-125_R01_P001_wave_1.meta.json
```

The GUI clean button is disabled in research mode. Existing sessions should be archived or removed with an explicit filesystem operation after validation, never with a broad in-app delete.

## Validate before training

Run:

```powershell
python validate_session.py data\R01\2026-09-01
```

The room/day manifest is an archive index and may contain multiple sessions. Filter one `session_id` before strict core-action coverage validation. See [tools/data/README.md](tools/data/README.md) for file naming, field semantics, and loading examples.

The validator checks required domain keys, duplicate recording IDs and paths, action coverage, sidecars, CSV hashes, frame counts, sequence gaps, explicit fall semantics, and configured-versus-observed sending settings. It writes `validation_report.json` and exits nonzero on failure.
Interrupted recordings are retained with `status=aborted` for diagnosis, but validation fails until they are excluded or recollected. The manifest and sidecar also store hashes for the experiment config and collector plus firmware/chip revision fields reported by `DEVICE_INFO`.

## Collection design

For cross-person, cross-session, and cross-room AI claims:

- collect all six core actions in every session;
- randomize action order;
- collect at least two sessions on different days per participant;
- use at least three rooms and log device geometry;
- reserve a complete participant, room, and date before model selection;
- split by manifest keys, never by individual adjacent recordings;
- keep structurally valid test recordings even when their quality is poor.

For ISAC/ISCC sweeps, change one controlled factor at a time and create a new session/config for each condition. Useful factors include send interval, channel, 20/40 MHz bandwidth, MCS/rate, CSI LTF type, distance, orientation, obstruction, interference load, and compute placement.

## Still recommended for a later firmware revision

- transmitter-side packet sequence and transmit timestamp embedded in payload;
- ESP-NOW send callback success/failure and retry counters;
- received byte and packet counters for application goodput;
- synchronized clocks or ping-pong timestamps for end-to-end latency;
- INA219/INA226 or similar power-monitor samples aligned to recording IDs;
- inference/preprocessing timing and model memory if inference moves onto the ESP32.

Those fields require changes to both sender and receiver protocol or extra hardware. They should not be estimated from current CSI timestamps.
