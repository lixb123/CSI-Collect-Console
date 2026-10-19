"""Validate a research CSI session before training or archival."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path


# Required labels for a new fall-detection session. Legacy labels remain
# accepted by the ledger, but are not enough to certify a new protocol run.
CORE_ACTIONS = {
    "none", "static", "walk_slow", "walk_fast", "sit down", "stand up",
    "bend", "pick_up", "lie_down", "turn",
    "fall_forward", "fall_backward", "fall_left", "fall_right",
}
FALL_ACTIONS = {"wave", "fall_forward", "fall_backward", "fall_left", "fall_right"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def as_bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def validate(session_root: Path, require_core_actions: bool = False,
             session_id: str | None = None) -> dict:
    manifest = session_root / "session_manifest.csv"
    errors: list[str] = []
    warnings: list[str] = []
    if not manifest.exists():
        return {"status": "FAIL", "errors": [f"Missing {manifest}"], "warnings": [], "recordings": 0}
    with manifest.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if session_id is not None:
        rows = [row for row in rows if row.get("session_id", "") == session_id]
        if not rows:
            errors.append(f"Manifest has no recordings for session_id={session_id!r}")
    session_ids = {row.get("session_id", "") for row in rows if row.get("session_id", "")}
    if session_id is None and len(session_ids) > 1:
        errors.append(
            "This room/day manifest contains multiple sessions; validate one session_id at a time "
            "or use the manifest as a day-level export index."
        )
    if not rows:
        errors.append("Manifest has no recordings")
    for field in ("recording_id", "relative_csv_path", "sidecar_path", "sha256", "participant_id",
                  "session_id", "room_id", "receiver_id", "action", "status"):
        if any(not row.get(field, "").strip() for row in rows):
            errors.append(f"Missing required field in one or more rows: {field}")
    for field in ("recording_id", "relative_csv_path"):
        values = [row.get(field, "") for row in rows]
        duplicates = [value for value, count in Counter(values).items() if value and count > 1]
        if duplicates:
            errors.append(f"Duplicate {field}: {duplicates[:5]}")
    for field in ("participant_id", "session_id", "date", "room_id", "receiver_id"):
        values = {row.get(field, "") for row in rows if row.get(field, "")}
        if len(values) > 1:
            errors.append(f"One session manifest contains multiple {field} values: {sorted(values)}")
    actions = Counter(row.get("action", "") for row in rows)
    if require_core_actions:
        covered_actions = {
            row.get("action", "")
            for row in rows
            if (row.get("action", "") == "none"
                or row.get("calibration_role", "").strip() == "activity")
        }
        missing = sorted(CORE_ACTIONS - covered_actions)
        if missing:
            errors.append(f"Session is missing required actions: {missing}")
    for row in rows:
        schema_version = row.get("schema_version", "").strip()
        current_schema = schema_version == "csi-experiment-manifest/1.1"
        try:
            csv_path = (session_root / row["relative_csv_path"]).resolve()
            sidecar_path = (session_root / row.get("sidecar_path", "")).resolve()
            session_resolved = session_root.resolve()
            csv_path.relative_to(session_resolved)
            sidecar_path.relative_to(session_resolved)
        except (OSError, ValueError):
            errors.append(f"Recording path escapes session root: {row.get('relative_csv_path', '')!r}")
            continue
        if row.get("status", "").strip() != "complete":
            errors.append(f"Recording is not complete (status={row.get('status', '')!r}): {csv_path}")
        if not csv_path.exists():
            errors.append(f"Missing CSV: {csv_path}")
            continue
        sidecar = None
        if not sidecar_path.exists():
            errors.append(f"Missing sidecar: {sidecar_path}")
        else:
            try:
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                sidecar_row = sidecar.get("recording", {})
                if sidecar_row.get("recording_id") != row.get("recording_id"):
                    errors.append(f"Sidecar recording_id mismatch: {sidecar_path}")
                if sidecar_row.get("sha256") != row.get("sha256"):
                    errors.append(f"Sidecar SHA-256 mismatch: {sidecar_path}")
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                errors.append(f"Invalid sidecar ({exc}): {sidecar_path}")
        expected_hash = row.get("sha256", "")
        if expected_hash and sha256_file(csv_path) != expected_hash:
            errors.append(f"SHA-256 mismatch: {csv_path}")
        try:
            frame_count = int(row.get("frame_count", "0"))
            gaps = int(row.get("sequence_gap_count", "0"))
            if frame_count < 2:
                errors.append(f"Too few frames ({frame_count}): {csv_path}")
            if gaps:
                warnings.append(f"Sequence gaps={gaps}: {csv_path}")
        except ValueError:
            errors.append(f"Invalid frame/gap count: {csv_path}")
        try:
            telemetry_samples = int(row.get("device_telemetry_samples", "0") or "0")
            if telemetry_samples == 0:
                message = f"No in-recording device telemetry; short-trial device metrics are unavailable: {csv_path}"
                if current_schema:
                    errors.append(message)
                else:
                    warnings.append(message)
        except ValueError:
            errors.append(f"Invalid device telemetry sample count: {csv_path}")
        if current_schema and isinstance(sidecar, dict):
            telemetry_start = sidecar.get("telemetry_start")
            telemetry_end = sidecar.get("telemetry_end")
            if not isinstance(telemetry_start, dict) or not isinstance(telemetry_end, dict):
                errors.append(f"Missing telemetry_start/telemetry_end in sidecar: {sidecar_path}")
            elif not telemetry_start or not telemetry_end:
                errors.append(f"Empty telemetry_start/telemetry_end in sidecar: {sidecar_path}")
        configured_interval = row.get("configured_send_interval_ms", "").strip()
        observed_interval = row.get("observed_send_interval_ms", "").strip()
        if configured_interval and observed_interval:
            try:
                if float(configured_interval) != float(observed_interval):
                    errors.append(
                        f"Configured/observed send interval mismatch ({configured_interval} != {observed_interval}): "
                        f"{csv_path}"
                    )
            except ValueError:
                errors.append(f"Invalid configured/observed send interval: {csv_path}")
        configured_mode = row.get("send_mode", "").strip()
        observed_mode = row.get("observed_send_mode", "").strip()
        if configured_mode and observed_mode and configured_mode != observed_mode:
            errors.append(
                f"Configured/observed send mode mismatch ({configured_mode!r} != {observed_mode!r}): {csv_path}"
            )
        target_mac = row.get("target_mac", "").strip().lower()
        observed_mac = row.get("observed_primary_mac", "").strip().lower()
        if target_mac:
            if observed_mac and observed_mac != target_mac:
                errors.append(
                    f"Primary source MAC does not match target ({observed_mac} != {target_mac}): {csv_path}"
                )
            try:
                target_fraction = float(row.get("target_mac_frame_fraction", "0") or "0")
                if target_fraction < 0.95:
                    errors.append(f"Target MAC frame fraction below 0.95 ({target_fraction:.4f}): {csv_path}")
            except ValueError:
                errors.append(f"Invalid target MAC frame fraction: {csv_path}")
        elif current_schema:
            errors.append(f"Manifest 1.1 recording has no target MAC: {csv_path}")
        try:
            phy_fraction = float(row.get("dominant_phy_fraction", "0") or "0")
            if current_schema and phy_fraction < 0.95:
                errors.append(f"Dominant PHY fraction below 0.95 ({phy_fraction:.4f}): {csv_path}")
        except ValueError:
            errors.append(f"Invalid dominant PHY fraction: {csv_path}")
        quality_status = row.get("quality_status", "").strip()
        if quality_status == "fail":
            errors.append(f"Recording quality gate failed ({row.get('quality_reasons', '')}): {csv_path}")
        if (as_bool(row.get("baseline_required")) and row.get("calibration_role", "").strip() == "activity"
                and not as_bool(row.get("baseline_ready_at_start"))):
            errors.append(f"Activity recording started without a qualified static baseline: {csv_path}")
        expected_tx_hash = row.get("transmitter_experiment_hash", "").strip()
        observed_tx_hash = row.get("observed_transmitter_experiment_hash", "").strip()
        if configured_mode == "espnow_research":
            if not observed_tx_hash or observed_tx_hash == "0":
                errors.append(f"No validated research transmitter frame observed: {csv_path}")
            elif expected_tx_hash and observed_tx_hash != expected_tx_hash:
                errors.append(
                    f"Research transmitter experiment mismatch ({expected_tx_hash} != {observed_tx_hash}): {csv_path}"
                )
            try:
                received_packets = int(row.get("receiver_valid_research_packets_delta", "0") or "0")
                if received_packets <= 0:
                    errors.append(f"No valid research packets within recording: {csv_path}")
            except ValueError:
                errors.append(f"Invalid research packet count: {csv_path}")
        if as_bool(row.get("complex_csi_enabled")):
            if row.get("csi_component_order", "").strip() != "imag_real_interleaved":
                errors.append(f"Unexpected complex CSI component order: {csv_path}")
            if as_bool(row.get("channel_filter_enabled")):
                errors.append(f"Physics CSI was captured with channel filtering enabled: {csv_path}")
            try:
                if int(float(row.get("csi_scale_shift", "0") or "0")) != 0:
                    errors.append(f"Physics CSI was captured with manual scale shift: {csv_path}")
                minimum = int(float(row.get("complex_subcarriers_min", "0") or "0"))
                maximum = int(float(row.get("complex_subcarriers_max", "0") or "0"))
                if (minimum, maximum) != (52, 52):
                    errors.append(f"Physics CSI requires exactly 52 complex subcarriers ({minimum}, {maximum}): {csv_path}")
            except ValueError:
                errors.append(f"Invalid complex CSI numeric metadata: {csv_path}")
            if as_bool(row.get("absolute_phase_valid")):
                errors.append(f"Raw ESP32 absolute phase cannot be marked valid: {csv_path}")
            if row.get("phase_quality_status", "").strip() != "usable_sanitized":
                warnings.append(
                    f"Relative phase is not eligible for the phase branch "
                    f"(status={row.get('phase_quality_status', '')!r}): {csv_path}"
                )
        if row.get("action") in FALL_ACTIONS and row.get("fall_type") not in {"demonstrated_fall", "incident_fall"}:
            errors.append(f"Fall action requires an explicit fall_type: {csv_path}")
    result = {
        "status": "FAIL" if errors else ("WARN" if warnings else "PASS"),
        "session_root": str(session_root.resolve()),
        "recordings": len(rows),
        "actions": dict(sorted(actions.items())),
        "errors": errors,
        "warnings": warnings,
        "group_keys": {
            field: sorted({row.get(field, "") for row in rows if row.get(field, "")})
            for field in ("participant_id", "session_id", "room_id", "site_id", "receiver_id", "device_pair_id")
        },
    }
    report_name = "validation_report.json"
    if session_id is not None:
        safe_session_id = "".join(
            character if character.isalnum() or character in "-_." else "_"
            for character in session_id
        )
        report_name = f"validation_report_{safe_session_id}.json"
    (session_root / report_name).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_root", type=Path)
    parser.add_argument("--require-core-actions", action="store_true")
    parser.add_argument("--session-id")
    args = parser.parse_args()
    result = validate(args.session_root, args.require_core_actions, args.session_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
