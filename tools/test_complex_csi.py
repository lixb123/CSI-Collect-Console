from __future__ import annotations

import unittest

import numpy as np

from complex_csi import ComplexCSIQuality, parse_complex_csi, sanitized_phase


class ComplexCSITest(unittest.TestCase):
    def test_imag_real_layout(self):
        parsed = parse_complex_csi([2, 1, -4, 3])
        np.testing.assert_allclose(parsed, np.array([1 + 2j, 3 - 4j]))

    def test_rejects_odd_byte_count(self):
        with self.assertRaises(ValueError):
            parse_complex_csi([1, 2, 3])

    def test_removes_linear_subcarrier_phase(self):
        index = np.arange(52)
        phase = 0.2 * index + 1.3
        values = np.empty(104, dtype=np.int8)
        complex_values = 60 * np.exp(1j * phase)
        values[0::2] = np.round(complex_values.imag).astype(np.int8)
        values[1::2] = np.round(complex_values.real).astype(np.int8)
        residual, amplitude = sanitized_phase(values)
        self.assertLess(float(np.sqrt(np.mean(residual ** 2))), 0.05)
        self.assertGreater(float(amplitude.mean()), 50.0)

    def test_quality_summary(self):
        quality = ComplexCSIQuality()
        for offset in range(4):
            values = np.tile(np.array([offset + 2, 20], dtype=np.int8), 52)
            quality.add(values)
        summary = quality.summary()
        self.assertEqual(summary["complex_frames"], 4)
        self.assertEqual(summary["complex_subcarriers_min"], 52)
        self.assertEqual(summary["phase_quality_status"], "usable_sanitized")


if __name__ == "__main__":
    unittest.main()
