from __future__ import annotations

import csv
import json
import tempfile
import unittest
import hashlib
from pathlib import Path

from experiment_capture import (
    ExperimentLedger,
    MANIFEST_COLUMNS,
    load_experiment_config,
    research_experiment_hash,
    validate_action_label,
)
from validate_session import validate


class ExperimentLedgerTest(unittest.TestCase):
    def test_research_experiment_hash_matches_c_protocol_vector(self):
        self.assertEqual(research_experiment_hash("P001-R01-D01-S01"), 0xB56201EF)

    def test_manifest_sidecar_and_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            action_dir = root / "wave"
            action_dir.mkdir(parents=True)
            config = {
                "participant_id": "P001", "session_id": "S001", "room_id": "R01",
                "receiver_id": "RX01", "date": "2026-09-01", "fall_type": "demonstrated_fall",
            }
            ledger = ExperimentLedger(root, config)
            ledger.note_telemetry({
                "type": "SYSTEM_TELEMETRY", "csi_received": 100, "queue_drops": 2,
                "send_interval_ms": 10, "send_mode": "router_ping",
            })
            csv_path = action_dir / "sample.csv"
            csv_path.write_text("type,seq,local_timestamp,rssi,noise_floor,agc_gain,rx_state,first_word_invalid\n", encoding="utf-8")
            ledger.begin(csv_path, "wave", "1", duration_ms=2000)
            for i in range(3):
                ledger.add_csi({
                    "seq": i + 10, "local_timestamp": 1_000_000 + i * 10_000,
                    "rssi": -50 + i, "noise_floor": -95, "agc_gain": 8,
                    "rx_state": 0, "first_word_invalid": 0, "len": 104,
                    "channel_primary": 11, "channel_secondary": 0, "mcs": 0, "cwb": 0,
                }, 5_000_000_000 + i * 10_100_000, 5_000_500_000 + i * 10_100_000)
            ledger.note_telemetry({
                "type": "SYSTEM_TELEMETRY", "csi_received": 104, "queue_drops": 3,
                "free_heap": 200000, "minimum_free_heap": 180000, "queue_high_water": 4,
                "queue_depth": 2, "send_interval_ms": 10, "send_mode": "router_ping",
            })
            result = ledger.finish()
            self.assertEqual(result["binary_label"], "fall")
            self.assertEqual(result["frame_count"], 3)
            self.assertAlmostEqual(result["effective_sample_rate_hz"], 100.0)
            self.assertEqual(result["device_queue_drop_delta"], 1)
            self.assertEqual(result["observed_send_interval_ms"], 10)
            self.assertEqual(result["observed_send_mode"], "router_ping")
            self.assertEqual(result["device_telemetry_samples"], 1)
            self.assertTrue(csv_path.with_suffix(".meta.json").exists())
            with (root / "session_manifest.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["participant_id"], "P001")
            sidecar = json.loads(csv_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["recording"]["action"], "wave")

    def test_config_requires_domain_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text('{"participant_id":"P001"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_experiment_config(path)

    def test_config_rejects_path_like_session_id(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text(json.dumps({
                "participant_id": "P001", "session_id": "../escape",
                "room_id": "R01", "receiver_id": "RX01",
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_experiment_config(path)

    def test_action_label_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            validate_action_label("../wave")
        self.assertEqual(validate_action_label("sit down"), "sit down")

    def test_room_day_manifest_allows_multiple_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data" / "R01" / "2026-09-01"
            root.mkdir(parents=True)
            for session_id, participant_id in (("S001", "P001"), ("S002", "P002")):
                config = {
                    "participant_id": participant_id, "session_id": session_id,
                    "room_id": "R01", "receiver_id": "RX01", "date": "2026-09-01",
                }
                ledger = ExperimentLedger(root, config)
                csv_path = root / f"12-00-00-000_R01_{participant_id}_none_1.csv"
                csv_path.write_text("type,seq,local_timestamp\n", encoding="utf-8")
                ledger.begin(csv_path, "none", "1", duration_ms=3000)
                ledger.finish()
            with (root / "session_manifest.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({row["session_id"] for row in rows}, {"S001", "S002"})
            self.assertEqual({row["participant_id"] for row in rows}, {"P001", "P002"})

    def test_directional_fall_is_binary_fall(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            root.mkdir(parents=True)
            config = {
                "participant_id": "P001", "session_id": "S001", "room_id": "R01",
                "receiver_id": "RX01", "date": "2026-09-01", "fall_type": "demonstrated_fall",
            }
            ledger = ExperimentLedger(root, config)
            csv_path = root / "12-00-00-000_R01_P001_fall_forward_1.csv"
            csv_path.write_text("type,seq,local_timestamp\n", encoding="utf-8")
            ledger.begin(csv_path, "fall_forward", "1", duration_ms=3000)
            result = ledger.finish()
            self.assertEqual(result["binary_label"], "fall")


class SessionValidatorTest(unittest.TestCase):
    def make_session(self, root: Path, action: str = "wave") -> Path:
        session = root / "P001-R01-D01-S01"
        action_dir = session / action
        action_dir.mkdir(parents=True)
        csv_path = action_dir / "sample.csv"
        csv_path.write_text("type,seq\nCSI_DATA,1\nCSI_DATA,2\n", encoding="utf-8")
        digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        sidecar_path = csv_path.with_suffix(".meta.json")
        sidecar_path.write_text(json.dumps({"recording": {"recording_id": "rec-001", "sha256": digest}}),
                                encoding="utf-8")
        row = {key: "" for key in MANIFEST_COLUMNS}
        row.update({
            "recording_id": "rec-001",
            "relative_csv_path": f"{action}/sample.csv",
            "sidecar_path": f"{action}/sample.meta.json",
            "sha256": digest,
            "participant_id": "P001",
            "session_id": "P001-R01-D01-S01",
            "date": "2026-09-01",
            "room_id": "R01",
            "receiver_id": "RX01",
            "action": action,
            "fall_type": "demonstrated_fall" if action == "wave" else "not_applicable",
            "frame_count": "2",
            "sequence_gap_count": "0",
            "device_telemetry_samples": "1",
            "status": "complete",
        })
        with (session / "session_manifest.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
            writer.writeheader()
            writer.writerow(row)
        return session

    def test_validator_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            result = validate(self.make_session(Path(temp)))
            self.assertEqual(result["status"], "PASS")

    def test_validator_requires_core_actions(self):
        with tempfile.TemporaryDirectory() as temp:
            result = validate(self.make_session(Path(temp)), require_core_actions=True)
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any("missing required actions" in error for error in result["errors"]))

    def test_validator_detects_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self.make_session(Path(temp))
            (session / "wave" / "sample.csv").write_text("modified", encoding="utf-8")
            result = validate(session)
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any("SHA-256 mismatch" in error for error in result["errors"]))

    def test_validator_rejects_aborted_recording(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self.make_session(Path(temp))
            manifest = session / "session_manifest.csv"
            with manifest.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["status"] = "aborted"
            with manifest.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            result = validate(session)
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any("not complete" in error for error in result["errors"]))

    def test_validator_detects_send_configuration_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self.make_session(Path(temp))
            manifest = session / "session_manifest.csv"
            with manifest.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0].update({
                "send_mode": "router_ping", "observed_send_mode": "null_data",
                "configured_send_interval_ms": "10", "observed_send_interval_ms": "100",
            })
            with manifest.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            result = validate(session)
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any("send interval mismatch" in error for error in result["errors"]))
            self.assertTrue(any("send mode mismatch" in error for error in result["errors"]))

    def set_manifest_fields(self, session: Path, **fields: str) -> None:
        manifest = session / "session_manifest.csv"
        with manifest.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0].update(fields)
        with manifest.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    def test_validator_rejects_research_transmitter_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self.make_session(Path(temp))
            self.set_manifest_fields(
                session,
                send_mode="espnow_research",
                observed_send_mode="espnow_research",
                transmitter_experiment_hash=str(0xB56201EF),
                observed_transmitter_experiment_hash="123",
                receiver_valid_research_packets_delta="4",
            )
            result = validate(session)
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any("Research transmitter experiment mismatch" in error
                                for error in result["errors"]))

    def test_validator_rejects_no_valid_research_packet(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self.make_session(Path(temp))
            expected_hash = str(research_experiment_hash("P001-R01-D01-S01"))
            self.set_manifest_fields(
                session,
                send_mode="espnow_research",
                observed_send_mode="espnow_research",
                transmitter_experiment_hash=expected_hash,
                observed_transmitter_experiment_hash=expected_hash,
                receiver_valid_research_packets_delta="0",
            )
            result = validate(session)
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any("No valid research packets within recording" in error
                                for error in result["errors"]))

    def test_ledger_core_action_session_validates_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "P001-R01-D01-S01"
            config = {
                "participant_id": "P001", "session_id": "P001-R01-D01-S01",
                "room_id": "R01", "receiver_id": "RX01", "date": "2026-09-01",
                "fall_type": "demonstrated_fall",
            }
            ledger = ExperimentLedger(session, config)
            for action_index, action in enumerate(sorted({"wave", "move", "static", "none", "front", "after"})):
                action_dir = session / action
                action_dir.mkdir(parents=True)
                csv_path = action_dir / f"sample-{action_index}.csv"
                csv_path.write_text("type,seq\nCSI_DATA,1\nCSI_DATA,2\n", encoding="utf-8")
                ledger.note_telemetry({
                    "type": "SYSTEM_TELEMETRY", "csi_received": action_index * 10,
                    "queue_drops": 0, "queue_depth": 0, "queue_high_water": 1,
                    "free_heap": 200000, "minimum_free_heap": 180000,
                    "send_interval_ms": 10, "send_mode": "router_ping",
                })
                ledger.begin(csv_path, action, str(action_index), duration_ms=2000)
                for frame_index in range(2):
                    ledger.add_csi({
                        "seq": action_index * 10 + frame_index,
                        "local_timestamp": 1_000_000 + frame_index * 10_000,
                        "rssi": -50, "noise_floor": -95, "agc_gain": 8,
                        "rx_state": 0, "first_word_invalid": 0, "len": 104,
                    }, 5_000_000_000 + frame_index * 10_000_000,
                       5_000_500_000 + frame_index * 10_000_000)
                ledger.note_telemetry({
                    "type": "SYSTEM_TELEMETRY", "csi_received": action_index * 10 + 2,
                    "queue_drops": 0, "queue_depth": 0, "queue_high_water": 1,
                    "free_heap": 199000, "minimum_free_heap": 180000,
                    "send_interval_ms": 10, "send_mode": "router_ping",
                })
                ledger.finish()
            result = validate(session, require_core_actions=True)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["recordings"], 6)


if __name__ == "__main__":
    unittest.main()
