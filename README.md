# CSI-Collect-Console

For AI-focused experiments, an ordinary fixed Wi-Fi router is the default transmitter; the ESP32 `csi_send` firmware is optional. Complex I/Q is captured by the ESP32-S3 receiver and does not require an ESP32 transmitter.

[中文说明](README_cn.md) · [Research capture details](RESEARCH_CAPTURE.md) · [Collection quality](tools/COLLECTION_QUALITY.md) · [Complex I/Q physics capture (Chinese)](PHYSICS_CSI_CAPTURE_CN.md) · [Data format](tools/data/README.md)

`CSI-Collect-Console` is an ESP32-S3 Wi-Fi CSI collection system for AI, ISAC, and ISCC experiments. It includes:

- an ESP-NOW research transmitter (`csi_send`);
- a CSI receiver and serial telemetry firmware (repository root);
- a PyQt5 desktop collector (`tools/esp_csi_tool.py`);
- bilingual Chinese/English UI, remembered collection settings, per-trial wait/recording countdowns, and structured experiment metadata;
- a runtime trigger selector for `router_ping` and `null_data`, with no receiver reflash required when switching modes;
- an optional ESP32-S3 SoftAP transmitter that can be selected through the existing SSID/password controls;
- room/date-organized CSV exports, JSON sidecars, a day-level manifest, and target-link quality gates.

The historical downstream cleaning and benchmark artifacts are maintained in [Leakage-Aware Selective Learning for Resource-Constrained Wi-Fi CSI Fall Detection](https://github.com/lixb123/Leakage-Aware-Selective-Learning-for-Resource-Constrained-Wi-Fi-CSI-Fall-Detection). This repository is the acquisition source. Earlier downstream releases use the legacy labels `wave/move/static/none/front/after`, so new fine-grained labels must be mapped explicitly.

## Hardware and software

- One ESP32-S3 receiver and a fixed Wi-Fi router for the default capture path; the ESP32-S3 research transmitter is optional.
- A receiver USB/UART connection at 2,000,000 baud. The optional transmitter needs a second connection for configuration.
- ESP-IDF 5.5.5. The locked `esp-radar` dependency requires ESP-IDF 5.4 or newer.
- Python 3.10+ with the packages in `tools/requirements.txt`.

The ESP-NOW research profile uses source MAC `1a:00:00:00:00:00` and defaults to Wi-Fi channel 11. Keep both boards on the same channel when using that profile.

The receiver uses the checked-in [esp-radar 0.3.4 patch](vendor/esp-radar/PATCHES.md) to preserve packet `rx_seq` and the tested ACK filtering behavior. The upstream license is retained in that directory.

## Clone and prepare

```powershell
git clone https://github.com/lixb123/CSI-Collect-Console.git
cd CSI-Collect-Console
```

Open an ESP-IDF 5.5.5 terminal before running `idf.py` commands.

## Optional research transmitter

```powershell
cd csi_send
idf.py -B build-research-s3 set-target esp32s3
idf.py -B build-research-s3 build
idf.py -B build-research-s3 -p COM_TX flash monitor
```

Replace `COM_TX` with the transmitter UART port. The command above builds the default SoftAP profile for router-connected capture. For an ESP-NOW research transmitter, build its separate profile instead:

```powershell
idf.py -B build-espnow -D IDF_TARGET=esp32s3 -D SDKCONFIG=sdkconfig.espnow -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.espnow.defaults" build
idf.py -B build-espnow -p COM_TX flash monitor
```

Configure the ESP-NOW research session in its monitor:

```text
research_tx --stop
research_tx --experiment_id P001-R01-D01-S01 --interval_ms 10 --payload_length 32 --reset_counters
research_tx --start
research_tx --status
```

The experiment ID must match `transmitter_experiment_id` in the receiver experiment JSON. See [csi_send/README.md](csi_send/README.md) for protocol fields and counter semantics.

## Build and flash the receiver

From the repository root:

```powershell
idf.py -B build-research-rx set-target esp32s3
idf.py -B build-research-rx build
idf.py -B build-research-rx -p COM_RX flash
```

Replace `COM_RX` with the receiver UART port. Close `idf.py monitor` before the GUI opens the same port.

For ESP-NOW, build and flash the matching receiver profile from the repository root:

```powershell
idf.py -B build-rx-espnow -D IDF_TARGET=esp32s3 -D SDKCONFIG=sdkconfig.espnow -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.espnow.defaults" build
idf.py -B build-rx-espnow -p COM_RX flash
```

Router and ESP-NOW profiles are distinct firmware images. The ESP-NOW collector checks the receiver mode and research-frame telemetry before recording.

## Flashing an existing build

Use `idf.py flash` or the exact build directory's `flasher_args.json` for both boards. Partition layouts differ by profile, so do not reuse offsets from another build.

## Run the GUI

```powershell
cd tools
python -m pip install -r requirements.txt
python esp_csi_tool.py
```

The last command opens offline mode without an ESP32, which is useful for configuring the layout and collection fields. To capture from the receiver:

```powershell
python esp_csi_tool.py -p COM_RX
```

The default window opens in English with Radar model enabled and Wi-Fi Auto connect disabled. The Collection panel is independent from Radar model: Activity occupies its own row, Raw data and Collection start at equal widths, and disabling Radar model hides its complete panel and title while leaving collection controls active. See [tools/GUI使用说明_CN.md](tools/GUI使用说明_CN.md) for the interface, curve colors, units, and operator procedure.

![CSI-Collect-Console actual interface](docs/_static/csi-collector-interface.png)

The screenshot shows the actual collector window in offline mode: Raw data and Collection share the upper display area, Activity uses a full row inside Collection, and Radar model is an independent lower panel.

The trigger dropdown can be changed at runtime:

- `router_ping` works with an ordinary router or the optional ESP32-S3 SoftAP and has the broadest compatibility;
- `null_data` sends 802.11 Null Data frames. It is available for connected-link diagnostics, while formal recording is blocked by default until that source is validated and `allow_null_data_collection` is explicitly enabled.

Keep one trigger mode for a session. Start a new session after changing it, and repeat the empty-room and static-person reference captures. The equivalent serial commands are `radar --send_mode router_ping` and `radar --send_mode null_data`.

For a dedicated SoftAP transmitter, see [ESP32S3_SOFTAP_CSI_GUIDE_CN.md](ESP32S3_SOFTAP_CSI_GUIDE_CN.md). The receiver GUI does not need a new control: disconnect, enter the SoftAP SSID and WPA2 password, then connect using the existing Wi-Fi controls.

For research metadata and an auditable manifest:

```powershell
Copy-Item config\experiment_config.example.json config\experiment_config.json
# Edit the copied file, then:
python esp_csi_tool.py -p COM_RX `
  --csi_output_type LLTF `
  --experiment-config config\experiment_config.json `
  --data-root data
```

Use pseudonymous participant IDs such as `P001`; do not enter names or contact details. The UI remembers its language, participant/location labels, selected action, wait time, recording duration, and trial count. Personal GUI and experiment configuration files are ignored by Git because the GUI configuration may contain a Wi-Fi password.

## Collection workflow

1. Confirm the receiver is connected to the intended AP. For ESP-NOW, match TX/RX channel and experiment ID.
2. Enter participant, location, receiver, and Tx-Rx distance (m) labels.
3. Select an action. The machine label remains English even when the UI is Chinese.
4. Set the wait time, recording duration, and number of trials. Use Serial Refresh after connecting or disconnecting a USB/UART board.
5. Press Start. Each trial runs `wait countdown -> recording countdown`, then repeats.
6. Verify the target MAC, target frame rate, mode stability, and static-person baseline before collecting actions. Inspect each recording's quality status and validate the manifest before training.

Output example:

```text
tools/data/
└── R01/
    └── 2026-09-01/
        ├── 14-32-08-125_R01_P001_fall_forward_1.csv
        ├── 14-32-08-125_R01_P001_fall_forward_1.meta.json
        └── session_manifest.csv
```

CSV rows include the PC timestamp, receiver monotonic timestamp, radio metadata, and decoded CSI bytes. See [tools/data/README.md](tools/data/README.md) for every file's role, field interpretation, labels, and loading example.

## Validate and process

```powershell
cd tools
python validate_session.py data\R01\2026-09-01
python validate_all_sessions.py data
python export_training_data.py data\R01\2026-09-01
```

The room/day manifest can index multiple sessions. Use `--session-id` with `validate_session.py` to check one session. The exporter retains only completed recordings that pass the quality and target-link checks; it does not modify source CSVs. See [collection quality](tools/COLLECTION_QUALITY.md) for retake criteria.

For cleaning and benchmark reproduction, continue with:

- [Conference processing repository](https://github.com/lixb123/Leakage-Aware-Selective-Learning-for-Resource-Constrained-Wi-Fi-CSI-Fall-Detection)

## Repository hygiene

Build output, managed dependencies, runtime logs, CSI recordings, and personal configuration files are excluded. Copy `tools/config/gui_config.example.json` only if you want to pre-create a local GUI configuration; otherwise the GUI creates `gui_config.json` on first run.

## License and upstream components

Project-specific code and documentation are provided under the MIT License. ESP-IDF, Espressif example code, and managed components retain their respective upstream licenses and notices.
