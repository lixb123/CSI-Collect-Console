from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np

from csi_stability_diagnostic import compare_sessions, read_rows


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    fields = ["mac", "rate", "sig_mode", "mcs", "cwb", "channel_primary", "channel_secondary", "rx_seq", "len", "local_timestamp", "rssi", "agc_gain", "data"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class StabilityDiagnosticTest(unittest.TestCase):
    def test_selects_common_phy_signature_and_reports_saturation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normal = np.tile(np.array([2, 3, 4, 5], dtype=np.int8), 26)
            clipped = normal.copy()
            clipped[0] = 127
            rows = []
            for i in range(3):
                rows.append({"mac": "aa", "rate": "11", "sig_mode": "1", "mcs": "7", "cwb": "1", "channel_primary": "11", "channel_secondary": "2", "len": "104", "local_timestamp": str(i * 1000), "rssi": "-40", "agc_gain": "20", "data": str(normal.tolist())})
            rows.append({"mac": "aa", "rate": "11", "sig_mode": "0", "mcs": "0", "cwb": "0", "channel_primary": "11", "channel_secondary": "2", "len": "104", "local_timestamp": "5000", "rssi": "-60", "agc_gain": "30", "data": str(normal.tolist())})
            _write(root / "router.csv", rows)
            null_rows = [dict(rows[0], local_timestamp=str(i * 1000), data=str((clipped if i == 0 else normal).tolist())) for i in range(2)]
            _write(root / "null.csv", null_rows)
            result = compare_sessions(root / "router.csv", root / "null.csv", root / "output")
            self.assertEqual(result["selection"]["common_mac"], "aa")
            self.assertEqual(result["router_ping"]["frames"], 3)
            self.assertEqual(result["null_data"]["frames"], 2)
            self.assertGreater(result["null_data"]["saturation_fraction"], 0)
            self.assertFalse(result["selection"]["causal_claim_allowed"])

    def test_invalid_rows_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            _write(path, [{"mac": "aa", "rate": "11", "sig_mode": "1", "mcs": "7", "cwb": "1", "channel_primary": "11", "channel_secondary": "2", "len": "3", "local_timestamp": "0", "rssi": "-40", "agc_gain": "20", "data": "[1]"}])
            rows, audit = read_rows(path)
            self.assertEqual(rows, [])
            self.assertEqual(audit["invalid_length_rows"], 1)

    def test_repeated_rx_sequence_is_removed_when_sequence_is_informative(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "retry.csv"
            base = {"mac": "aa", "rate": "11", "sig_mode": "1", "mcs": "7", "cwb": "1", "channel_primary": "11", "channel_secondary": "2", "len": "104", "rssi": "-40", "agc_gain": "20", "data": str([2, 3] * 52)}
            _write(path, [
                dict(base, local_timestamp="1000", rx_seq="1"),
                dict(base, local_timestamp="2000", rx_seq="2"),
                dict(base, local_timestamp="3000", rx_seq="2"),
                dict(base, local_timestamp="4000", rx_seq="3"),
            ])
            rows, audit = read_rows(path)
            self.assertEqual(len(rows), 3)
            self.assertEqual(audit["duplicate_rx_seq_rows"], 1)
            self.assertEqual(audit["rx_seq_candidate_filter_applied"], 1)

    def test_rx_sequence_wrap_is_kept_after_candidate_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrap.csv"
            base = {"mac": "aa", "rate": "11", "sig_mode": "1", "mcs": "7", "cwb": "1", "channel_primary": "11", "channel_secondary": "2", "len": "104", "rssi": "-40", "agc_gain": "20", "data": str([2, 3] * 52)}
            _write(path, [
                dict(base, local_timestamp="1000", rx_seq="1"),
                dict(base, local_timestamp="201000", rx_seq="1"),
            ])
            rows, audit = read_rows(path)
            self.assertEqual(len(rows), 2)
            self.assertNotIn("duplicate_rx_seq_rows", audit)


if __name__ == "__main__":
    unittest.main()
