"""Complex CSI parsing and phase-usability diagnostics.

ESP-IDF CSI bytes are ordered [imaginary, real, imaginary, real, ...].
Absolute phase is not treated as propagation truth.  Diagnostics operate on
unwrapped phase after removing a per-frame linear trend across subcarriers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np


CSI_COMPONENT_ORDER = "imag_real_interleaved"
CSI_COMPONENT_DTYPE = "int8"
PHASE_SANITIZATION = "unwrap_subcarrier_remove_linear_trend_then_temporal_difference"


def parse_complex_csi(values: Any) -> np.ndarray:
    raw = np.asarray(values, dtype=np.float32).reshape(-1)
    if raw.size == 0 or raw.size % 2:
        raise ValueError(f"CSI byte count must be a non-zero even number, got {raw.size}")
    return raw[1::2] + 1j * raw[0::2]


def sanitized_phase(values: Any) -> tuple[np.ndarray, np.ndarray]:
    complex_csi = parse_complex_csi(values)
    amplitude = np.abs(complex_csi)
    phase = np.unwrap(np.angle(complex_csi))
    index = np.arange(phase.size, dtype=np.float64)
    valid = amplitude > max(float(np.median(amplitude)) * 0.10, 1.0)
    if valid.sum() >= 3:
        slope, intercept = np.polyfit(index[valid], phase[valid], 1)
        phase = phase - (slope * index + intercept)
    phase = phase - np.median(phase[valid] if valid.any() else phase)
    return phase.astype(np.float32), amplitude.astype(np.float32)


@dataclass
class ComplexCSIQuality:
    frames: int = 0
    subcarriers_min: int | None = None
    subcarriers_max: int | None = None
    invalid_frames: int = 0
    low_amplitude_fraction_sum: float = 0.0
    phase_residual_rms_sum: float = 0.0
    adjacent_phase_coherence_sum: float = 0.0
    temporal_phase_step_rms_sum: float = 0.0
    temporal_pairs: int = 0
    _previous_phase: np.ndarray | None = None

    def add(self, values: Any) -> None:
        try:
            phase, amplitude = sanitized_phase(values)
        except Exception:
            self.invalid_frames += 1
            return
        subcarriers = int(len(phase))
        self.subcarriers_min = subcarriers if self.subcarriers_min is None else min(self.subcarriers_min, subcarriers)
        self.subcarriers_max = subcarriers if self.subcarriers_max is None else max(self.subcarriers_max, subcarriers)
        threshold = max(float(np.median(amplitude)) * 0.10, 1.0)
        valid = amplitude > threshold
        self.low_amplitude_fraction_sum += float(np.mean(~valid))
        valid_phase = phase[valid]
        self.phase_residual_rms_sum += float(np.sqrt(np.mean(valid_phase ** 2))) if valid_phase.size else math.nan
        if valid_phase.size >= 2:
            self.adjacent_phase_coherence_sum += float(np.abs(np.mean(np.exp(1j * np.diff(valid_phase)))))
        if self._previous_phase is not None and self._previous_phase.shape == phase.shape:
            step = np.angle(np.exp(1j * (phase - self._previous_phase)))
            self.temporal_phase_step_rms_sum += float(np.sqrt(np.mean(step[valid] ** 2))) if valid.any() else math.nan
            self.temporal_pairs += 1
        self._previous_phase = phase
        self.frames += 1

    def summary(self) -> dict[str, Any]:
        frames = max(self.frames, 1)
        pairs = max(self.temporal_pairs, 1)
        result = {
            "complex_frames": self.frames,
            "complex_invalid_frames": self.invalid_frames,
            "complex_subcarriers_min": self.subcarriers_min,
            "complex_subcarriers_max": self.subcarriers_max,
            "complex_low_amplitude_fraction": self.low_amplitude_fraction_sum / frames,
            "phase_residual_rms_mean": self.phase_residual_rms_sum / frames,
            "phase_adjacent_coherence_mean": self.adjacent_phase_coherence_sum / frames,
            "phase_temporal_step_rms_mean": self.temporal_phase_step_rms_sum / pairs if self.temporal_pairs else None,
            "phase_temporal_pairs": self.temporal_pairs,
        }
        # This is an audit recommendation, not a training-set filter.
        result["phase_quality_status"] = (
            "invalid" if self.frames == 0 or self.invalid_frames > 0 else
            "review" if result["complex_low_amplitude_fraction"] > 0.25 or self.temporal_pairs < max(1, self.frames - 2) else
            "usable_sanitized"
        )
        return result


def complex_layout_metadata(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "complex_csi_enabled": bool(config.get("complex_csi_enabled", True)),
        "csi_component_order": config.get("csi_component_order", CSI_COMPONENT_ORDER),
        "component_dtype": CSI_COMPONENT_DTYPE,
        "bytes_per_complex": 2,
        "ltf_type": config.get("csi_output_type", "LLTF"),
        "channel_filter_enabled": bool(config.get("channel_filter_enabled", False)),
        "csi_scale_shift": int(config.get("csi_scale_shift", 0)),
        "phase_sanitization": PHASE_SANITIZATION,
        "absolute_phase_valid": False,
        "absolute_phase_warning": "CFO/SFO, packet timing, AGC, and RF-chain offsets make raw absolute ESP32 phase non-physical without calibration.",
    }
