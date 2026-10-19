from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from export_training_data import export_training_data


class TrainingExportTest(unittest.TestCase):
    def test_exports_only_target_mac_and_dominant_phy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "session"
            source_dir = root / "walk_slow"
            source_dir.mkdir(parents=True)
            source = source_dir / "sample.csv"
            fields = ["type", "mac", "rate", "sig_mode", "mcs", "cwb", "channel_primary", "channel_secondary", "len", "data"]
            rows = [
                {"type": "CSI_DATA", "mac": "aa:bb:cc:dd:ee:ff", "rate": "11", "sig_mode": "0", "mcs": "0", "cwb": "0", "channel_primary": "4", "channel_secondary": "0", "len": "104", "data": "x"},
                {"type": "CSI_DATA", "mac": "aa:bb:cc:dd:ee:ff", "rate": "11", "sig_mode": "0", "mcs": "0", "cwb": "0", "channel_primary": "4", "channel_secondary": "0", "len": "104", "data": "y"},
                {"type": "CSI_DATA", "mac": "11:22:33:44:55:66", "rate": "1", "sig_mode": "0", "mcs": "0", "cwb": "0", "channel_primary": "4", "channel_secondary": "0", "len": "104", "data": "z"},
            ]
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            manifest_fields = [
                "schema_version", "recording_id", "relative_csv_path", "status", "quality_status",
                "device_telemetry_samples", "target_mac", "target_mac_frame_fraction",
                "dominant_phy_fraction", "dominant_phy_signature", "session_id", "participant_id",
                "site_id", "room_id", "receiver_id", "router_id", "device_pair_id", "action", "send_mode",
                "frame_count",
            ]
            manifest = root / "session_manifest.csv"
            with manifest.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=manifest_fields)
                writer.writeheader()
                writer.writerow({
                    "schema_version": "csi-experiment-manifest/1.1", "recording_id": "rec-1",
                    "relative_csv_path": "walk_slow/sample.csv", "status": "complete", "quality_status": "pass",
                    "device_telemetry_samples": "3", "target_mac": "aa:bb:cc:dd:ee:ff",
                    "target_mac_frame_fraction": "0.99", "dominant_phy_fraction": "0.99",
                    "dominant_phy_signature": json.dumps({"rate": "11", "sig_mode": "0", "mcs": "0", "cwb": "0", "channel_primary": "4", "channel_secondary": "0", "len": "104"}),
                    "session_id": "S1", "participant_id": "P1", "site_id": "SITE1", "room_id": "R1",
                    "receiver_id": "RX1", "router_id": "AP1", "device_pair_id": "RX1__AP1", "action": "walk_slow",
                    "send_mode": "router_ping", "frame_count": "3",
                })
            report = export_training_data(root, root / "training_filtered")
            self.assertEqual(report["selected_recordings"], 1)
            exported = root / "training_filtered" / "walk_slow" / "sample.csv"
            self.assertTrue(exported.exists())
            with exported.open(encoding="utf-8", newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 2)
            self.assertEqual(source.read_text(encoding="utf-8").count("CSI_DATA"), 3)

    def test_rejects_missing_telemetry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "session_manifest.csv").write_text(
                "status,quality_status,device_telemetry_samples,target_mac,target_mac_frame_fraction,dominant_phy_fraction\n"
                "complete,pass,0,aa:bb:cc:dd:ee:ff,1.0,1.0\n",
                encoding="utf-8",
            )
            report = export_training_data(root, root / "out")
            self.assertEqual(report["selected_recordings"], 0)
            self.assertEqual(report["rejected"][0]["reason"], "insufficient_device_telemetry")

    def test_requires_session_id_for_multi_session_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "session_manifest.csv").write_text(
                "session_id,status\nS1,complete\nS2,complete\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "multiple sessions"):
                export_training_data(root, root / "out")


if __name__ == "__main__":
    unittest.main()
