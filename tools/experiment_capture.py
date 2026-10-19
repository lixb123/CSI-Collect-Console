"""Reproducible experiment ledger for Wi-Fi CSI collection.

The original per-recording CSI CSV schema is intentionally left unchanged.
This module writes one JSON sidecar per recording and an append-only
``session_manifest.csv`` with AI domain labels, ISAC link summaries, and ISCC
host/device telemetry. Participant identifiers must be pseudonyms.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import re
import socket
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from complex_csi import ComplexCSIQuality, complex_layout_metadata
from acquisition_quality import acquisition_defaults, baseline_fingerprint, LinkTiming


SCHEMA_VERSION = "csi-experiment-manifest/1.1"
FALL_ACTION = "wave"
FALL_ACTIONS = {"wave", "fall_forward", "fall_backward", "fall_left", "fall_right"}
NON_FALL_ACTIONS = {
    "move", "static", "none", "front", "after", "left", "right", "go", "jump",
    "sit down", "stand up", "climb up", "bend", "pick_up", "lie_down", "turn",
    "walk_slow", "walk_fast",
}
MANIFEST_COLUMNS = [
    "schema_version", "recording_id", "relative_csv_path", "sidecar_path",
    "sha256", "participant_id", "session_id", "date", "room_id", "site_id",
    "action", "binary_label", "fall_type", "trial_id", "activity_order",
    "router_id", "receiver_id", "sender_id", "device_pair_id", "router_position",
    "receiver_position", "sender_position", "orientation", "distance_m",
    "scenario", "occupancy", "interference_condition", "csi_output_type",
    "complex_csi_enabled", "csi_component_order", "phase_sanitization",
    "absolute_phase_valid", "channel_filter_enabled", "csi_scale_shift",
    "calibration_role", "calibration_id",
    "send_mode", "configured_send_interval_ms", "observed_send_mode",
    "observed_send_interval_ms", "transmitter_experiment_id", "transmitter_experiment_hash",
    "observed_transmitter_experiment_hash", "configured_duration_ms",
    "device_seq_start", "device_seq_end", "frame_count", "sequence_gap_count",
    "device_time_span_s", "effective_sample_rate_hz", "host_time_span_s",
    "host_pipeline_latency_ms_mean", "host_pipeline_latency_ms_p95", "host_device_span_skew_ms",
    "rssi_mean", "rssi_std", "noise_floor_mean", "agc_gain_mean",
    "channel_primary", "channel_secondary", "mcs", "bandwidth",
    "csi_len", "target_mac", "observed_primary_mac", "target_mac_frame_fraction",
    "unexpected_mac_frame_count", "dominant_phy_signature", "dominant_phy_fraction",
    "phy_signature_count", "phy_switch_count", "quality_status", "quality_reasons",
    "baseline_required", "baseline_min_duration_s", "baseline_ready_at_start",
    "rx_state_nonzero", "first_word_invalid_count",
    "complex_frames", "complex_invalid_frames", "complex_subcarriers_min", "complex_subcarriers_max",
    "complex_low_amplitude_fraction", "phase_residual_rms_mean",
    "phase_adjacent_coherence_mean", "phase_temporal_step_rms_mean", "phase_quality_status",
    "device_csi_received_delta", "device_queue_drop_delta",
    "device_null_tx_count_delta", "device_null_tx_error_count_delta",
    "device_null_ack_or_response_count_delta", "device_wifi_connected_end",
    "receiver_valid_research_packets_delta", "receiver_research_bytes_delta",
    "receiver_research_crc_errors_delta", "receiver_research_other_errors_delta",
    "receiver_research_sequence_gaps_delta", "receiver_research_last_seq",
    "device_telemetry_samples", "device_queue_depth_max", "device_free_heap_min",
    "device_minimum_free_heap_min", "device_queue_high_water_since_boot",
    "host_name", "python_version",
    "collector_version", "collector_sha256", "config_sha256",
    "firmware_app_revision", "firmware_idf_revision", "firmware_compile_time",
    "chip_name", "chip_revision", "firmware_device_mac", "capture_protocol",
    "started_at_utc", "ended_at_utc", "status", "notes",
]
MANIFEST_COLUMNS += [
    'quality_policy_version', 'baseline_fingerprint', 'connected_ap_bssid',
    'connected_ap_ssid', 'raw_stream_path', 'source_filter_enabled', 'excluded_source_frames',
    'target_frame_count', 'target_sample_rate_hz', 'target_gap_p95_ms', 'target_gap_max_ms',
    'target_long_gap_count', 'target_clock_error_count', 'target_dominant_phy_fraction',
    'target_dominant_phy_gap_max_ms', 'target_dominant_phy_rate_hz', 'needs_retake',
    'target_time_coverage_s',
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def safe_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def compact_json(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return safe_text(value)


def normalize_mac(value: Any) -> str:
    text = safe_text(value).lower().replace("-", ":")
    if not text:
        return ""
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", text):
        raise ValueError(f"Invalid MAC address: {value!r}")
    return text


def device_pair_id(config: dict[str, Any]) -> str:
    receiver = safe_text(config.get("receiver_id")) or "receiver-unknown"
    sender = safe_text(config.get("sender_id")) or safe_text(config.get("router_id")) or "sender-unknown"
    return f"{receiver}__{sender}"


def session_reference_status(
    data_root: Path, config: dict[str, Any], calibration_role: str,
    minimum_s: float = 60,
) -> dict[str, Any]:
    """Return the latest qualifying reference capture for this session and link."""
    minimum_s = float(minimum_s or 60)
    manifest = Path(data_root) / "session_manifest.csv"
    result = {"ready": False, "recording_id": "", "reason": "missing", "minimum_s": minimum_s}
    if not manifest.exists() or manifest.stat().st_size == 0:
        return result
    with manifest.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    session_id = safe_text(config.get("session_id"))
    candidates = [
        row for row in rows
        if safe_text(row.get("session_id")) == session_id
        and safe_text(row.get("calibration_role")) == calibration_role
        and safe_text(row.get("status")) == "complete"
    ]
    for row in reversed(candidates):
        if config.get('acquisition_gate_enabled') and row.get('baseline_fingerprint') != baseline_fingerprint(config):
            result['reason'] = 'acquisition_settings_changed_or_legacy_baseline'
            continue
        duration_s = safe_float(row.get("device_time_span_s")) or safe_float(row.get("host_time_span_s")) or 0.0
        quality = safe_text(row.get("quality_status"))
        if duration_s >= minimum_s * 0.9 and quality in {"pass", "warn"}:
            return {
                "ready": True,
                "recording_id": safe_text(row.get("recording_id")),
                "reason": quality,
                "minimum_s": minimum_s,
            }
        result = {
            "ready": False,
            "recording_id": safe_text(row.get("recording_id")),
            "reason": f"duration={duration_s:.2f}s, quality={quality or 'unknown'}",
            "minimum_s": minimum_s,
        }
    return result


def session_baseline_status(data_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Return the latest qualifying occupied, static-person baseline."""
    minimum_s = float(config.get("baseline_min_duration_s", 60) or 60)
    return session_reference_status(data_root, config, "static_reference", minimum_s)


def validate_action_label(value: Any) -> str:
    action = safe_text(value)
    if action in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}", action):
        raise ValueError(f"Unsafe action label: {action!r}")
    return action


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def research_experiment_hash(experiment_id: str) -> int:
    """32-bit FNV-1a used by the shared ESP32 research frame protocol."""
    value = 2166136261
    for byte in experiment_id.encode("ascii"):
        value ^= byte
        value = (value * 16777619) & 0xFFFFFFFF
    return value


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return float(ordered[index])


def mean(values: list[float]) -> float | None:
    return float(sum(values) / len(values)) if values else None


def std(values: list[float]) -> float | None:
    if not values:
        return None
    avg = mean(values)
    return float(math.sqrt(sum((x - avg) ** 2 for x in values) / len(values)))


def load_experiment_config(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    config_path = Path(path) if path else Path("config/experiment_config.json")
    if not config_path.exists():
        raise FileNotFoundError(
            f"Experiment config not found: {config_path}. Copy config/experiment_config.example.json "
            "and fill participant/session/room metadata before collection."
        )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    required = ["participant_id", "session_id", "room_id", "receiver_id"]
    missing = [key for key in required if not safe_text(config.get(key))]
    if missing:
        raise ValueError(f"Experiment config missing required fields: {', '.join(missing)}")
    compact_id = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    for key in ("participant_id", "session_id", "room_id", "site_id", "receiver_id", "sender_id", "router_id"):
        value = safe_text(config.get(key))
        if value and not compact_id.fullmatch(value):
            raise ValueError(
                f"{key} must contain only letters, digits, '.', '_' or '-' (maximum 64 characters)"
            )
    try:
        datetime.strptime(safe_text(config.get("date", datetime.now().date().isoformat())), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD") from exc
    interval = safe_int(config.get("send_interval_ms"))
    if interval is not None and interval <= 0:
        raise ValueError("send_interval_ms must be greater than zero")
    distance_m = config.get("distance_m")
    if distance_m is not None and safe_text(distance_m):
        try:
            distance_m = float(distance_m)
        except (TypeError, ValueError) as exc:
            raise ValueError("distance_m must be a number in meters") from exc
        if distance_m <= 0:
            raise ValueError("distance_m must be greater than zero")
        config["distance_m"] = round(distance_m, 2)
    fall_type = safe_text(config.get("fall_type"))
    if fall_type and fall_type not in {"demonstrated_fall", "incident_fall"}:
        raise ValueError("fall_type must be demonstrated_fall or incident_fall")
    config.setdefault("schema_version", SCHEMA_VERSION)
    config.setdefault("date", datetime.now().date().isoformat())
    config.setdefault("fall_type", "demonstrated_fall" if config.get("default_action") == FALL_ACTION else "")
    config.setdefault("collector_version", "console-csi-v7-research")
    config.setdefault("complex_csi_enabled", True)
    config.setdefault("csi_component_order", "imag_real_interleaved")
    config.setdefault("channel_filter_enabled", False)
    config.setdefault("csi_scale_shift", 0)
    config.setdefault("calibration_role", "activity")
    config.setdefault("calibration_id", "")
    config.setdefault("site_id", safe_text(config.get("room_id")))
    config.setdefault("target_mac", "")
    config["target_mac"] = normalize_mac(config.get("target_mac"))
    config.setdefault("require_static_baseline", True)
    config.setdefault("baseline_min_duration_s", 60)
    config.setdefault("minimum_target_mac_fraction", 0.95)
    config.setdefault("minimum_dominant_phy_fraction", 0.95)
    config.setdefault("minimum_telemetry_samples", 1)
    config["minimum_telemetry_samples"] = max(1, safe_int(config.get("minimum_telemetry_samples")) or 1)
    config.setdefault("device_pair_id", device_pair_id(config))
    if config["csi_component_order"] != "imag_real_interleaved":
        raise ValueError("csi_component_order must be imag_real_interleaved for ESP-IDF CSI")
    if config["calibration_role"] not in {"activity", "empty_room_reference", "static_reference"}:
        raise ValueError("calibration_role must be activity, empty_room_reference, or static_reference")
    config.setdefault("notes", "")
    config = acquisition_defaults(config)
    for key in ('minimum_target_rate_hz', 'maximum_target_gap_ms', 'mode_settle_seconds'):
        value = safe_float(config[key])
        if value is None or value <= 0:
            raise ValueError(f'{key} must be a finite positive number')
        config[key] = value
    config.setdefault("transmitter_experiment_id", safe_text(config["session_id"]))
    transmitter_experiment_id = safe_text(config["transmitter_experiment_id"])
    if not compact_id.fullmatch(transmitter_experiment_id) or len(transmitter_experiment_id) > 31:
        raise ValueError("transmitter_experiment_id must be a compact ASCII ID with at most 31 characters")
    config["config_source"] = str(config_path.resolve())
    config["config_sha256"] = sha256_file(config_path)
    return config


@dataclass
class DeviceTelemetry:
    latest: dict[str, Any] = field(default_factory=dict)

    def update(self, row: dict[str, Any]) -> None:
        text_fields = {"type", "send_mode"}
        self.latest = {key: value if key in text_fields else safe_int(value) for key, value in row.items()}

    def snapshot(self) -> dict[str, Any]:
        return dict(self.latest)


@dataclass
class RecordingAccumulator:
    recording_id: str
    csv_path: Path
    action: str
    target_seq: str
    trial_id: str
    started_at: datetime
    configured_duration_ms: int | None
    telemetry_start: dict[str, Any]
    rows: int = 0
    seq_start: int | None = None
    seq_end: int | None = None
    seq_previous: int | None = None
    sequence_gaps: int = 0
    device_time_start_us: int | None = None
    device_time_end_us: int | None = None
    host_monotonic_start_ns: int | None = None
    host_monotonic_end_ns: int | None = None
    host_pipeline_latencies_ms: list[float] = field(default_factory=list)
    rssi: list[float] = field(default_factory=list)
    noise_floor: list[float] = field(default_factory=list)
    agc_gain: list[float] = field(default_factory=list)
    rx_state_nonzero: int = 0
    first_word_invalid_count: int = 0
    telemetry_samples: int = 0
    queue_depth: list[float] = field(default_factory=list)
    free_heap: list[float] = field(default_factory=list)
    minimum_free_heap: list[float] = field(default_factory=list)
    queue_high_water_since_boot: list[float] = field(default_factory=list)
    last_radio: dict[str, Any] = field(default_factory=dict)
    complex_quality: ComplexCSIQuality = field(default_factory=ComplexCSIQuality)
    mac_counts: Counter[str] = field(default_factory=Counter)
    phy_signature_counts: Counter[tuple[str, ...]] = field(default_factory=Counter)
    previous_phy_signature: tuple[str, ...] | None = None
    phy_switch_count: int = 0
    baseline_ready_at_start: bool = False
    target_timing: LinkTiming = field(default_factory=LinkTiming)
    excluded_source_frames: int = 0
    observed_start_ns: int | None = None
    observed_end_ns: int | None = None
    target_start_ns: int | None = None
    target_end_ns: int | None = None
    acquisition_errors: set[str] = field(default_factory=set)

    def add(self, row: dict[str, Any], host_received_ns: int, host_written_ns: int) -> None:
        seq = safe_int(row.get("seq"))
        device_us = safe_int(row.get("local_timestamp"))
        if self.rows == 0:
            self.seq_start = seq
            self.device_time_start_us = device_us
            self.host_monotonic_start_ns = host_received_ns
        if seq is not None and self.seq_previous is not None and seq > self.seq_previous + 1:
            self.sequence_gaps += seq - self.seq_previous - 1
        self.seq_previous = seq
        self.seq_end = seq
        self.device_time_end_us = device_us
        self.host_monotonic_end_ns = host_received_ns
        self.host_pipeline_latencies_ms.append(max(0.0, (host_written_ns - host_received_ns) / 1_000_000.0))
        for key, target in [("rssi", self.rssi), ("noise_floor", self.noise_floor), ("agc_gain", self.agc_gain)]:
            value = safe_float(row.get(key))
            if value is not None:
                target.append(value)
        self.rx_state_nonzero += int((safe_int(row.get("rx_state")) or 0) != 0)
        self.first_word_invalid_count += int((safe_int(row.get("first_word_invalid")) or 0) != 0)
        self.last_radio = {key: row.get(key, "") for key in (
            "channel_primary", "channel_secondary", "mcs", "cwb", "len", "rate", "sig_mode", "mac"
        )}
        mac = safe_text(row.get("mac")).lower()
        if mac:
            self.mac_counts[mac] += 1
        phy_signature = tuple(safe_text(row.get(key)) for key in (
            "rate", "sig_mode", "mcs", "cwb", "channel_primary", "channel_secondary", "len"
        ))
        self.phy_signature_counts[phy_signature] += 1
        if self.previous_phy_signature is not None and phy_signature != self.previous_phy_signature:
            self.phy_switch_count += 1
        self.previous_phy_signature = phy_signature
        if row.get("data") is not None:
            self.complex_quality.add(row.get("data"))
        self.rows += 1

    def add_telemetry(self, row: dict[str, Any]) -> None:
        for key, target in (
            ("queue_depth", self.queue_depth),
            ("free_heap", self.free_heap),
            ("minimum_free_heap", self.minimum_free_heap),
            ("queue_high_water", self.queue_high_water_since_boot),
        ):
            value = safe_float(row.get(key))
            if value is not None:
                target.append(value)
        self.telemetry_samples += 1


class ExperimentLedger:
    def __init__(self, data_root: Path, config: dict[str, Any]):
        self.data_root = Path(data_root)
        self.config = dict(config)
        self.manifest_path = self.data_root / "session_manifest.csv"
        self.telemetry = DeviceTelemetry()
        self.device_info: dict[str, Any] = {}
        self.active: RecordingAccumulator | None = None
        self._trial_counter = 0
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._resume_existing_session()

    def baseline_status(self) -> dict[str, Any]:
        return session_baseline_status(self.data_root, self.config)

    def _resume_existing_session(self) -> None:
        if not self.manifest_path.exists() or self.manifest_path.stat().st_size == 0:
            return
        with self.manifest_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            return
        # A room/day export directory may contain multiple sessions. Resume
        # checks and trial numbering apply only to the configured session;
        # the shared manifest can still index every CSV collected that day.
        session_id = safe_text(self.config.get("session_id"))
        session_rows = [row for row in rows if safe_text(row.get("session_id")) == session_id]
        if not session_rows:
            return
        rows = session_rows
        for key in ("participant_id", "session_id", "date", "room_id", "receiver_id"):
            existing = {safe_text(row.get(key)) for row in rows if safe_text(row.get(key))}
            configured = safe_text(self.config.get(key))
            if existing and existing != {configured}:
                raise ValueError(
                    f"Existing session metadata mismatch for {key}: manifest={sorted(existing)}, config={configured!r}"
                )
        condition_fields = {
            "router_id": safe_text(self.config.get("router_id")),
            "sender_id": safe_text(self.config.get("sender_id")),
            "router_position": compact_json(self.config.get("router_position", {})),
            "receiver_position": compact_json(self.config.get("receiver_position", {})),
            "sender_position": compact_json(self.config.get("sender_position", {})),
            "orientation": safe_text(self.config.get("orientation")),
            "distance_m": safe_text(self.config.get("distance_m")),
            "scenario": safe_text(self.config.get("scenario")),
            "occupancy": safe_text(self.config.get("occupancy")),
            "interference_condition": safe_text(self.config.get("interference_condition")),
            "csi_output_type": safe_text(self.config.get("csi_output_type", "LLTF")),
            "complex_csi_enabled": safe_text(self.config.get("complex_csi_enabled", True)),
            "csi_component_order": safe_text(self.config.get("csi_component_order", "imag_real_interleaved")),
            "channel_filter_enabled": safe_text(self.config.get("channel_filter_enabled", False)),
            "csi_scale_shift": safe_text(self.config.get("csi_scale_shift", 0)),
            "send_mode": safe_text(self.config.get("send_mode", "router_ping")),
            "configured_send_interval_ms": safe_text(self.config.get("send_interval_ms")),
            "transmitter_experiment_id": safe_text(self.config.get("transmitter_experiment_id", self.config["session_id"])),
            "site_id": safe_text(self.config.get("site_id", self.config.get("room_id"))),
            "device_pair_id": safe_text(self.config.get("device_pair_id", device_pair_id(self.config))),
            "target_mac": normalize_mac(self.config.get("target_mac")),
        }
        for field_name, configured in condition_fields.items():
            existing = {safe_text(row.get(field_name)) for row in rows if safe_text(row.get(field_name))}
            if existing and existing != {configured}:
                raise ValueError(
                    f"Existing session condition mismatch for {field_name}: "
                    f"manifest={sorted(existing)}, config={configured!r}; start a new session_id"
                )
        orders = [safe_int(row.get("activity_order")) for row in rows]
        self._trial_counter = max((value for value in orders if value is not None), default=len(rows))

    def note_telemetry(self, row: dict[str, Any]) -> None:
        self.telemetry.update(row)
        if self.active is not None:
            self.active.add_telemetry(row)
            if self.config.get('acquisition_gate_enabled'):
                dedicated = self.config.get('send_mode') == 'espnow_research'
                if not dedicated and safe_int(row.get('wifi_connected')) != 1:
                    self.active.acquisition_errors.add('wifi_disconnected_during_recording')
                if dedicated:
                    expected_hash = research_experiment_hash(str(self.config.get(
                        'transmitter_experiment_id', self.config.get('session_id', ''))))
                    if safe_int(row.get('research_rx_experiment_hash')) != expected_hash:
                        self.active.acquisition_errors.add('transmitter_experiment_mismatch')
                    for key in ('research_rx_crc_errors', 'research_rx_seq_gaps', 'research_rx_other_errors'):
                        if (safe_int(row.get(key)) or 0) > (safe_int(self.active.telemetry_start.get(key)) or 0):
                            self.active.acquisition_errors.add(key)
                if row.get('send_mode') != self.config.get('send_mode', 'router_ping'):
                    self.active.acquisition_errors.add('send_mode_changed_during_recording')
                start = safe_int(self.active.telemetry_start.get('csi_received'))
                current = safe_int(row.get('csi_received'))
                if start is not None and current is not None and current < start:
                    self.active.acquisition_errors.add('device_counter_reset')

    def note_device_info(self, row: dict[str, Any]) -> None:
        self.device_info = dict(row)

    def update_capture_role(self, *, complex_csi_enabled: bool, calibration_role: str) -> None:
        if self.active is not None:
            raise RuntimeError("Cannot update the capture role while a recording is active")
        if calibration_role not in {"activity", "empty_room_reference", "static_reference"}:
            raise ValueError(f"Unsupported calibration role: {calibration_role}")
        self.config.update({
            "complex_csi_enabled": bool(complex_csi_enabled),
            "csi_component_order": "imag_real_interleaved",
            "channel_filter_enabled": False,
            "csi_scale_shift": 0,
            "calibration_role": calibration_role,
        })

    def begin(self, csv_path: Path, action: str, target_seq: str, duration_ms: int | None = None) -> RecordingAccumulator:
        if self.active is not None:
            raise RuntimeError("A recording is already active")
        action = validate_action_label(action)
        role = safe_text(self.config.get("calibration_role", "activity"))
        baseline = self.baseline_status()
        if (bool(self.config.get("require_static_baseline", False)) and role == "activity"
                and not baseline["ready"]):
            raise RuntimeError(
                f"Static baseline is required before activity collection: {baseline['reason']}"
            )
        self._trial_counter += 1
        started = utc_now()
        recording_id = f"rec-{started.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}"
        configured_trial = safe_text(self.config.get("trial_id_prefix"))
        trial_id = f"{configured_trial}{self._trial_counter:04d}" if configured_trial else f"trial-{self._trial_counter:04d}"
        self.active = RecordingAccumulator(
            recording_id=recording_id,
            csv_path=Path(csv_path),
            action=action,
            target_seq=target_seq,
            trial_id=trial_id,
            started_at=started,
            configured_duration_ms=duration_ms,
            telemetry_start=self.telemetry.snapshot(),
            baseline_ready_at_start=bool(baseline["ready"]),
        )
        return self.active

    def add_csi(self, row: dict[str, Any], host_received_ns: int, host_written_ns: int) -> bool:
        if self.active is None:
            raise RuntimeError("No active recording")
        recording = self.active
        if recording.observed_start_ns is None:
            recording.observed_start_ns = host_received_ns
        recording.observed_end_ns = host_received_ns
        target = normalize_mac(self.config.get('target_mac'))
        is_target = not target or safe_text(row.get('mac')).lower() == target
        if is_target:
            recording.target_timing.add(row)
            if recording.target_start_ns is None:
                recording.target_start_ns = host_received_ns
            recording.target_end_ns = host_received_ns
        elif self.config.get('target_only_recordings', False):
            recording.excluded_source_frames += 1
            return False
        recording.add(row, host_received_ns, host_written_ns)
        return True

    def finish(self, status: str = "complete") -> dict[str, Any] | None:
        if self.active is None:
            return None
        recording = self.active
        self.active = None
        ended = utc_now()
        telemetry_end = self.telemetry.snapshot()
        device_span_s = None
        if recording.device_time_start_us is not None and recording.device_time_end_us is not None:
            device_span_s = max(0.0, (recording.device_time_end_us - recording.device_time_start_us) / 1_000_000.0)
        host_span_s = None
        if recording.host_monotonic_start_ns is not None and recording.host_monotonic_end_ns is not None:
            host_span_s = max(0.0, (recording.host_monotonic_end_ns - recording.host_monotonic_start_ns) / 1_000_000_000.0)
        action = recording.action
        binary_label = "fall" if action in FALL_ACTIONS else ("non_fall" if action in NON_FALL_ACTIONS else "other")
        fall_type = safe_text(self.config.get("fall_type")) if action in FALL_ACTIONS else "not_applicable"
        if action in FALL_ACTIONS and not fall_type:
            fall_type = "demonstrated_fall"
        start_rx = safe_int(recording.telemetry_start.get("csi_received"))
        end_rx = safe_int(telemetry_end.get("csi_received"))
        start_drop = safe_int(recording.telemetry_start.get("queue_drops"))
        end_drop = safe_int(telemetry_end.get("queue_drops"))
        transmitter_experiment_id = safe_text(
            self.config.get("transmitter_experiment_id", self.config.get("session_id")))
        target_mac = normalize_mac(self.config.get("target_mac"))
        primary_mac, primary_mac_count = recording.mac_counts.most_common(1)[0] if recording.mac_counts else ("", 0)
        target_count = recording.mac_counts.get(target_mac, 0) if target_mac else primary_mac_count
        target_fraction = target_count / recording.rows if recording.rows else 0.0
        dominant_phy, dominant_phy_count = (
            recording.phy_signature_counts.most_common(1)[0]
            if recording.phy_signature_counts else (("",) * 7, 0)
        )
        dominant_phy_fraction = dominant_phy_count / recording.rows if recording.rows else 0.0
        quality_reasons: list[str] = []
        quality_warnings: list[str] = []
        minimum_mac_fraction = float(self.config.get("minimum_target_mac_fraction", 0.95) or 0.95)
        minimum_phy_fraction = float(self.config.get("minimum_dominant_phy_fraction", 0.95) or 0.95)
        minimum_telemetry_samples = max(1, safe_int(self.config.get("minimum_telemetry_samples")) or 1)
        if not target_mac:
            quality_warnings.append("target_mac_not_configured_legacy")
        elif target_fraction < minimum_mac_fraction:
            quality_reasons.append(f"target_mac_fraction={target_fraction:.4f}<{minimum_mac_fraction:.4f}")
        if dominant_phy_fraction < minimum_phy_fraction:
            (quality_warnings if self.config.get('acquisition_gate_enabled') else quality_reasons).append(
                f"dominant_phy_fraction={dominant_phy_fraction:.4f}<{minimum_phy_fraction:.4f}")
        if target_mac and recording.telemetry_samples < minimum_telemetry_samples:
            quality_reasons.append(
                f"device_telemetry_samples={recording.telemetry_samples}<{minimum_telemetry_samples}"
            )
        if recording.rx_state_nonzero:
            quality_reasons.append(f"rx_state_nonzero={recording.rx_state_nonzero}")
        if recording.first_word_invalid_count:
            quality_reasons.append(f"first_word_invalid={recording.first_word_invalid_count}")
        duration_s = max((recording.configured_duration_ms or 0)/1000, device_span_s or 0, host_span_s or 0)
        gap_limit = float(self.config.get('maximum_target_gap_ms', 100))
        timing = recording.target_timing.summary(duration_s, gap_limit)
        if self.config.get('acquisition_gate_enabled'):
            quality_reasons.extend(sorted(recording.acquisition_errors))
            if timing['target_sample_rate_hz'] < float(self.config.get('minimum_target_rate_hz', 50)):
                quality_reasons.append(f"target_rate_too_low={timing['target_sample_rate_hz']:.2f}Hz")
            if timing['target_gap_max_ms'] > gap_limit:
                quality_reasons.append(f"target_gap_too_long={timing['target_gap_max_ms']:.2f}ms")
            if duration_s - timing['target_time_coverage_s'] > 2 * gap_limit / 1000:
                quality_reasons.append('target_does_not_cover_recording_duration')
            if timing['target_clock_error_count']:
                quality_reasons.append('target_timestamp_missing_or_nonmonotonic')
            if recording.target_start_ns is not None and recording.observed_start_ns is not None:
                edge_ms = max(recording.target_start_ns-recording.observed_start_ns,
                              (recording.observed_end_ns or 0)-(recording.target_end_ns or 0))/1e6
                if edge_ms > gap_limit:
                    quality_reasons.append(f'target_missing_at_recording_edge={edge_ms:.2f}ms')
            if start_drop is not None and end_drop is not None and end_drop > start_drop:
                quality_reasons.append(f'device_queue_drops={end_drop-start_drop}')
            if self.config.get('target_mac') != self.config.get('connected_ap_bssid'):
                quality_reasons.append('target_ap_identity_unverified')
            if timing['target_dominant_phy_gap_max_ms'] > gap_limit:
                quality_warnings.append('dominant_phy_substream_has_long_gaps')
        quality_status = "fail" if quality_reasons else ("warn" if quality_warnings else "pass")
        quality_messages = quality_reasons + quality_warnings
        recording_schema = SCHEMA_VERSION if target_mac else "csi-experiment-manifest/1.0"

        def telemetry_delta(key: str) -> int | None:
            start = safe_int(recording.telemetry_start.get(key))
            end = safe_int(telemetry_end.get(key))
            return max(0, end - start) if start is not None and end is not None else None
        row = {
            **timing,
            'quality_policy_version': self.config.get('quality_policy_version', ''),
            'baseline_fingerprint': baseline_fingerprint(self.config),
            'connected_ap_bssid': self.config.get('connected_ap_bssid', ''),
            'connected_ap_ssid': self.config.get('connected_ap_ssid', ''),
            'raw_stream_path': self.config.get('raw_stream_path', ''),
            'source_filter_enabled': bool(self.config.get('target_only_recordings', False)),
            'excluded_source_frames': recording.excluded_source_frames,
            'needs_retake': quality_status == 'fail',
            "schema_version": recording_schema,
            "recording_id": recording.recording_id,
            "relative_csv_path": recording.csv_path.relative_to(self.data_root).as_posix(),
            "sidecar_path": recording.csv_path.with_suffix(".meta.json").relative_to(self.data_root).as_posix(),
            "sha256": sha256_file(recording.csv_path) if recording.csv_path.exists() else "",
            "participant_id": self.config.get("participant_id"),
            "session_id": self.config.get("session_id"),
            "date": self.config.get("date"),
            "room_id": self.config.get("room_id"),
            "site_id": self.config.get("site_id", self.config.get("room_id", "")),
            "action": action,
            "binary_label": binary_label,
            "fall_type": fall_type,
            "trial_id": recording.trial_id,
            "activity_order": self._trial_counter,
            "router_id": self.config.get("router_id", ""),
            "receiver_id": self.config.get("receiver_id", ""),
            "sender_id": self.config.get("sender_id", ""),
            "device_pair_id": self.config.get("device_pair_id", device_pair_id(self.config)),
            "router_position": compact_json(self.config.get("router_position", {})),
            "receiver_position": compact_json(self.config.get("receiver_position", {})),
            "sender_position": compact_json(self.config.get("sender_position", {})),
            "orientation": self.config.get("orientation", ""),
            "distance_m": self.config.get("distance_m", ""),
            "scenario": self.config.get("scenario", ""),
            "occupancy": self.config.get("occupancy", ""),
            "interference_condition": self.config.get("interference_condition", ""),
            "csi_output_type": self.config.get("csi_output_type", "LLTF"),
            **complex_layout_metadata(self.config),
            "calibration_role": self.config.get("calibration_role", "activity"),
            "calibration_id": self.config.get("calibration_id", ""),
            "send_mode": self.config.get("send_mode", "router_ping"),
            "configured_send_interval_ms": self.config.get("send_interval_ms", ""),
            "observed_send_mode": telemetry_end.get("send_mode", ""),
            "observed_send_interval_ms": telemetry_end.get("send_interval_ms", ""),
            "transmitter_experiment_id": transmitter_experiment_id,
            "transmitter_experiment_hash": research_experiment_hash(transmitter_experiment_id),
            "observed_transmitter_experiment_hash": telemetry_end.get("research_rx_experiment_hash", ""),
            "configured_duration_ms": recording.configured_duration_ms or "",
            "device_seq_start": recording.seq_start,
            "device_seq_end": recording.seq_end,
            "frame_count": recording.rows,
            "sequence_gap_count": recording.sequence_gaps,
            "device_time_span_s": device_span_s,
            "effective_sample_rate_hz": ((recording.rows - 1) / device_span_s) if device_span_s and recording.rows > 1 else None,
            "host_time_span_s": host_span_s,
            "host_pipeline_latency_ms_mean": mean(recording.host_pipeline_latencies_ms),
            "host_pipeline_latency_ms_p95": percentile(recording.host_pipeline_latencies_ms, 0.95),
            "host_device_span_skew_ms": ((host_span_s - device_span_s) * 1000.0)
            if host_span_s is not None and device_span_s is not None else None,
            "rssi_mean": mean(recording.rssi),
            "rssi_std": std(recording.rssi),
            "noise_floor_mean": mean(recording.noise_floor),
            "agc_gain_mean": mean(recording.agc_gain),
            "channel_primary": recording.last_radio.get("channel_primary", ""),
            "channel_secondary": recording.last_radio.get("channel_secondary", ""),
            "mcs": recording.last_radio.get("mcs", ""),
            "bandwidth": recording.last_radio.get("cwb", ""),
            "csi_len": recording.last_radio.get("len", ""),
            "target_mac": target_mac,
            "observed_primary_mac": primary_mac,
            "target_mac_frame_fraction": target_fraction,
            "unexpected_mac_frame_count": max(0, recording.rows - target_count),
            "dominant_phy_signature": compact_json(dict(zip(
                ("rate", "sig_mode", "mcs", "bandwidth", "channel_primary", "channel_secondary", "csi_len"),
                dominant_phy,
            ))),
            "dominant_phy_fraction": dominant_phy_fraction,
            "phy_signature_count": len(recording.phy_signature_counts),
            "phy_switch_count": recording.phy_switch_count,
            "quality_status": quality_status,
            "quality_reasons": compact_json(quality_messages),
            "baseline_required": bool(self.config.get("require_static_baseline", False)),
            "baseline_min_duration_s": self.config.get("baseline_min_duration_s", 60),
            "baseline_ready_at_start": recording.baseline_ready_at_start,
            "rx_state_nonzero": recording.rx_state_nonzero,
            "first_word_invalid_count": recording.first_word_invalid_count,
            **recording.complex_quality.summary(),
            "device_csi_received_delta": max(0, end_rx - start_rx)
            if start_rx is not None and end_rx is not None else None,
            "device_queue_drop_delta": max(0, end_drop - start_drop)
            if start_drop is not None and end_drop is not None else None,
            "device_null_tx_count_delta": telemetry_delta("null_tx_count"),
            "device_null_tx_error_count_delta": telemetry_delta("null_tx_error_count"),
            "device_null_ack_or_response_count_delta": telemetry_delta("null_ack_or_response_count"),
            "device_wifi_connected_end": telemetry_end.get("wifi_connected", ""),
            "receiver_valid_research_packets_delta": telemetry_delta("research_rx_valid"),
            "receiver_research_bytes_delta": telemetry_delta("research_rx_bytes"),
            "receiver_research_crc_errors_delta": telemetry_delta("research_rx_crc_errors"),
            "receiver_research_other_errors_delta": telemetry_delta("research_rx_other_errors"),
            "receiver_research_sequence_gaps_delta": telemetry_delta("research_rx_seq_gaps"),
            "receiver_research_last_seq": telemetry_end.get("research_rx_last_seq", ""),
            "device_telemetry_samples": recording.telemetry_samples,
            "device_queue_depth_max": max(recording.queue_depth) if recording.queue_depth else None,
            "device_free_heap_min": min(recording.free_heap) if recording.free_heap else None,
            "device_minimum_free_heap_min": min(recording.minimum_free_heap)
            if recording.minimum_free_heap else None,
            "device_queue_high_water_since_boot": max(recording.queue_high_water_since_boot)
            if recording.queue_high_water_since_boot else None,
            "host_name": socket.gethostname(),
            "python_version": platform.python_version(),
            "collector_version": self.config.get("collector_version", "console-csi-v7-research"),
            "collector_sha256": self.config.get("collector_sha256", ""),
            "config_sha256": self.config.get("config_sha256", ""),
            "firmware_app_revision": self.device_info.get("app_revision", ""),
            "firmware_idf_revision": self.device_info.get("idf_revision", ""),
            "firmware_compile_time": self.device_info.get("compile_time", ""),
            "chip_name": self.device_info.get("chip_name", ""),
            "chip_revision": self.device_info.get("chip_revision", ""),
            "firmware_device_mac": self.device_info.get("receiver_mac", ""),
            "capture_protocol": self.device_info.get("capture_protocol", ""),
            "started_at_utc": iso_utc(recording.started_at),
            "ended_at_utc": iso_utc(ended),
            "status": status,
            "notes": self.config.get("notes", ""),
        }
        sidecar = {
            "schema_version": SCHEMA_VERSION,
            "recording": row,
            "experiment_config": self.config,
            "telemetry_start": recording.telemetry_start,
            "telemetry_end": telemetry_end,
            "software": {"python_executable": sys.executable, "python_version": sys.version},
        }
        sidecar_path = recording.csv_path.with_suffix(".meta.json")
        sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
        exists = self.manifest_path.exists() and self.manifest_path.stat().st_size > 0
        if exists:
            with self.manifest_path.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                existing_columns = reader.fieldnames or []
                existing_rows = list(reader)
            if existing_columns != MANIFEST_COLUMNS:
                with self.manifest_path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(existing_rows)
        with self.manifest_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow({key: "" if value is None else value for key, value in row.items()})
        return row

    def abort(self) -> dict[str, Any] | None:
        return self.finish(status="aborted")
