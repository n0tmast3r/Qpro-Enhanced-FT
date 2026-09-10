#!/usr/bin/env python3
"""Calibrate Meta's personalized, independent local-branch visual axes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from stereo_eye_calibration import StereoEyeCalibrationController


def affine_features(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if values.ndim == 1:
        values = values.reshape(1, 2)
    return np.column_stack([np.ones(len(values), dtype=np.float64), values])


def error_metrics(predicted: np.ndarray, expected: np.ndarray) -> dict[str, float]:
    delta = np.asarray(predicted) - np.asarray(expected)
    angular = np.linalg.norm(delta, axis=1)
    return {
        "yaw_mae_deg": float(np.mean(np.abs(delta[:, 0]))),
        "pitch_mae_deg": float(np.mean(np.abs(delta[:, 1]))),
        "angular_mae_deg": float(np.mean(angular)),
        "angular_p95_deg": float(np.percentile(angular, 95)),
    }


def fit_axis_eye_mapping(samples: list[dict[str, Any]], eye: str) -> dict[str, Any]:
    """Fit a conservative independent affine correction for one visual axis."""
    if eye not in ("left", "right"):
        raise ValueError("eye must be left or right")
    if len(samples) < 120:
        raise ValueError(f"Need at least 120 gaze samples, got {len(samples)}")
    raw = np.asarray([sample[f"{eye}_raw"] for sample in samples], dtype=np.float64)
    target = np.asarray(
        [sample[f"{eye}_target_deg"] for sample in samples], dtype=np.float64
    )
    features = affine_features(raw)
    started_ns = min(int(sample["timestamp_ns"]) for sample in samples)
    blocks = np.asarray(
        [
            (int(sample["timestamp_ns"]) - started_ns) // 1_000_000_000
            for sample in samples
        ],
        dtype=np.int64,
    )
    holdout = blocks % 5 == 4
    if int(np.count_nonzero(holdout)) < 24:
        holdout = np.arange(len(samples)) % 5 == 4
    training = ~holdout
    penalty = np.diag([0.0, 0.002, 0.002])

    def solve(indices: np.ndarray) -> np.ndarray:
        selected = features[indices]
        return np.linalg.solve(
            selected.T @ selected + penalty,
            selected.T @ target[indices],
        )

    coefficients = solve(training)
    training_error = np.linalg.norm(
        features[training] @ coefficients - target[training], axis=1
    )
    median = float(np.median(training_error))
    mad = float(np.median(np.abs(training_error - median)))
    cutoff = max(1.5, median + 4.0 * max(mad, 0.15))
    clean_training = np.flatnonzero(training)[training_error <= cutoff]
    if len(clean_training) >= 96:
        coefficients = solve(clean_training)
    held_prediction = features[holdout] @ coefficients

    all_error = np.linalg.norm(features @ coefficients - target, axis=1)
    all_median = float(np.median(all_error))
    all_mad = float(np.median(np.abs(all_error - all_median)))
    clean = all_error <= max(1.5, all_median + 4.0 * max(all_mad, 0.15))
    if int(np.count_nonzero(clean)) >= 120:
        coefficients = solve(clean)

    prediction = features @ coefficients
    return {
        "model": "independent-yaw-pitch-affine",
        "coefficients": coefficients.tolist(),
        "input_order": ["1", "visual_yaw_deg", "visual_pitch_deg"],
        "output_order": ["target_yaw_deg", "target_pitch_deg"],
        "sample_count": len(samples),
        "retained_count": int(np.count_nonzero(clean)),
        "holdout_count": int(np.count_nonzero(holdout)),
        "held_out": error_metrics(held_prediction, target[holdout]),
        "all_samples": error_metrics(prediction, target),
        "raw_range_deg": {
            "yaw": [float(np.min(raw[:, 0])), float(np.max(raw[:, 0]))],
            "pitch": [float(np.min(raw[:, 1])), float(np.max(raw[:, 1]))],
        },
        "target_range_deg": {
            "yaw": [float(np.min(target[:, 0])), float(np.max(target[:, 0]))],
            "pitch": [float(np.min(target[:, 1])), float(np.max(target[:, 1]))],
        },
    }


def apply_axis_eye_mapping(
    mapping: dict[str, Any], angles: tuple[float, float] | list[float]
) -> tuple[float, float]:
    coefficients = np.asarray(mapping["coefficients"], dtype=np.float64)
    prediction = affine_features(np.asarray(angles, dtype=np.float64))[0] @ coefficients
    return float(prediction[0]), float(prediction[1])


def evaluate_independent_convergence(
    samples: list[dict[str, Any]],
    left_mapping: dict[str, Any],
    right_mapping: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate vergence derived after two isolated per-eye mappings."""
    result: dict[str, Any] = {}
    for phase in ("gaze", "convergence"):
        selected = [sample for sample in samples if sample["phase"] == phase]
        if len(selected) < 60:
            raise ValueError(f"Need at least 60 {phase} samples, got {len(selected)}")
        mapped_left = np.asarray(
            [apply_axis_eye_mapping(left_mapping, sample["left_raw"]) for sample in selected]
        )
        mapped_right = np.asarray(
            [apply_axis_eye_mapping(right_mapping, sample["right_raw"]) for sample in selected]
        )
        predicted = mapped_right[:, 0] - mapped_left[:, 0]
        expected = np.asarray(
            [
                sample["right_target_deg"][0] - sample["left_target_deg"][0]
                for sample in selected
            ],
            dtype=np.float64,
        )
        raw = np.asarray(
            [sample["right_raw"][0] - sample["left_raw"][0] for sample in selected],
            dtype=np.float64,
        )
        error = np.abs(predicted - expected)
        correlation = float(np.corrcoef(predicted, expected)[0, 1])
        if not np.isfinite(correlation):
            correlation = 0.0
        result[phase] = {
            "sample_count": len(selected),
            "mae_deg": float(np.mean(error)),
            "p95_deg": float(np.percentile(error, 95)),
            "correlation": correlation,
            "raw_disparity_range_deg": [float(np.min(raw)), float(np.max(raw))],
            "mapped_disparity_range_deg": [
                float(np.min(predicted)), float(np.max(predicted))
            ],
            "target_disparity_range_deg": [
                float(np.min(expected)), float(np.max(expected))
            ],
            "distance_range_m": [
                float(min(sample["distance_m"] for sample in selected)),
                float(max(sample["distance_m"] for sample in selected)),
            ],
        }
    return result


class VisualAxisCalibrationController(StereoEyeCalibrationController):
    """Collect and fit the patched model's post-personalization eye axes."""

    def add_axis_sample(
        self,
        left_angles: tuple[float, float],
        right_angles: tuple[float, float],
        timestamp_ns: int,
        *,
        kernel_time_s: float | None = None,
    ) -> None:
        with self._lock:
            phase = self.phase
        target = self.target_at(timestamp_ns)
        if phase not in ("gaze", "convergence") or target is None:
            return
        # DetectorOutputParser exposes tag 0 through ``left_angles`` and tag 1
        # through ``right_angles`` for historical reasons.  Physical testing
        # (closing/drifting one eye at a time) established that Seacliff's
        # detector tags have the opposite ownership: tag 1 is the physical
        # left eye and tag 0 is the physical right eye.  Store physical-eye
        # semantics here so the fitted maps and the VR output cannot cross.
        physical_left = right_angles
        physical_right = left_angles
        sample: dict[str, Any] = {
            "timestamp_ns": int(timestamp_ns),
            "phase": phase,
            "distance_m": target.distance,
            "left_raw": [float(physical_left[0]), float(physical_left[1])],
            "right_raw": [float(physical_right[0]), float(physical_right[1])],
            "detector_tag_mapping": {
                "physical_left": "trace_tag_1",
                "physical_right": "trace_tag_0",
            },
            # Match Baballonia's FrameCollector convention.
            "left_target_deg": [-target.left_yaw, target.left_pitch],
            "right_target_deg": [-target.right_yaw, target.right_pitch],
        }
        if kernel_time_s is not None:
            sample["headset_kernel_time_s"] = float(kernel_time_s)
        with self._lock:
            self.samples.append(sample)

    def _fit_and_save(self) -> None:
        with self._lock:
            self.phase = "fitting"
            self.message = "Fitting isolated affine corrections for each Meta visual axis..."
            samples = list(self.samples)
        try:
            gaze = [sample for sample in samples if sample["phase"] == "gaze"]
            left = fit_axis_eye_mapping(gaze, "left")
            right = fit_axis_eye_mapping(gaze, "right")
            convergence = evaluate_independent_convergence(samples, left, right)
            gaze_pass = bool(
                left["held_out"]["angular_mae_deg"] <= 2.0
                and right["held_out"]["angular_mae_deg"] <= 2.0
                and left["held_out"]["angular_p95_deg"] <= 4.0
                and right["held_out"]["angular_p95_deg"] <= 4.0
            )
            convergence_pass = bool(
                convergence["convergence"]["mae_deg"] <= 1.5
                and convergence["convergence"]["p95_deg"] <= 3.0
                and convergence["convergence"]["correlation"] >= 0.65
            )
            result = {
                "format": "qpro-independent-personalized-visual-axis-v2",
                "created_unix_ns": time.time_ns(),
                "source": (
                    "Seacliff node 18 local per-eye branch, followed by Meta "
                    "VisualAxisDetector personalization"
                ),
                "mapping_constraint": (
                    "left and right affine mappings are fitted independently; "
                    "convergence is derived only after mapping"
                ),
                "detector_tag_mapping": {
                    "physical_left": "trace_tag_1",
                    "physical_right": "trace_tag_0",
                },
                "sample_count": len(samples),
                "phases": {
                    name: sum(sample["phase"] == name for sample in samples)
                    for name in ("gaze", "convergence")
                },
                "left": left,
                "right": right,
                "derived_convergence": convergence,
                "quality_gate": {
                    "status": "pass" if gaze_pass and convergence_pass else (
                        "partial_pass" if gaze_pass or convergence_pass else "fail"
                    ),
                    "gaze_pass": gaze_pass,
                    "convergence_pass": convergence_pass,
                },
            }
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            sample_path = self.output_path.with_suffix(".samples.jsonl")
            with sample_path.open("w", encoding="utf-8") as output:
                for sample in samples:
                    output.write(json.dumps(sample, separators=(",", ":")) + "\n")
            result["sample_path"] = str(sample_path)
            self.output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            with self._lock:
                self.result = result
                self.phase = "done"
                self.message = (
                    f"Saved ({result['quality_gate']['status']}). Gaze MAE "
                    f"L {left['held_out']['angular_mae_deg']:.2f}, "
                    f"R {right['held_out']['angular_mae_deg']:.2f} deg; moving-depth "
                    f"vergence MAE {convergence['convergence']['mae_deg']:.2f} deg. Press Q."
                )
            try:
                self._send_routine("close", 1)
            except OSError:
                pass
        except Exception as error:
            with self._lock:
                self.phase = "error"
                self.message = f"Visual-axis calibration failed: {error}"

    def status(self) -> tuple[str, str, int]:
        with self._lock:
            return self.phase, self.message, len(self.samples)


__all__ = [
    "VisualAxisCalibrationController",
    "apply_axis_eye_mapping",
    "evaluate_independent_convergence",
    "fit_axis_eye_mapping",
]
