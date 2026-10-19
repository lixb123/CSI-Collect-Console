"""Audit and compare router-ping and null-data ESP32 CSI stability.

Outputs include pre-filter PHY/RSSI/interval diagnostics, an exact common-PHY
comparison, per-frame RMS-normalized amplitude, detrended phase, per-subcarrier
coefficients of variation, and machine-readable arrays. ESP-IDF CSI byte order
is interpreted as [imaginary, real].

The current CSV schema has no 802.11 Retry bit or transmitter sequence number.
Therefore only duplicated records and non-increasing receiver timestamps can be
removed. Strict link-layer retransmission removal requires future firmware
metadata and is explicitly reported as unavailable.
"""
from __future__ import annotations

import argparse
import ast
import base64
import binascii
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from complex_csi import sanitized_phase


PHY_FIELDS = ("rate", "sig_mode", "mcs", "cwb", "channel_primary", "channel_secondary", "len")
MODE_LABELS = {"router_ping": "Router Ping", "null_data": "Null Data"}


def _number(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_csi(value: str) -> np.ndarray:
    value = (value or "").strip()
    if not value:
        return np.empty(0, dtype=np.float32)
    if value.startswith("["):
        return np.asarray(ast.literal_eval(value), dtype=np.float32).reshape(-1)
    raw = base64.b64decode(value, validate=True)
    return np.frombuffer(raw, dtype=np.int8).astype(np.float32)


def _signature(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple((row.get(field) or "").strip() for field in PHY_FIELDS)


def _payload_digest(values: np.ndarray) -> str:
    return hashlib.blake2s(values.astype(np.int8, copy=False).tobytes(), digest_size=8).hexdigest()


def read_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    audit: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    seen_records: set[tuple[str, str, str]] = set()
    previous_timestamp_by_mac: dict[str, float] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            audit["csv_rows"] += 1
            if (row.get("type") or "CSI_DATA") != "CSI_DATA":
                audit["non_csi_rows"] += 1
                continue
            try:
                values = _parse_csi(row.get("data", ""))
            except (ValueError, SyntaxError, UnicodeDecodeError, binascii.Error):
                audit["decode_errors"] += 1
                continue
            declared_len = _integer(row.get("len"), len(values))
            if values.size == 0 or values.size % 2 or declared_len != values.size:
                audit["invalid_length_rows"] += 1
                continue
            if _integer(row.get("first_word_invalid")) != 0 or _integer(row.get("rx_state")) != 0:
                audit["receiver_invalid_rows"] += 1
                continue
            mac = (row.get("mac") or "").lower()
            timestamp_text = (row.get("local_timestamp") or "").strip()
            digest = _payload_digest(values)
            record_key = (mac, timestamp_text, digest)
            if record_key in seen_records:
                audit["exact_duplicate_rows"] += 1
                continue
            seen_records.add(record_key)
            timestamp = _number(timestamp_text)
            previous = previous_timestamp_by_mac.get(mac)
            if math.isfinite(timestamp) and previous is not None and timestamp <= previous:
                audit["non_increasing_timestamp_rows"] += 1
                continue
            if math.isfinite(timestamp):
                previous_timestamp_by_mac[mac] = timestamp
            row["_values"] = values
            row["_timestamp"] = timestamp
            rows.append(row)
            audit["valid_rows"] += 1
    # rx_seq is 12-bit on this target and wraps during a 60-second capture.
    # Treat only a repeated (MAC, rx_seq) within 100 ms as a retransmission
    # candidate. This preserves ordinary sequence wraparound.
    kept: list[dict[str, Any]] = []
    last_rx_seq_timestamp: dict[tuple[str, str], float] = {}
    populated_rx_seq = 0
    for row in rows:
        rx_seq = (row.get("rx_seq") or "").strip()
        if not rx_seq:
            kept.append(row)
            continue
        populated_rx_seq += 1
        key = ((row.get("mac") or "").lower(), rx_seq)
        previous = last_rx_seq_timestamp.get(key)
        timestamp = row["_timestamp"]
        if previous is not None and math.isfinite(timestamp) and 0 <= timestamp - previous <= 100_000:
            audit["duplicate_rx_seq_rows"] += 1
            audit["valid_rows"] -= 1
            last_rx_seq_timestamp[key] = timestamp
            continue
        last_rx_seq_timestamp[key] = timestamp
        kept.append(row)
    rows = kept
    audit["rx_seq_candidate_filter_applied"] = int(populated_rx_seq > 0)
    audit["strict_retry_filter_available"] = 0
    return rows, dict(audit)


def _choose_common_selection(
    router_rows: list[dict[str, Any]], null_rows: list[dict[str, Any]], requested_mac: str | None
) -> tuple[str | None, tuple[str, ...] | None]:
    router_macs = Counter((row.get("mac") or "").lower() for row in router_rows)
    null_macs = Counter((row.get("mac") or "").lower() for row in null_rows)
    if requested_mac:
        mac = requested_mac.lower()
    else:
        common_macs = set(router_macs) & set(null_macs)
        mac = max(common_macs, key=lambda item: min(router_macs[item], null_macs[item]), default=None)
    if mac is None:
        return None, None
    router_signatures = Counter(_signature(row) for row in router_rows if (row.get("mac") or "").lower() == mac)
    null_signatures = Counter(_signature(row) for row in null_rows if (row.get("mac") or "").lower() == mac)
    common_signatures = set(router_signatures) & set(null_signatures)
    signature = max(
        common_signatures,
        key=lambda item: (min(router_signatures[item], null_signatures[item]), router_signatures[item] + null_signatures[item]),
        default=None,
    )
    return mac, signature


def _filtered_rows(rows: list[dict[str, Any]], mac: str | None, signature: tuple[str, ...] | None) -> list[dict[str, Any]]:
    if mac is None or signature is None:
        return []
    return [row for row in rows if (row.get("mac") or "").lower() == mac and _signature(row) == signature]


def _active_subcarriers(complex_count: int) -> tuple[np.ndarray, np.ndarray, str]:
    if complex_count == 52:
        labels = np.r_[np.arange(-26, 0), np.arange(1, 27)]
        return np.arange(52), labels, "esp-radar filtered LLTF: DC/guard/null bins already absent"
    if complex_count == 64:
        labels64 = np.arange(-32, 32)
        mask = ((labels64 >= -26) & (labels64 <= -1)) | ((labels64 >= 1) & (labels64 <= 26))
        return np.flatnonzero(mask), labels64[mask], "removed DC and 11 guard/null bins from 64-bin LLTF"
    indices = np.arange(complex_count)
    return indices, indices, "unknown layout: retained all bins; use 52-bin esp-radar LLTF for verified DC/null removal"


def _arrays(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    widths = Counter(len(row["_values"]) for row in rows)
    width = widths.most_common(1)[0][0]
    consistent = [row for row in rows if len(row["_values"]) == width]
    raw = np.stack([row["_values"] for row in consistent]).astype(np.float64)
    complex_values = raw[:, 1::2] + 1j * raw[:, 0::2]
    active_positions, labels, removal_note = _active_subcarriers(complex_values.shape[1])
    complex_values = complex_values[:, active_positions]
    amplitude = np.abs(complex_values)
    frame_rms = np.sqrt(np.mean(amplitude**2, axis=1))
    normalized = amplitude / np.maximum(frame_rms[:, None], 1e-9)
    calibrated_phase = np.stack([sanitized_phase(row["_values"])[0][active_positions] for row in consistent])
    timestamp = np.asarray([row["_timestamp"] for row in consistent], dtype=np.float64)
    return {
        "raw": raw,
        "amplitude": amplitude,
        "amplitude_normalized": normalized,
        "phase_calibrated": calibrated_phase,
        "frame_rms": frame_rms,
        "timestamp_us": timestamp,
        "interval_us": np.r_[np.nan, np.diff(timestamp)],
        "rssi": np.asarray([_number(row.get("rssi")) for row in consistent]),
        "noise_floor": np.asarray([_number(row.get("noise_floor")) for row in consistent]),
        "agc_gain": np.asarray([_number(row.get("agc_gain")) for row in consistent]),
        "fft_gain": np.asarray([_number(row.get("fft_gain")) for row in consistent]),
        "rate": np.asarray([_number(row.get("rate")) for row in consistent]),
        "subcarrier": labels,
        "subcarrier_removal_note": removal_note,
        "consistent_rows": consistent,
    }


def _safe_correlation(a: np.ndarray, b: np.ndarray) -> float | None:
    mask = np.isfinite(a) & np.isfinite(b)
    if mask.sum() < 3 or np.std(a[mask]) == 0 or np.std(b[mask]) == 0:
        return None
    return float(np.corrcoef(a[mask], b[mask])[0, 1])


def _finite_mean_std(values: np.ndarray) -> tuple[float | None, float | None]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None, None
    return float(np.mean(finite)), float(np.std(finite))


def _summary(rows: list[dict[str, Any]], arrays: dict[str, Any]) -> dict[str, Any]:
    if not rows or not arrays:
        return {"frames": 0}
    amplitude = arrays["amplitude"]
    normalized = arrays["amplitude_normalized"]
    raw = arrays["raw"]
    frame_rms = arrays["frame_rms"]
    valid_intervals = arrays["interval_us"][np.isfinite(arrays["interval_us"]) & (arrays["interval_us"] > 0)]
    raw_cv = np.std(amplitude, axis=0) / np.maximum(np.mean(amplitude, axis=0), 1e-9)
    norm_cv = np.std(normalized, axis=0) / np.maximum(np.mean(normalized, axis=0), 1e-9)
    rate = arrays["rate"]
    rssi_mean, rssi_std = _finite_mean_std(arrays["rssi"])
    noise_mean, noise_std = _finite_mean_std(arrays["noise_floor"])
    agc_mean, agc_std = _finite_mean_std(arrays["agc_gain"])
    return {
        "frames": int(amplitude.shape[0]),
        "duration_s": float((arrays["timestamp_us"][-1] - arrays["timestamp_us"][0]) / 1e6) if amplitude.shape[0] > 1 else 0.0,
        "subcarriers": int(amplitude.shape[1]),
        "subcarrier_removal_note": arrays["subcarrier_removal_note"],
        "amplitude_mean": float(np.mean(amplitude)),
        "raw_amplitude_cv_mean": float(np.mean(raw_cv)),
        "rms_normalized_amplitude_cv_mean": float(np.mean(norm_cv)),
        "frame_rms_mean": float(np.mean(frame_rms)),
        "frame_rms_std": float(np.std(frame_rms)),
        "frame_rms_cv": float(np.std(frame_rms) / max(np.mean(frame_rms), 1e-9)),
        "saturation_fraction": float(np.mean((raw == -128) | (raw == 127))),
        "frames_with_saturation_fraction": float(np.mean(np.any((raw == -128) | (raw == 127), axis=1))),
        "median_interval_us": float(np.median(valid_intervals)) if valid_intervals.size else None,
        "interval_mean_us": float(np.mean(valid_intervals)) if valid_intervals.size else None,
        "interval_std_us": float(np.std(valid_intervals)) if valid_intervals.size else None,
        "interval_cv": float(np.std(valid_intervals) / max(np.mean(valid_intervals), 1e-9)) if valid_intervals.size else None,
        "rssi_mean": rssi_mean,
        "rssi_std": rssi_std,
        "noise_floor_mean": noise_mean,
        "noise_floor_std": noise_std,
        "agc_gain_mean": agc_mean,
        "agc_gain_std": agc_std,
        "rate_values": sorted({float(value) for value in rate[np.isfinite(rate)]}),
        "rate_switch_count": int(np.count_nonzero(np.diff(rate))) if rate.size > 1 else 0,
        "correlation_frame_rms_rssi": _safe_correlation(frame_rms, arrays["rssi"]),
        "correlation_frame_rms_agc_gain": _safe_correlation(frame_rms, arrays["agc_gain"]),
        "phase_temporal_step_rms": float(np.sqrt(np.mean(np.angle(np.exp(1j * np.diff(arrays["phase_calibrated"], axis=0))) ** 2))) if amplitude.shape[0] > 1 else None,
        "raw_cv_per_subcarrier": raw_cv.tolist(),
        "normalized_cv_per_subcarrier": norm_cv.tolist(),
    }


def _time_axis(timestamp_us: np.ndarray) -> np.ndarray:
    return (timestamp_us - timestamp_us[0]) / 1e6 if timestamp_us.size else timestamp_us


def _plot_prefilter_diagnostics(mode: str, rows: list[dict[str, Any]], mac: str | None, output: Path) -> dict[str, Any]:
    if not rows:
        return {"frames": 0}
    if mac is None:
        return {"frames": 0}
    selected = [row for row in rows if (row.get("mac") or "").lower() == mac]
    timestamp = np.asarray([row["_timestamp"] for row in selected])
    time_s = _time_axis(timestamp)
    interval_ms = np.r_[np.nan, np.diff(timestamp) / 1000.0]
    rssi = np.asarray([_number(row.get("rssi")) for row in selected])
    rate = np.asarray([_number(row.get("rate")) for row in selected])
    noise = np.asarray([_number(row.get("noise_floor")) for row in selected])
    agc = np.asarray([_number(row.get("agc_gain")) for row in selected])
    rms = np.asarray([np.sqrt(np.mean(np.abs(row["_values"][1::2] + 1j * row["_values"][0::2]) ** 2)) for row in selected])
    fig, axes = plt.subplots(6, 1, figsize=(13, 11), sharex=True, constrained_layout=True)
    series = [(rms, "CSI frame RMS", "amplitude"), (rssi, "RSSI", "dBm"), (interval_ms, "Packet interval", "ms"), (rate, "Rate code", "code"), (noise, "Noise floor", "dBm"), (agc, "AGC gain", "code")]
    for axis, (values, title, ylabel) in zip(axes, series):
        axis.plot(time_s, values, linewidth=0.8)
        axis.set_ylabel(ylabel)
        axis.set_title(title, loc="left", fontsize=9)
        axis.grid(alpha=0.25)
    axes[-1].set_xlabel("Time since first frame (s)")
    fig.suptitle(f"{MODE_LABELS[mode]} pre-filter diagnostics — MAC {mac}")
    fig.savefig(output, dpi=180)
    plt.close(fig)
    return {
        "frames": len(selected),
        "mac": mac,
        "rate_values": sorted({float(value) for value in rate[np.isfinite(rate)]}),
        "rate_switch_count": int(np.count_nonzero(np.diff(rate))),
        "correlation_frame_rms_rssi": _safe_correlation(rms, rssi),
        "correlation_frame_rms_agc_gain": _safe_correlation(rms, agc),
        "correlation_frame_rms_rate": _safe_correlation(rms, rate),
    }


def _save_processed(mode: str, arrays: dict[str, Any], output_dir: Path) -> None:
    np.savez_compressed(
        output_dir / f"{mode}_processed.npz",
        amplitude=arrays["amplitude"].astype(np.float32),
        amplitude_normalized=arrays["amplitude_normalized"].astype(np.float32),
        phase_calibrated=arrays["phase_calibrated"].astype(np.float32),
        timestamp_us=arrays["timestamp_us"],
        interval_us=arrays["interval_us"],
        rssi=arrays["rssi"],
        noise_floor=arrays["noise_floor"],
        agc_gain=arrays["agc_gain"],
        fft_gain=arrays["fft_gain"],
        rate=arrays["rate"],
        subcarrier=arrays["subcarrier"],
    )
    fields = ["seq", "timestamp", "mac", *PHY_FIELDS, "rssi", "noise_floor", "agc_gain", "fft_gain", "local_timestamp"]
    with (output_dir / f"{mode}_selected_frames.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(arrays["consistent_rows"])


def _plot_cv(results: dict[str, dict[str, Any]], arrays: dict[str, dict[str, Any]], output: Path) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True, constrained_layout=True)
    for mode, color in (("router_ping", "#d95f02"), ("null_data", "#1b9e77")):
        axes[0].plot(arrays[mode]["subcarrier"], results[mode]["raw_cv_per_subcarrier"], marker=".", linewidth=1, label=MODE_LABELS[mode], color=color)
        axes[1].plot(arrays[mode]["subcarrier"], results[mode]["normalized_cv_per_subcarrier"], marker=".", linewidth=1, label=MODE_LABELS[mode], color=color)
    axes[0].set_title("Raw amplitude coefficient of variation")
    axes[1].set_title("Per-frame RMS-normalized amplitude coefficient of variation")
    axes[1].set_xlabel("Active OFDM subcarrier")
    for axis in axes:
        axis.set_ylabel("CV = std / mean")
        axis.grid(alpha=0.25)
        axis.legend()
    fig.savefig(output, dpi=180)
    plt.close(fig)


def _plot_processed(mode: str, arrays: dict[str, Any], output: Path) -> None:
    time_s = _time_axis(arrays["timestamp_us"])
    labels = arrays["subcarrier"]
    extent = [float(time_s[0]), float(time_s[-1]), float(labels[0]), float(labels[-1])]
    fig, axes = plt.subplots(2, 1, figsize=(13, 8), sharex=True, constrained_layout=True)
    amp_image = axes[0].imshow(
        arrays["amplitude_normalized"].T,
        aspect="auto",
        origin="lower",
        extent=extent,
        cmap="viridis",
    )
    phase_image = axes[1].imshow(
        arrays["phase_calibrated"].T,
        aspect="auto",
        origin="lower",
        extent=extent,
        cmap="twilight",
        vmin=-math.pi,
        vmax=math.pi,
    )
    axes[0].set_title("Per-frame RMS-normalized amplitude")
    axes[1].set_title("Phase after unwrap and per-frame linear-trend removal")
    axes[1].set_xlabel("Time since first selected frame (s)")
    for axis in axes:
        axis.set_ylabel("Active OFDM subcarrier")
    fig.colorbar(amp_image, ax=axes[0], label="Normalized amplitude")
    fig.colorbar(phase_image, ax=axes[1], label="Residual phase (rad)")
    fig.suptitle(f"{MODE_LABELS[mode]} processed CSI")
    fig.savefig(output, dpi=180)
    plt.close(fig)


def compare_sessions(router_path: Path, null_path: Path, output_dir: Path, common_mac: str | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    router_rows, router_audit = read_rows(router_path)
    null_rows, null_audit = read_rows(null_path)
    selected_mac, signature = _choose_common_selection(router_rows, null_rows, common_mac)
    prefilter = {
        "router_ping": _plot_prefilter_diagnostics("router_ping", router_rows, selected_mac, output_dir / "router_ping_prefilter_diagnostics.png"),
        "null_data": _plot_prefilter_diagnostics("null_data", null_rows, selected_mac, output_dir / "null_data_prefilter_diagnostics.png"),
    }
    selected = {
        "router_ping": _filtered_rows(router_rows, selected_mac, signature),
        "null_data": _filtered_rows(null_rows, selected_mac, signature),
    }
    arrays = {mode: _arrays(rows) for mode, rows in selected.items()}
    summaries = {mode: _summary(selected[mode], arrays[mode]) for mode in MODE_LABELS}
    if all(summaries[mode].get("frames", 0) for mode in MODE_LABELS):
        for mode in MODE_LABELS:
            _save_processed(mode, arrays[mode], output_dir)
            _plot_processed(mode, arrays[mode], output_dir / f"{mode}_processed_csi.png")
        _plot_cv(summaries, arrays, output_dir / "subcarrier_cv_comparison.png")
    result = {
        "selection": {
            "common_mac": selected_mac,
            "common_phy_fields": PHY_FIELDS,
            "common_phy_signature": dict(zip(PHY_FIELDS, signature)) if signature else None,
            "strict_80211_retry_filter_available": False,
            "retry_filter_limit": "ESP-IDF exposes rx_seq but not the 802.11 Retry bit. New captures remove repeated (MAC, rx_seq) candidates; old captures only remove exact duplicates and non-increasing timestamps.",
            "causal_claim_allowed": False,
        },
        "input_audit": {"router_ping": router_audit, "null_data": null_audit},
        "prefilter_diagnostics": prefilter,
        "router_ping": {"path": str(router_path), **summaries["router_ping"]},
        "null_data": {"path": str(null_path), **summaries["null_data"]},
        "interpretation": "Sequential sessions are diagnostic only. A causal trigger-mode comparison requires randomized 60-second static captures under unchanged placement and firmware.",
    }
    (output_dir / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("router_ping", type=Path)
    parser.add_argument("null_data", type=Path)
    parser.add_argument("--common-mac")
    parser.add_argument("--output-dir", type=Path, default=Path("csi_stability_analysis"))
    args = parser.parse_args()
    result = compare_sessions(args.router_ping, args.null_data, args.output_dir, args.common_mac)
    print(json.dumps(result, indent=2))
    return 0 if result["router_ping"].get("frames", 0) and result["null_data"].get("frames", 0) else 2


if __name__ == "__main__":
    raise SystemExit(main())
