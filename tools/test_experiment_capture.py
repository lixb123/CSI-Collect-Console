from __future__ import annotations

import csv
import json
import tempfile
import unittest
import hashlib
import ast
from queue import Queue
from types import SimpleNamespace
from pathlib import Path

from experiment_capture import (
    ExperimentLedger,
    MANIFEST_COLUMNS,
    load_experiment_config,
    normalize_mac,
    research_experiment_hash,
    session_baseline_status,
    session_reference_status,
    validate_action_label,
)
from validate_session import validate


class ExperimentLedgerTest(unittest.TestCase):
    def test_gui_has_distinct_reference_buttons_and_unrecorded_statuses(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtWidgets import QApplication
        from esp_csi_tool import DataGraphicalWindow, SerialConnectionController
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as temp:
            config = {
                "participant_id": "P001", "session_id": "S001", "room_id": "R01",
                "receiver_id": "RX01", "router_id": "AP01", "target_mac": "aa:bb:cc:dd:ee:ff",
                "date": "2026-09-23", "send_mode": "router_ping",
                "require_static_baseline": True, "baseline_min_duration_s": 60,
                "acquisition_gate_enabled": False,
            }
            controller = SerialConnectionController(Queue(), Queue(), "LLTF", config, temp)
            window = DataGraphicalWindow(controller, "LLTF", config, temp)
            self.assertEqual(window.pushButton_empty_room.text(), "Record 60 s empty room")
            window.language = "zh"
            window.apply_language()
            window.refresh_baseline_status()
            self.assertEqual(window.pushButton_empty_room.text(), "采集 60 秒空房参考")
            self.assertEqual(window.pushButton_static_baseline.text(), "采集 60 秒人员站立静止基线")
            self.assertIn("未通过", window.label_empty_room_status.text())
            self.assertIn("人员站立静止基线 60 秒", window.label_baseline_status.text())
            grid = window.collection_controls_layout
            baseline_rows = [
                grid.getItemPosition(grid.indexOf(window.pushButton_empty_room))[0],
                grid.getItemPosition(grid.indexOf(window.pushButton_static_baseline))[0],
            ]
            action_label_row = grid.getItemPosition(grid.indexOf(window.label_target_4))[0]
            self.assertEqual(baseline_rows[0], baseline_rows[1])
            self.assertEqual(baseline_rows[0] + 1, action_label_row)
            self.assertEqual(window.spinBox_collect_duration.value(), 3)
            window.pushButton_collect_show = lambda: window.pushButton_collect_start.setText("停止")
            window.start_empty_room_reference()
            self.assertEqual(window.timeEdit_collect_delay.value(), 15)
            self.assertEqual(window.spinBox_collect_duration.value(), 60)
            window.finish_collection(send_stop=False)
            self.assertEqual(window.timeEdit_collect_delay.value(), 3)
            self.assertEqual(window.spinBox_collect_duration.value(), 3)
            window.close()
            controller.stop()
            app.processEvents()

    def test_empty_room_reference_status_is_session_and_quality_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {
                "participant_id": "P001", "session_id": "S001", "room_id": "R01",
                "receiver_id": "RX01", "router_id": "AP01", "target_mac": "aa:bb:cc:dd:ee:ff",
                "acquisition_gate_enabled": False,
            }
            fields = ["session_id", "calibration_role", "status", "device_time_span_s",
                      "host_time_span_s", "quality_status", "recording_id"]
            manifest = root / "session_manifest.csv"
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"session_id": "OTHER", "calibration_role": "empty_room_reference",
                                 "status": "complete", "device_time_span_s": 60,
                                 "quality_status": "pass", "recording_id": "wrong-session"})
                writer.writerow({"session_id": "S001", "calibration_role": "empty_room_reference",
                                 "status": "complete", "device_time_span_s": 60,
                                 "quality_status": "fail", "recording_id": "bad-quality"})
                writer.writerow({"session_id": "S001", "calibration_role": "empty_room_reference",
                                 "status": "complete", "device_time_span_s": 60,
                                 "quality_status": "pass", "recording_id": "empty-ok"})
            status = session_reference_status(root, config, "empty_room_reference")
            self.assertTrue(status["ready"])
            self.assertEqual(status["recording_id"], "empty-ok")

    def test_data_root_resolution_is_portable_and_migrates_stale_absolute_paths(self):
        from esp_csi_tool import resolve_data_root

        with tempfile.TemporaryDirectory() as temp:
            working_directory = Path(temp)
            self.assertEqual(
                Path(resolve_data_root('data', working_directory)),
                (working_directory / 'data').resolve(),
            )
            stale_root = working_directory / 'missing-old-computer-root'
            self.assertEqual(
                Path(resolve_data_root(stale_root, working_directory)),
                (working_directory / 'data').resolve(),
            )

    def test_session_identity_metadata_tracks_new_site_devices(self):
        from esp_csi_tool import session_identity_metadata

        metadata = session_identity_metadata(
            'P007-SITE03-RX02-20260918-S01', 'RX_SITE03_02', 'AP_SITE03_01')
        self.assertEqual(
            metadata['device_pair_id'], 'RX_SITE03_02__AP_SITE03_01')
        self.assertEqual(
            metadata['transmitter_experiment_id'], 'P007-SITE03-RX02-20260918-S01')
        self.assertEqual(
            metadata['calibration_id'], 'P007-SITE03-RX02-20260918-S01-CAL01')
        self.assertEqual(
            metadata['trial_id_prefix'], 'P007-SITE03-RX02-20260918-S01-')

    def test_connected_controller_sends_runtime_experiment_context(self):
        from esp_csi_tool import SerialConnectionController

        queue_write = Queue()
        controller = SerialConnectionController(Queue(), queue_write, "LLTF", None, "data")
        controller.process = SimpleNamespace(is_alive=lambda: True)
        config = {"session_id": "P001-R01-D01-S01"}
        controller.set_experiment_context(config, "captures")
        command = queue_write.get_nowait()
        self.assertEqual(command["type"], "activate_experiment")
        self.assertEqual(command["experiment_config"], config)
        self.assertEqual(command["data_root"], "captures")

    def test_gui_exposes_only_current_core_action_labels(self):
        source_path = Path(__file__).with_name("esp_csi_tool.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        action_node = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "ACTION_LABELS"
                    for target in node.targets)
        )
        labels = [ast.literal_eval(item.elts[0]) for item in action_node.value.elts]
        self.assertEqual(labels, [
            "unknown", "none", "static", "walk_slow", "walk_fast", "sit down",
            "stand up", "bend", "pick_up", "lie_down", "turn", "fall_forward",
            "fall_backward", "fall_left", "fall_right",
        ])

    def test_unified_collection_items_map_to_action_and_role(self):
        source_path = Path(__file__).with_name("esp_csi_tool.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        collection_node = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "COLLECTION_ITEMS"
                    for target in node.targets)
        )
        items = [ast.literal_eval(item) for item in collection_node.value.elts]
        mapping = {key: (action, role) for key, action, role, _, _ in items}
        self.assertEqual(mapping["empty_room_reference"], ("none", "empty_room_reference"))
        self.assertEqual(mapping["activity_static"], ("static", "activity"))
        self.assertEqual(mapping["activity_fall_forward"], ("fall_forward", "activity"))
        self.assertNotIn("static_reference", mapping)
        self.assertEqual(len(items), 15)

    def test_normalize_mac_accepts_common_notation(self):
        self.assertEqual(normalize_mac("AA-BB-CC-DD-EE-FF"), "aa:bb:cc:dd:ee:ff")
        with self.assertRaises(ValueError):
            normalize_mac("not-a-mac")

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

    def test_valid_config_load_and_save_reload_telemetry_default(self):
        template = Path(__file__).parent / "config" / "experiment_config.example.json"
        base = json.loads(template.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.json"
            for value, expected in ((None, 1), (0, 1), (-1, 1), ("3", 3), ("invalid", 1)):
                with self.subTest(value=value):
                    config = dict(base, minimum_telemetry_samples=value)
                    path.write_text(json.dumps(config), encoding="utf-8")
                    loaded = load_experiment_config(path)
                    self.assertEqual(loaded["minimum_telemetry_samples"], expected)
                    path.write_text(json.dumps(loaded), encoding="utf-8")
                    self.assertEqual(load_experiment_config(path)["minimum_telemetry_samples"], expected)
            base.pop("minimum_telemetry_samples", None)
            path.write_text(json.dumps(base), encoding="utf-8")
            self.assertEqual(load_experiment_config(path)["minimum_telemetry_samples"], 1)

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

    def test_quality_gate_records_mac_and_phy_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            root.mkdir(parents=True)
            config = {
                "participant_id": "P001", "session_id": "S001", "room_id": "R01",
                "receiver_id": "RX01", "router_id": "AP01",
                "target_mac": "aa:bb:cc:dd:ee:ff",
                "minimum_target_mac_fraction": 0.95,
                "minimum_dominant_phy_fraction": 0.95,
            }
            ledger = ExperimentLedger(root, config)
            csv_path = root / "mixed.csv"
            csv_path.write_text("type,seq\n", encoding="utf-8")
            ledger.begin(csv_path, "static", "1")
            for index in range(10):
                ledger.add_csi({
                    "seq": index, "local_timestamp": index * 10_000,
                    "mac": "aa:bb:cc:dd:ee:ff" if index < 9 else "11:22:33:44:55:66",
                    "rate": 11 if index < 9 else 1, "sig_mode": 1, "mcs": 3,
                    "cwb": 1, "channel_primary": 11, "channel_secondary": 2, "len": 104,
                    "rx_state": 0, "first_word_invalid": 0,
                }, index * 10_000_000, index * 10_000_000)
            result = ledger.finish()
            self.assertEqual(result["quality_status"], "fail")
            self.assertAlmostEqual(result["target_mac_frame_fraction"], 0.9)
            self.assertAlmostEqual(result["dominant_phy_fraction"], 0.9)
            self.assertEqual(result["phy_signature_count"], 2)

    def test_activity_requires_qualified_static_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            root.mkdir(parents=True)
            config = {
                "participant_id": "P001", "session_id": "S001", "room_id": "R01",
                "receiver_id": "RX01", "router_id": "AP01",
                "target_mac": "aa:bb:cc:dd:ee:ff", "require_static_baseline": True,
                "baseline_min_duration_s": 1, "calibration_role": "activity",
            }
            ledger = ExperimentLedger(root, config)
            activity_path = root / "activity.csv"
            activity_path.write_text("type,seq\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ledger.begin(activity_path, "wave", "1")

            ledger.update_capture_role(complex_csi_enabled=True, calibration_role="static_reference")
            baseline_path = root / "baseline.csv"
            baseline_path.write_text("type,seq\n", encoding="utf-8")
            ledger.begin(baseline_path, "static", "2")
            ledger.note_telemetry({
                "type": "SYSTEM_TELEMETRY", "csi_received": 10, "queue_drops": 0,
                "send_interval_ms": 10, "send_mode": "router_ping",
            })
            for index, timestamp in enumerate((0, 1_000_000)):
                ledger.add_csi({
                    "seq": index, "local_timestamp": timestamp,
                    "mac": "aa:bb:cc:dd:ee:ff", "rate": 11, "sig_mode": 1, "mcs": 3,
                    "cwb": 1, "channel_primary": 11, "channel_secondary": 2, "len": 104,
                    "rx_state": 0, "first_word_invalid": 0,
                }, index * 1_000_000_000, index * 1_000_000_000)
            ledger.finish()
            self.assertTrue(session_baseline_status(root, ledger.config)["ready"])

            ledger.update_capture_role(complex_csi_enabled=True, calibration_role="activity")
            ledger.begin(activity_path, "wave", "3")
            self.assertTrue(ledger.active.baseline_ready_at_start)
            ledger.abort()


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

    def test_validator_can_select_one_session_from_day_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self.make_session(Path(temp))
            result = validate(session, session_id='P001-R01-D01-S01')
            self.assertEqual(result['status'], 'PASS')
            self.assertTrue(
                (session / 'validation_report_P001-R01-D01-S01.json').exists())

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

    def test_validator_rejects_multisite_quality_gate_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self.make_session(Path(temp))
            self.set_manifest_fields(
                session,
                schema_version="csi-experiment-manifest/1.1",
                target_mac="aa:bb:cc:dd:ee:ff",
                observed_primary_mac="aa:bb:cc:dd:ee:ff",
                target_mac_frame_fraction="0.90",
                dominant_phy_fraction="0.90",
                quality_status="fail",
                quality_reasons='["target_mac_fraction","dominant_phy_fraction"]',
                calibration_role="activity",
                baseline_required="true",
                baseline_ready_at_start="false",
            )
            result = validate(session)
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any("Target MAC frame fraction" in error for error in result["errors"]))
            self.assertTrue(any("Dominant PHY fraction" in error for error in result["errors"]))
            self.assertTrue(any("without a qualified static baseline" in error for error in result["errors"]))

    def test_validator_rejects_current_schema_without_telemetry(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self.make_session(Path(temp))
            self.set_manifest_fields(
                session,
                schema_version="csi-experiment-manifest/1.1",
                target_mac="aa:bb:cc:dd:ee:ff",
                observed_primary_mac="aa:bb:cc:dd:ee:ff",
                target_mac_frame_fraction="1.0",
                dominant_phy_fraction="1.0",
                quality_status="pass",
                device_telemetry_samples="0",
            )
            result = validate(session)
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any("No in-recording device telemetry" in error for error in result["errors"]))
            self.assertTrue(any("telemetry_start/telemetry_end" in error for error in result["errors"]))

    def test_ledger_core_action_session_validates_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "P001-R01-D01-S01"
            config = {
                "participant_id": "P001", "session_id": "P001-R01-D01-S01",
                "room_id": "R01", "receiver_id": "RX01", "date": "2026-09-01",
                "fall_type": "demonstrated_fall",
            }
            ledger = ExperimentLedger(session, config)
            core_actions = {
                "none", "static", "walk_slow", "walk_fast", "sit down", "stand up",
                "bend", "pick_up", "lie_down", "turn",
                "fall_forward", "fall_backward", "fall_left", "fall_right",
            }
            for action_index, action in enumerate(sorted(core_actions)):
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
                        "data": [1, 2] * 52,
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
            self.assertEqual(result["recordings"], 14)


if __name__ == "__main__":
    unittest.main()
