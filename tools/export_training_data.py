"""Export quality-gated CSI rows for model training.

The source CSV files are never modified.  Only completed recordings with a
passing manifest quality gate are exported, and every retained row must match
the configured target MAC and the recording's dominant PHY signature.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


PHY_FIELDS = (
    "rate", "sig_mode", "mcs", "cwb", "channel_primary", "channel_secondary", "len"
)
MAC_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")


def normalize_mac(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", ":")
    return text if MAC_RE.fullmatch(text) else ""


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def load_signature(row: dict[str, str]) -> dict[str, str]:
    try:
        signature = json.loads(row.get("dominant_phy_signature", ""))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(signature, dict):
        return {}
    return {key: str(signature.get(key, "")).strip() for key in PHY_FIELDS}


def safe_source_path(root: Path, relative: str) -> Path | None:
    try:
        path = (root / relative).resolve()
        path.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return path


def export_training_data(
    session_root: Path,
    output_dir: Path,
    *,
    session_id: str | None = None,
    min_frames: int = 2,
    min_mac_fraction: float = 0.95,
    min_phy_fraction: float = 0.95,
    min_telemetry_samples: int = 1,
) -> dict[str, Any]:
    manifest_path = session_root / "session_manifest.csv"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing session manifest: {manifest_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    with manifest_path.open(encoding="utf-8", newline="") as handle:
        manifest_rows = list(csv.DictReader(handle))
    session_ids = {row.get("session_id", "").strip() for row in manifest_rows if row.get("session_id", "").strip()}
    if session_id:
        manifest_rows = [row for row in manifest_rows if row.get("session_id", "").strip() == session_id]
        if not manifest_rows:
            raise ValueError(f"No recordings found for session_id={session_id!r}")
    elif len(session_ids) > 1:
        raise ValueError(
            "Manifest contains multiple sessions; pass --session-id to prevent cross-session mixing"
        )
    for manifest_row in manifest_rows:
        source_rel = manifest_row.get("relative_csv_path", "")
        source = safe_source_path(session_root, source_rel)
        reason = ""
        if manifest_row.get("status", "").strip() != "complete":
            reason = "status_not_complete"
        elif manifest_row.get("quality_status", "").strip() != "pass":
            reason = "quality_status_not_pass"
        elif safe_int(manifest_row.get("device_telemetry_samples")) < min_telemetry_samples:
            reason = "insufficient_device_telemetry"
        elif safe_float(manifest_row.get("target_mac_frame_fraction")) < min_mac_fraction:
            reason = "target_mac_fraction_below_threshold"
        elif safe_float(manifest_row.get("dominant_phy_fraction")) < min_phy_fraction:
            reason = "dominant_phy_fraction_below_threshold"
        target_mac = normalize_mac(manifest_row.get("target_mac"))
        signature = load_signature(manifest_row)
        if not reason and not target_mac:
            reason = "missing_target_mac"
        if not reason and (not signature or any(not signature[field] for field in PHY_FIELDS)):
            reason = "missing_dominant_phy_signature"
        if not reason and source is None:
            reason = "source_path_invalid"
        if not reason and not source.exists():
            reason = "source_csv_missing"
        if reason:
            rejected.append({"relative_csv_path": source_rel, "reason": reason})
            continue

        assert source is not None
        output_name = Path(source_rel).with_suffix(".csv")
        output_path = output_dir / output_name
        output_path.parent.mkdir(parents=True, exist_ok=True)
        rows_written = 0
        with source.open(encoding="utf-8", newline="") as source_handle, output_path.open(
            "w", encoding="utf-8", newline=""
        ) as output_handle:
            reader = csv.DictReader(source_handle)
            if not reader.fieldnames:
                rejected.append({"relative_csv_path": source_rel, "reason": "missing_csv_header"})
                output_path.unlink(missing_ok=True)
                continue
            writer = csv.DictWriter(output_handle, fieldnames=reader.fieldnames)
            writer.writeheader()
            for data_row in reader:
                row_mac = normalize_mac(data_row.get("mac"))
                row_signature = {field: str(data_row.get(field, "")).strip() for field in PHY_FIELDS}
                if row_mac == target_mac and row_signature == signature:
                    writer.writerow(data_row)
                    rows_written += 1
        if rows_written < min_frames:
            output_path.unlink(missing_ok=True)
            rejected.append({
                "relative_csv_path": source_rel,
                "reason": f"filtered_frames_below_{min_frames}",
                "filtered_frames": rows_written,
            })
            continue
        selected.append({
            "source_relative_csv_path": source_rel,
            "output_relative_csv_path": output_path.relative_to(output_dir).as_posix(),
            "recording_id": manifest_row.get("recording_id", ""),
            "session_id": manifest_row.get("session_id", ""),
            "participant_id": manifest_row.get("participant_id", ""),
            "site_id": manifest_row.get("site_id", ""),
            "room_id": manifest_row.get("room_id", ""),
            "receiver_id": manifest_row.get("receiver_id", ""),
            "router_id": manifest_row.get("router_id", ""),
            "device_pair_id": manifest_row.get("device_pair_id", ""),
            "action": manifest_row.get("action", ""),
            "send_mode": manifest_row.get("send_mode", ""),
            "target_mac": target_mac,
            "dominant_phy_signature": signature,
            "source_frame_count": safe_int(manifest_row.get("frame_count")),
            "filtered_frame_count": rows_written,
            "device_telemetry_samples": safe_int(manifest_row.get("device_telemetry_samples")),
        })

    report = {
        "session_root": str(session_root.resolve()),
        "output_dir": str(output_dir.resolve()),
        "session_id": session_id or (next(iter(session_ids)) if len(session_ids) == 1 else ""),
        "selection_policy": {
            "min_frames": min_frames,
            "min_target_mac_fraction": min_mac_fraction,
            "min_dominant_phy_fraction": min_phy_fraction,
            "min_telemetry_samples": min_telemetry_samples,
            "phy_fields": list(PHY_FIELDS),
        },
        "selected": selected,
        "rejected": rejected,
        "selected_recordings": len(selected),
        "rejected_recordings": len(rejected),
    }
    (output_dir / "training_selection_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output_dir / "training_manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        columns = [
            "source_relative_csv_path", "output_relative_csv_path", "recording_id", "session_id",
            "participant_id", "site_id", "room_id", "receiver_id", "router_id", "device_pair_id",
            "action", "send_mode", "target_mac", "dominant_phy_signature", "source_frame_count",
            "filtered_frame_count", "device_telemetry_samples",
        ]
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in selected:
            writer.writerow({
                **item,
                "dominant_phy_signature": json.dumps(
                    item["dominant_phy_signature"], ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ),
            })
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_root", type=Path)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--min-frames", type=int, default=2)
    parser.add_argument("--min-mac-fraction", type=float, default=0.95)
    parser.add_argument("--min-phy-fraction", type=float, default=0.95)
    parser.add_argument("--min-telemetry-samples", type=int, default=1)
    args = parser.parse_args()
    output_dir = args.output_dir or (args.session_root / "training_filtered")
    report = export_training_data(
        args.session_root,
        output_dir,
        session_id=args.session_id,
        min_frames=max(1, args.min_frames),
        min_mac_fraction=args.min_mac_fraction,
        min_phy_fraction=args.min_phy_fraction,
        min_telemetry_samples=max(1, args.min_telemetry_samples),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["selected_recordings"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
