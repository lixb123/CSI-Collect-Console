"""Capture a diagnostic static router-ping/null-data pair from an ESP32.

This utility writes raw CSI CSV files outside the formal experiment dataset.
It assumes the room is kept static by the operator; occupancy is not sensed or
verified by the script.
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime
from pathlib import Path

import serial


COLUMNS = [
    "type", "seq", "timestamp", "taget_seq", "taget", "mac", "rssi", "rate",
    "sig_mode", "mcs", "cwb", "smoothing", "not_sounding", "aggregation",
    "stbc", "fec_coding", "sgi", "noise_floor", "ampdu_cnt", "channel_primary",
    "channel_secondary", "local_timestamp", "ant", "sig_len", "rx_state",
    "agc_gain", "fft_gain", "rx_seq", "len", "first_word_invalid", "data",
]


def _read_line(port: serial.Serial) -> str:
    return port.readline().decode("utf-8", errors="replace").strip()


def _send(port: serial.Serial, command: str) -> None:
    port.write((command + "\r\n").encode("ascii"))
    port.flush()


def wait_for_repl(port: serial.Serial, timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = _read_line(port)
        if line.startswith("SYSTEM_TELEMETRY,") or "csi>" in line:
            return True
    return False


def capture_mode(
    port: serial.Serial,
    mode: str,
    duration_s: float,
    settle_s: float,
    output: Path,
    raw_output: Path | None = None,
) -> dict[str, object]:
    _send(port, f"radar --send_mode {mode}")
    active_marker = f"CSI trigger mode active: {mode}"
    deadline = time.monotonic() + max(settle_s, 8.0)
    active_seen = False
    while time.monotonic() < deadline:
        line = _read_line(port)
        if active_marker in line:
            active_seen = True
        if active_seen and time.monotonic() >= deadline - max(0.0, 8.0 - settle_s):
            break
    # Drain queued lines so each file starts after the settling window.
    port.reset_input_buffer()
    started = time.monotonic()
    rows = 0
    parse_errors = 0
    panic = False
    output.parent.mkdir(parents=True, exist_ok=True)
    raw_handle = raw_output.open("w", encoding="utf-8") if raw_output else None
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        while time.monotonic() - started < duration_s:
            line = _read_line(port)
            if not line:
                continue
            if raw_handle is not None:
                raw_handle.write(line + "\n")
                raw_handle.flush()
            if "Guru Meditation" in line or "Rebooting..." in line:
                panic = True
            if not line.startswith("CSI_DATA,"):
                continue
            try:
                parsed = next(csv.reader([line]))
            except csv.Error:
                parse_errors += 1
                continue
            if len(parsed) == len(COLUMNS) - 1:
                parsed.insert(COLUMNS.index("rx_seq"), "")
            if len(parsed) != len(COLUMNS):
                parse_errors += 1
                continue
            writer.writerow(parsed)
            rows += 1
    if raw_handle is not None:
        raw_handle.close()
    return {
        "mode": mode,
        "path": str(output),
        "duration_s": duration_s,
        "rows": rows,
        "parse_errors": parse_errors,
        "active_marker_seen": active_seen,
        "firmware_panic_seen": panic,
    }


def connect_wifi(port: serial.Serial, ssid: str, password: str) -> bool:
    quoted_ssid = ssid.replace("\\", "\\\\").replace('"', '\\"')
    quoted_password = password.replace("\\", "\\\\").replace('"', '\\"')
    _send(port, f'wifi_config --ssid "{quoted_ssid}" --password "{quoted_password}"')
    deadline = time.monotonic() + 15.0
    connected = False
    status_lines: list[str] = []
    while time.monotonic() < deadline:
        line = _read_line(port)
        if line and password not in line and not line.startswith("CSI_DATA,"):
            status_lines.append(line)
        if "IP_EVENT_STA_GOT_IP" in line:
            connected = True
        if line.startswith("SYSTEM_TELEMETRY,") and line.endswith(",1"):
            connected = True
        if connected:
            return True
    for line in status_lines[-40:]:
        print(line)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM15")
    parser.add_argument("--baud", type=int, default=2_000_000)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--settle", type=float, default=3.0)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--ssid")
    parser.add_argument("--password-env", default="CSI_DIAGNOSTIC_WIFI_PASSWORD")
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir or Path("../diagnostic_capture") / f"static_pair_{stamp}"
    with serial.Serial(args.port, args.baud, timeout=0.1) as port:
        if not wait_for_repl(port):
            print("ESP32 console did not become ready within 8 seconds.")
            return 4
        port.reset_input_buffer()
        if args.ssid:
            password = os.environ.get(args.password_env)
            if password is None:
                raise SystemExit(f"Missing password environment variable: {args.password_env}")
            if not connect_wifi(port, args.ssid, password):
                print("Wi-Fi connection did not reach wifi_connected=1 within 15 seconds.")
                return 3
        _send(port, "radar --csi_output_type LLTF --csi_output_format base64")
        time.sleep(0.5)
        _send(port, "radar --physics_mode")
        time.sleep(0.5)
        results = []
        for mode in ("router_ping", "null_data"):
            result = capture_mode(
                port,
                mode,
                args.duration,
                args.settle,
                output_dir / f"{mode}.csv",
                output_dir / f"{mode}.raw.txt",
            )
            results.append(result)
            print(result, flush=True)
    if any(result["firmware_panic_seen"] or result["rows"] == 0 for result in results):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
