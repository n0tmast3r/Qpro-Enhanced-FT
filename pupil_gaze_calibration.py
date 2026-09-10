#!/usr/bin/env python3
"""Calibrate Meta's independent neural pupil tensors to per-eye gaze."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from eye_signal_filter import IndependentEyeFilter
from stereo_eye_calibration import StereoEyeCalibrationController, quaternion_yaw_pitch


PUPIL_MEAN = {
    "left": np.asarray(
        [-0.0017055189605983, 0.0045290040805944, -0.01040792463589],
        dtype=np.float64,
    ),
    "right": np.asarray(
        [0.00046130682644319, 0.0017377919915684, -0.010235720690707],
        dtype=np.float64,
    ),
}
PUPIL_SCALE = {
    "left": np.asarray(
        [0.0035429674058232, 0.0050770516145521, 0.0042806321969521],
        dtype=np.float64,
    ),
    "right": np.asarray(
        [0.0047817454012475, 0.0055140730704888, 0.0051256563049549],
        dtype=np.float64,
    ),
}
FEATURE_ORDER = ["1", "x", "y", "z", "x2", "xy", "xz", "y2", "yz", "z2"]
SEPARABLE_FEATURE_ORDER = {
    "yaw": ["1", "x", "x2"],
    "pitch": ["1", "y", "y2"],
}
STEREO_FEATURE_ORDER = ["1"] + [
    f"{eye}_{axis}" for eye in ("left", "right") for axis in ("x", "y", "z")
] + [
    f"{first}*{second}"
    for first_index, first in enumerate(
        [f"{eye}_{axis}" for eye in ("left", "right") for axis in ("x", "y", "z")]
    )
    for second in [
        f"{eye}_{axis}" for eye in ("left", "right") for axis in ("x", "y", "z")
    ][first_index:]
]
RAY_STEREO_FEATURE_ORDER = [
    "1",
    "raw_disparity",
    "common_yaw",
    "common_pitch",
    "raw_disparity2",
    "raw_disparity*common_yaw",
    "raw_disparity*common_pitch",
    "common_yaw2",
    "common_yaw*common_pitch",
    "common_pitch2",
]


def pupil_features(raw: np.ndarray, eye: str) -> np.ndarray:
    values = np.asarray(raw, dtype=np.float64)
    if values.ndim == 1:
        values = values.reshape(1, 3)
    if values.ndim != 2 or values.shape[1] != 3:
        raise ValueError("pupil values must be Nx3")
    normalized = (values - PUPIL_MEAN[eye]) / PUPIL_SCALE[eye]
    x, y, z = normalized.T
    return np.column_stack(
        [np.ones(len(values)), x, y, z, x * x, x * y, x * z, y * y, y * z, z * z]
    )


def stereo_pupil_features(left_raw: np.ndarray, right_raw: np.ndarray) -> np.ndarray:
    """Build the six-coordinate quadratic feature vector used for vergence."""
    left = np.asarray(left_raw, dtype=np.float64)
    right = np.asarray(right_raw, dtype=np.float64)
    if left.ndim == 1:
        left = left.reshape(1, 3)
    if right.ndim == 1:
        right = right.reshape(1, 3)
    if left.shape != right.shape or left.ndim != 2 or left.shape[1] != 3:
        raise ValueError("left and right pupil values must both be Nx3")
    values = np.column_stack(
        [
            (left - PUPIL_MEAN["left"]) / PUPIL_SCALE["left"],
            (right - PUPIL_MEAN["right"]) / PUPIL_SCALE["right"],
        ]
    )
    columns = [np.ones(len(values), dtype=np.float64)]
    columns.extend(values[:, index] for index in range(values.shape[1]))
    for first in range(values.shape[1]):
        for second in range(first, values.shape[1]):
            columns.append(values[:, first] * values[:, second])
    return np.column_stack(columns)


def ray_stereo_features(
    left_gaze_deg: np.ndarray, right_gaze_deg: np.ndarray
) -> np.ndarray:
    """Build a bounded direction-aware feature vector without altering either ray."""
    left = np.asarray(left_gaze_deg, dtype=np.float64)
    right = np.asarray(right_gaze_deg, dtype=np.float64)
    if left.ndim == 1:
        left = left.reshape(1, 2)
    if right.ndim == 1:
        right = right.reshape(1, 2)
    if left.shape != right.shape or left.ndim != 2 or left.shape[1] != 2:
        raise ValueError("left and right gaze values must both be Nx2")
    disparity = right[:, 0] - left[:, 0]
    common_yaw = (right[:, 0] + left[:, 0]) * 0.5
    common_pitch = (right[:, 1] + left[:, 1]) * 0.5
    return np.column_stack(
        [
            np.ones(len(left), dtype=np.float64),
            disparity,
            common_yaw,
            common_pitch,
            disparity * disparity,
            disparity * common_yaw,
            disparity * common_pitch,
            common_yaw * common_yaw,
            common_yaw * common_pitch,
            common_pitch * common_pitch,
        ]
    )


def _shifted_rows(length: int, lag_frames: int) -> tuple[np.ndarray, np.ndarray]:
    """Return pupil and target indices for a measured recorder timing offset."""
    if abs(lag_frames) >= length:
        raise ValueError("lag is longer than the calibration")
    if lag_frames > 0:
        return np.arange(length - lag_frames), np.arange(lag_frames, length)
    if lag_frames < 0:
        return np.arange(-lag_frames, length), np.arange(length + lag_frames)
    rows = np.arange(length)
    return rows, rows


def fit_pupil_stereo_mapping(
    samples: list[dict[str, Any]], lag_frames: int = -5, ridge: float = 0.001
) -> dict[str, Any]:
    """Fit horizontal inter-eye disparity without depending on absolute gaze."""
    selected = [sample for sample in samples if sample["phase"] == "convergence"]
    if len(selected) < 120:
        raise ValueError(f"Need at least 120 convergence samples, got {len(selected)}")
    left = np.asarray([sample["left_raw"] for sample in selected], dtype=np.float64)
    right = np.asarray([sample["right_raw"] for sample in selected], dtype=np.float64)
    features = stereo_pupil_features(left, right)
    target = np.asarray(
        [sample["right_target_deg"][0] - sample["left_target_deg"][0]
         for sample in selected],
        dtype=np.float64,
    )
    feature_rows, target_rows = _shifted_rows(len(selected), lag_frames)
    features = features[feature_rows]
    target = target[target_rows]

    # Sample-order blocks avoid the duplicated Windows receive timestamps caused
    # by batched ADB trace delivery in the first native-pupil recording.
    blocks = np.arange(len(target)) // 72
    holdout = blocks % 5 == 4
    training = ~holdout
    penalty = np.eye(features.shape[1], dtype=np.float64) * ridge
    penalty[0, 0] = 0.0

    def solve(mask: np.ndarray) -> np.ndarray:
        x = features[mask]
        return np.linalg.solve(x.T @ x + penalty, x.T @ target[mask])

    evaluation_coefficients = solve(training)
    held_error = np.abs(features[holdout] @ evaluation_coefficients - target[holdout])
    # Runtime coefficients use every captured row after the honest held-out
    # metric has been measured.
    final_coefficients = solve(np.ones(len(target), dtype=bool))
    prediction = features @ final_coefficients
    return {
        "model": "normalized-binocular-pupil-xyz-degree2-ridge",
        "coefficients": final_coefficients.tolist(),
        "feature_order": STEREO_FEATURE_ORDER,
        "output": "right_yaw_minus_left_yaw_deg",
        "lag_frames_during_fit": lag_frames,
        "ridge": ridge,
        "sample_count": len(target),
        "holdout_count": int(np.count_nonzero(holdout)),
        "held_out": {
            "mae_deg": float(np.mean(held_error)),
            "p95_deg": float(np.percentile(held_error, 95)),
        },
        "all_samples": {
            "mae_deg": float(np.mean(np.abs(prediction - target))),
            "p95_deg": float(np.percentile(np.abs(prediction - target), 95)),
        },
        "target_span_deg": float(np.ptp(target)),
        "target_range_deg": [float(np.min(target)), float(np.max(target))],
        "predicted_span_deg": float(np.ptp(prediction)),
    }


def apply_pupil_stereo_mapping(
    mapping: dict[str, Any],
    left_raw: tuple[float, float, float] | list[float] | np.ndarray,
    right_raw: tuple[float, float, float] | list[float] | np.ndarray,
) -> float:
    features = stereo_pupil_features(
        np.asarray(left_raw, dtype=np.float64),
        np.asarray(right_raw, dtype=np.float64),
    )
    return float(features[0] @ np.asarray(mapping["coefficients"], dtype=np.float64))


def _holdout_mask(samples: list[dict[str, Any]]) -> np.ndarray:
    timestamps = np.asarray([sample["timestamp_ns"] for sample in samples], dtype=np.int64)
    blocks = (timestamps - timestamps.min()) // 1_000_000_000
    holdout = blocks % 5 == 4
    if int(np.count_nonzero(holdout)) < max(12, len(samples) // 12):
        holdout = np.arange(len(samples)) % 5 == 4
    return holdout


def filter_recorded_pupil_samples(
    samples: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply the exact runtime pupil filter to a timestamped saved capture."""
    signal_filter = IndependentEyeFilter(
        3, median_window=3, min_cutoff_hz=1.5, beta=0.04
    )
    output: list[dict[str, Any]] = []
    previous_phase = ""
    previous_time = float("-inf")
    for source in samples:
        row = dict(source)
        phase = str(row.get("phase", ""))
        timestamp_s = float(
            row.get(
                "headset_kernel_time_s",
                int(row["timestamp_ns"]) / 1_000_000_000.0,
            )
        )
        if phase != previous_phase or timestamp_s <= previous_time:
            signal_filter.reset()
        previous_phase = phase
        previous_time = timestamp_s
        left_unfiltered = np.asarray(row["left_raw"], dtype=np.float64)
        right_unfiltered = np.asarray(row["right_raw"], dtype=np.float64)
        left_normalized = (
            left_unfiltered - PUPIL_MEAN["left"]
        ) / PUPIL_SCALE["left"]
        right_normalized = (
            right_unfiltered - PUPIL_MEAN["right"]
        ) / PUPIL_SCALE["right"]
        left_filtered, right_filtered = signal_filter.update(
            left_normalized, right_normalized, timestamp_s
        )
        row["left_raw_unfiltered"] = left_unfiltered.tolist()
        row["right_raw_unfiltered"] = right_unfiltered.tolist()
        row["left_raw"] = (
            left_filtered * PUPIL_SCALE["left"] + PUPIL_MEAN["left"]
        ).tolist()
        row["right_raw"] = (
            right_filtered * PUPIL_SCALE["right"] + PUPIL_MEAN["right"]
        ).tolist()
        output.append(row)
    return output


def _metrics(predicted: np.ndarray, expected: np.ndarray) -> dict[str, float]:
    angular = np.linalg.norm(predicted - expected, axis=1)
    absolute = np.abs(predicted - expected)
    return {
        "yaw_mae_deg": float(np.mean(absolute[:, 0])),
        "pitch_mae_deg": float(np.mean(absolute[:, 1])),
        "angular_mae_deg": float(np.mean(angular)),
        "angular_p95_deg": float(np.percentile(angular, 95)),
    }


def fit_pupil_eye_mapping(
    samples: list[dict[str, Any]], eye: str
) -> tuple[dict[str, Any], np.ndarray]:
    if eye not in ("left", "right"):
        raise ValueError("eye must be left or right")
    if len(samples) < 120:
        raise ValueError(f"Need at least 120 pupil samples, got {len(samples)}")
    raw = np.asarray([sample[f"{eye}_raw"] for sample in samples], dtype=np.float64)
    normalized_raw = (raw - PUPIL_MEAN[eye]) / PUPIL_SCALE[eye]
    target = np.asarray(
        [sample[f"{eye}_target_deg"] for sample in samples], dtype=np.float64
    )
    features = pupil_features(raw, eye)
    holdout = _holdout_mask(samples)
    training = ~holdout
    penalty = np.eye(features.shape[1], dtype=np.float64) * 0.02
    penalty[0, 0] = 0.0

    def fit(indices: np.ndarray) -> np.ndarray:
        selected = features[indices]
        return np.linalg.solve(
            selected.T @ selected + penalty,
            selected.T @ target[indices],
        )

    evaluation_coefficients = fit(training)
    training_error = np.linalg.norm(
        features[training] @ evaluation_coefficients - target[training], axis=1
    )
    median = float(np.median(training_error))
    mad = float(np.median(np.abs(training_error - median)))
    cutoff = max(2.0, median + 4.0 * max(mad, 0.25))
    clean_training = np.flatnonzero(training)[training_error <= cutoff]
    if len(clean_training) >= 96:
        evaluation_coefficients = fit(clean_training)

    evaluation_prediction = features @ evaluation_coefficients
    preliminary_coefficients = fit(np.ones(len(samples), dtype=bool))
    preliminary_prediction = features @ preliminary_coefficients
    all_error = np.linalg.norm(preliminary_prediction - target, axis=1)
    all_median = float(np.median(all_error))
    all_mad = float(np.median(np.abs(all_error - all_median)))
    clean = all_error <= max(2.0, all_median + 4.0 * max(all_mad, 0.25))
    if int(np.count_nonzero(clean)) >= 120:
        final_coefficients = fit(clean)
    else:
        final_coefficients = preliminary_coefficients
    final_prediction = features @ final_coefficients

    center = np.mean(normalized_raw, axis=0)
    covariance = np.cov(normalized_raw, rowvar=False) + np.eye(3) * 1e-4
    inverse_covariance = np.linalg.inv(covariance)
    centered = normalized_raw - center
    distance_squared = np.einsum(
        "ni,ij,nj->n", centered, inverse_covariance, centered
    )

    return {
        "eye": eye,
        "model": "normalized-pupil-xyz-degree2-ridge",
        "coefficients": final_coefficients.tolist(),
        "feature_order": FEATURE_ORDER,
        "output_order": ["yaw_deg", "pitch_deg"],
        "pupil_mean": PUPIL_MEAN[eye].tolist(),
        "pupil_scale": PUPIL_SCALE[eye].tolist(),
        "normalized_bounds": {
            "low": np.percentile(normalized_raw, 0.5, axis=0).tolist(),
            "high": np.percentile(normalized_raw, 99.5, axis=0).tolist(),
        },
        "joint_domain": {
            "center": center.tolist(),
            "inverse_covariance": inverse_covariance.tolist(),
            "max_distance_squared": float(np.percentile(distance_squared, 99.5)),
        },
        "sample_count": len(samples),
        "retained_count": int(np.count_nonzero(clean)),
        "holdout_count": int(np.count_nonzero(holdout)),
        "held_out": _metrics(
            evaluation_prediction[holdout], target[holdout]
        ),
        "all_samples": _metrics(final_prediction, target),
    }, evaluation_prediction


def fit_safe_pupil_eye_mapping(
    samples: list[dict[str, Any]], eye: str
) -> tuple[dict[str, Any], np.ndarray]:
    """Fit bounded, separable yaw/pitch curves with an honest held-out score."""
    if eye not in ("left", "right"):
        raise ValueError("eye must be left or right")
    if len(samples) < 120:
        raise ValueError(f"Need at least 120 pupil samples, got {len(samples)}")
    raw = np.asarray([sample[f"{eye}_raw"] for sample in samples], dtype=np.float64)
    normalized = (raw - PUPIL_MEAN[eye]) / PUPIL_SCALE[eye]
    target = np.asarray(
        [sample[f"{eye}_target_deg"] for sample in samples], dtype=np.float64
    )
    yaw_features = np.column_stack(
        [np.ones(len(samples)), normalized[:, 0], normalized[:, 0] ** 2]
    )
    pitch_features = np.column_stack(
        [np.ones(len(samples)), normalized[:, 1], normalized[:, 1] ** 2]
    )
    holdout = _holdout_mask(samples)
    training = ~holdout
    penalty = np.diag([0.0, 0.02, 0.02])

    def robust_fit(features: np.ndarray, values: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, int]:
        rows = np.flatnonzero(mask)
        selected = features[rows]
        coefficients = np.linalg.solve(
            selected.T @ selected + penalty, selected.T @ values[rows]
        )
        error = np.abs(selected @ coefficients - values[rows])
        median = float(np.median(error))
        mad = float(np.median(np.abs(error - median)))
        keep = error <= max(2.0, median + 4.0 * max(mad, 0.25))
        if int(np.count_nonzero(keep)) >= 96:
            selected = selected[keep]
            rows = rows[keep]
            coefficients = np.linalg.solve(
                selected.T @ selected + penalty, selected.T @ values[rows]
            )
        return coefficients, len(rows)

    evaluation_yaw, _ = robust_fit(yaw_features, target[:, 0], training)
    evaluation_pitch, _ = robust_fit(pitch_features, target[:, 1], training)
    evaluation_prediction = np.column_stack(
        [yaw_features @ evaluation_yaw, pitch_features @ evaluation_pitch]
    )
    final_yaw, yaw_retained = robust_fit(
        yaw_features, target[:, 0], np.ones(len(samples), dtype=bool)
    )
    final_pitch, pitch_retained = robust_fit(
        pitch_features, target[:, 1], np.ones(len(samples), dtype=bool)
    )
    final_prediction = np.column_stack(
        [yaw_features @ final_yaw, pitch_features @ final_pitch]
    )

    center = np.mean(normalized, axis=0)
    covariance = np.cov(normalized, rowvar=False) + np.eye(3) * 1e-4
    inverse_covariance = np.linalg.inv(covariance)
    centered = normalized - center
    distance_squared = np.einsum(
        "ni,ij,nj->n", centered, inverse_covariance, centered
    )
    return {
        "eye": eye,
        "model": "normalized-pupil-separable-degree2-ridge",
        "yaw_coefficients": final_yaw.tolist(),
        "pitch_coefficients": final_pitch.tolist(),
        "feature_order": SEPARABLE_FEATURE_ORDER,
        "output_order": ["yaw_deg", "pitch_deg"],
        "pupil_mean": PUPIL_MEAN[eye].tolist(),
        "pupil_scale": PUPIL_SCALE[eye].tolist(),
        "normalized_bounds": {
            "low": np.percentile(normalized, 0.5, axis=0).tolist(),
            "high": np.percentile(normalized, 99.5, axis=0).tolist(),
        },
        "joint_domain": {
            "center": center.tolist(),
            "inverse_covariance": inverse_covariance.tolist(),
            "max_distance_squared": float(np.percentile(distance_squared, 99.5)),
        },
        "sample_count": len(samples),
        "retained_count": min(yaw_retained, pitch_retained),
        "holdout_count": int(np.count_nonzero(holdout)),
        "held_out": _metrics(
            evaluation_prediction[holdout], target[holdout]
        ),
        "all_samples": _metrics(final_prediction, target),
    }, evaluation_prediction


def apply_pupil_eye_mapping(
    mapping: dict[str, Any], raw: tuple[float, float, float] | list[float] | np.ndarray
) -> tuple[float, float]:
    eye = str(mapping["eye"])
    raw_array = np.asarray(raw, dtype=np.float64)
    bounds = mapping.get("normalized_bounds")
    if isinstance(bounds, dict):
        normalized = (raw_array - PUPIL_MEAN[eye]) / PUPIL_SCALE[eye]
        low = np.asarray(bounds["low"], dtype=np.float64) - 0.35
        high = np.asarray(bounds["high"], dtype=np.float64) + 0.35
        raw_array = np.clip(normalized, low, high) * PUPIL_SCALE[eye] + PUPIL_MEAN[eye]
    if mapping.get("model") == "normalized-pupil-separable-degree2-ridge":
        normalized = (raw_array - PUPIL_MEAN[eye]) / PUPIL_SCALE[eye]
        yaw_features = np.asarray([1.0, normalized[0], normalized[0] ** 2])
        pitch_features = np.asarray([1.0, normalized[1], normalized[1] ** 2])
        output = np.asarray(
            [
                yaw_features @ np.asarray(mapping["yaw_coefficients"]),
                pitch_features @ np.asarray(mapping["pitch_coefficients"]),
            ],
            dtype=np.float64,
        )
    else:
        output = pupil_features(raw_array, eye)[0] @ np.asarray(
            mapping["coefficients"], dtype=np.float64
        )
    return float(output[0]), float(output[1])


def pupil_eye_in_calibrated_range(
    mapping: dict[str, Any],
    raw: tuple[float, float, float] | list[float] | np.ndarray,
) -> bool:
    bounds = mapping.get("normalized_bounds")
    if not isinstance(bounds, dict):
        return True
    eye = str(mapping["eye"])
    normalized = (np.asarray(raw, dtype=np.float64) - PUPIL_MEAN[eye]) / PUPIL_SCALE[eye]
    low = np.asarray(bounds["low"], dtype=np.float64) - 0.35
    high = np.asarray(bounds["high"], dtype=np.float64) + 0.35
    if not bool(np.all((normalized >= low) & (normalized <= high))):
        return False
    joint = mapping.get("joint_domain")
    if isinstance(joint, dict):
        centered = normalized - np.asarray(joint["center"], dtype=np.float64)
        distance_squared = float(
            centered @ np.asarray(joint["inverse_covariance"], dtype=np.float64) @ centered
        )
        if distance_squared > float(joint["max_distance_squared"]) * 1.15:
            return False
    return True


def fit_direction_aware_convergence_mapping(
    samples: list[dict[str, Any]],
    left_evaluation_prediction: np.ndarray,
    right_evaluation_prediction: np.ndarray,
    *,
    left_runtime_prediction: np.ndarray | None = None,
    right_runtime_prediction: np.ndarray | None = None,
    convergence_weight: float = 8.0,
    ridge: float = 0.1,
) -> dict[str, Any]:
    """Fit convergence separately from the two independently mapped gaze rays.

    The cross-eye model is allowed to estimate only vergence. It is never fed
    back into either eye's yaw or pitch, preserving unilateral eye motion.
    """
    evaluation_design = ray_stereo_features(
        left_evaluation_prediction, right_evaluation_prediction
    )
    if left_runtime_prediction is None:
        left_runtime_prediction = left_evaluation_prediction
    if right_runtime_prediction is None:
        right_runtime_prediction = right_evaluation_prediction
    runtime_design = ray_stereo_features(
        left_runtime_prediction, right_runtime_prediction
    )
    expected = np.asarray(
        [
            sample["right_target_deg"][0] - sample["left_target_deg"][0]
            for sample in samples
        ],
        dtype=np.float64,
    )
    phases = np.asarray([sample["phase"] for sample in samples])
    holdout = _holdout_mask(samples)
    training = ~holdout
    weights = np.ones(len(samples), dtype=np.float64)
    weights[phases == "convergence"] = convergence_weight
    penalty = np.eye(evaluation_design.shape[1], dtype=np.float64) * ridge
    penalty[0, 0] = 0.0

    def solve(design: np.ndarray, mask: np.ndarray) -> np.ndarray:
        x = design[mask]
        selected_weights = weights[mask]
        return np.linalg.solve(
            x.T @ (x * selected_weights[:, None]) + penalty,
            x.T @ (expected[mask] * selected_weights),
        )

    evaluation_coefficients = solve(evaluation_design, training)
    evaluation_prediction = evaluation_design @ evaluation_coefficients
    final_coefficients = solve(runtime_design, np.ones(len(samples), dtype=bool))
    final_prediction = runtime_design @ final_coefficients
    input_values = runtime_design[:, 1:4]
    center = np.mean(input_values, axis=0)
    covariance = np.cov(input_values, rowvar=False) + np.eye(3) * 1e-3
    inverse_covariance = np.linalg.inv(covariance)
    centered = input_values - center
    distance_squared = np.einsum(
        "ni,ij,nj->n", centered, inverse_covariance, centered
    )

    evaluations: dict[str, Any] = {}
    for phase in ("gaze", "convergence", "all"):
        selected = holdout.copy()
        if phase != "all":
            selected &= phases == phase
        error = np.abs(evaluation_prediction[selected] - expected[selected])
        evaluations[phase] = {
            "sample_count": int(np.count_nonzero(selected)),
            "mae_deg": float(np.mean(error)),
            "p95_deg": float(np.percentile(error, 95)),
            "target_range_deg": [
                float(np.min(expected[selected])), float(np.max(expected[selected]))
            ],
            "predicted_range_deg": [
                float(np.min(evaluation_prediction[selected])),
                float(np.max(evaluation_prediction[selected])),
            ],
        }
    return {
        "model": "direction-aware-independent-ray-convergence-degree2-ridge",
        "purpose": "convergence_only_never_modifies_individual_eye_rays",
        "coefficients": final_coefficients.tolist(),
        "feature_order": RAY_STEREO_FEATURE_ORDER,
        "output": "positive_inward_vergence_deg",
        "convergence_weight": convergence_weight,
        "ridge": ridge,
        "sample_count": len(samples),
        "held_out": evaluations,
        "input_domain": {
            "order": ["raw_disparity", "common_yaw", "common_pitch"],
            "low": np.percentile(input_values, 0.5, axis=0).tolist(),
            "high": np.percentile(input_values, 99.5, axis=0).tolist(),
            "center": center.tolist(),
            "inverse_covariance": inverse_covariance.tolist(),
            "max_distance_squared": float(np.percentile(distance_squared, 99.5)),
        },
        "all_samples": {
            "mae_deg": float(np.mean(np.abs(final_prediction - expected))),
            "p95_deg": float(np.percentile(np.abs(final_prediction - expected), 95)),
            "target_range_deg": [float(np.min(expected)), float(np.max(expected))],
            "predicted_range_deg": [
                float(np.min(final_prediction)), float(np.max(final_prediction))
            ],
        },
    }


def apply_direction_aware_convergence_mapping(
    mapping: dict[str, Any],
    left_gaze_deg: tuple[float, float] | list[float] | np.ndarray,
    right_gaze_deg: tuple[float, float] | list[float] | np.ndarray,
) -> tuple[float, bool]:
    design = ray_stereo_features(
        np.asarray(left_gaze_deg, dtype=np.float64),
        np.asarray(right_gaze_deg, dtype=np.float64),
    )
    value = float(design[0] @ np.asarray(mapping["coefficients"], dtype=np.float64))
    domain = mapping.get("input_domain")
    if not isinstance(domain, dict):
        return value, True
    inputs = design[0, 1:4]
    low = np.asarray(domain["low"], dtype=np.float64) - np.asarray([0.5, 2.0, 2.0])
    high = np.asarray(domain["high"], dtype=np.float64) + np.asarray([0.5, 2.0, 2.0])
    in_range = bool(np.all((inputs >= low) & (inputs <= high)))
    centered = inputs - np.asarray(domain["center"], dtype=np.float64)
    distance_squared = float(
        centered @ np.asarray(domain["inverse_covariance"], dtype=np.float64) @ centered
    )
    in_range &= distance_squared <= float(domain["max_distance_squared"]) * 1.25
    return value, in_range


def _independent_convergence_metrics(
    samples: list[dict[str, Any]],
    left_prediction: np.ndarray,
    right_prediction: np.ndarray,
) -> dict[str, Any]:
    convergence = np.asarray(
        [sample["phase"] == "convergence" for sample in samples], dtype=bool
    )
    holdout = _holdout_mask(samples) & convergence
    if not np.any(holdout):
        holdout = convergence
    expected = np.asarray(
        [
            sample["right_target_deg"][0] - sample["left_target_deg"][0]
            for sample in samples
        ],
        dtype=np.float64,
    )
    predicted = right_prediction[:, 0] - left_prediction[:, 0]
    errors = np.abs(predicted[holdout] - expected[holdout])
    selected_expected = expected[convergence]
    selected_prediction = predicted[convergence]
    return {
        "model": "difference-of-independent-per-eye-rays",
        "cross_eye_inputs": False,
        "sample_count": int(np.count_nonzero(convergence)),
        "holdout_count": int(np.count_nonzero(holdout)),
        "held_out": {
            "mae_deg": float(np.mean(errors)),
            "p95_deg": float(np.percentile(errors, 95)),
        },
        "target_span_deg": float(np.ptp(selected_expected)),
        "target_range_deg": [
            float(np.min(selected_expected)),
            float(np.max(selected_expected)),
        ],
        "predicted_span_deg": float(np.ptp(selected_prediction)),
    }


def build_meta_teacher_samples(
    samples: list[dict[str, Any]],
    *,
    minimum_samples: int = 120,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Distill Meta's personalized common direction into isolated eye mappings.

    Meta supplies only the common-direction teacher. The VR target supplies the
    left/right geometric offset, so the teacher never pretends Meta contains
    independent convergence. Runtime remains pupil-only.
    """
    valid = np.asarray(
        [
            isinstance(sample.get("factory"), dict)
            and bool(sample["factory"].get("left_valid", False))
            and bool(sample["factory"].get("right_valid", False))
            for sample in samples
        ],
        dtype=bool,
    )
    if int(np.count_nonzero(valid)) < minimum_samples:
        raise ValueError(
            f"Need at least {minimum_samples} valid Meta teacher samples, "
            f"got {int(np.count_nonzero(valid))}"
        )
    meta_common = np.full((len(samples), 2), np.nan, dtype=np.float64)
    meta_difference = np.full((len(samples), 2), np.nan, dtype=np.float64)
    for index, sample in enumerate(samples):
        if not valid[index]:
            continue
        factory = sample["factory"]
        left = np.asarray(
            quaternion_yaw_pitch(factory["left_orientation"]), dtype=np.float64
        )
        right = np.asarray(
            quaternion_yaw_pitch(factory["right_orientation"]), dtype=np.float64
        )
        meta_common[index] = (left + right) * 0.5
        meta_difference[index] = right - left

    phases = np.asarray([str(sample["phase"]) for sample in samples])
    best: tuple[float, int, list[dict[str, Any]], dict[str, Any], dict[str, Any]] | None = None
    for lag_frames in range(-12, 13):
        teacher_samples: list[dict[str, Any]] = []
        for pupil_index, sample in enumerate(samples):
            meta_index = pupil_index + lag_frames
            if meta_index < 0 or meta_index >= len(samples):
                continue
            if phases[meta_index] != phases[pupil_index] or not valid[meta_index]:
                continue
            left_target = np.asarray(sample["left_target_deg"], dtype=np.float64)
            right_target = np.asarray(sample["right_target_deg"], dtype=np.float64)
            geometric_common = (left_target + right_target) * 0.5
            row = dict(sample)
            row["geometric_left_target_deg"] = left_target.tolist()
            row["geometric_right_target_deg"] = right_target.tolist()
            row["meta_common_target_deg"] = meta_common[meta_index].tolist()
            row["meta_teacher_lag_frames"] = lag_frames
            row["left_target_deg"] = (
                meta_common[meta_index] + left_target - geometric_common
            ).tolist()
            row["right_target_deg"] = (
                meta_common[meta_index] + right_target - geometric_common
            ).tolist()
            teacher_samples.append(row)
        if len(teacher_samples) < minimum_samples:
            continue
        left, _ = fit_pupil_eye_mapping(teacher_samples, "left")
        right, _ = fit_pupil_eye_mapping(teacher_samples, "right")
        score = (
            left["held_out"]["angular_mae_deg"]
            + right["held_out"]["angular_mae_deg"]
            + 0.1 * left["held_out"]["angular_p95_deg"]
            + 0.1 * right["held_out"]["angular_p95_deg"]
        )
        candidate = (score, lag_frames, teacher_samples, left, right)
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        raise ValueError("Meta teacher samples had no phase-aligned timing overlap")
    _score, lag_frames, teacher_samples, left, right = best
    usable_difference = meta_difference[valid]
    alignment_values = np.asarray(
        [
            float(sample["factory"].get("alignment_ms", 0.0))
            for sample in samples if isinstance(sample.get("factory"), dict)
        ],
        dtype=np.float64,
    )
    diagnostics = {
        "mode": "meta_common_direction_plus_vr_per_eye_geometric_offset",
        "runtime_meta_required": False,
        "valid_sample_count": int(np.count_nonzero(valid)),
        "selected_lag_frames": lag_frames,
        "selected_lag_ms_at_72_hz": lag_frames * 1000.0 / 72.0,
        "factory_alignment_ms": {
            "median": float(np.median(alignment_values)),
            "p95_absolute": float(np.percentile(np.abs(alignment_values), 95)),
        },
        "meta_right_minus_left_std_deg": np.std(
            usable_difference, axis=0
        ).tolist(),
        "candidate_held_out": {
            "left": left["held_out"],
            "right": right["held_out"],
        },
    }
    return teacher_samples, diagnostics


def fit_pupil_calibration(samples: list[dict[str, Any]]) -> dict[str, Any]:
    meta_teacher = None
    fitting_samples = samples
    if sum(
        isinstance(sample.get("factory"), dict)
        and bool(sample["factory"].get("left_valid", False))
        and bool(sample["factory"].get("right_valid", False))
        for sample in samples
    ) >= 120:
        fitting_samples, meta_teacher = build_meta_teacher_samples(samples)
    left, left_prediction = fit_pupil_eye_mapping(fitting_samples, "left")
    right, right_prediction = fit_pupil_eye_mapping(fitting_samples, "right")
    left_runtime_prediction = np.asarray(
        [
            apply_pupil_eye_mapping(left, sample["left_raw"])
            for sample in fitting_samples
        ],
        dtype=np.float64,
    )
    right_runtime_prediction = np.asarray(
        [
            apply_pupil_eye_mapping(right, sample["right_raw"])
            for sample in fitting_samples
        ],
        dtype=np.float64,
    )
    ray_difference = _independent_convergence_metrics(
        fitting_samples, left_prediction, right_prediction
    )
    convergence = fit_direction_aware_convergence_mapping(
        fitting_samples,
        left_prediction,
        right_prediction,
        left_runtime_prediction=left_runtime_prediction,
        right_runtime_prediction=right_runtime_prediction,
    )
    convergence_samples = [
        sample for sample in samples if sample["phase"] == "convergence"
    ]
    convergence_common_yaw = np.asarray(
        [
            (sample["left_target_deg"][0] + sample["right_target_deg"][0]) * 0.5
            for sample in convergence_samples
        ],
        dtype=np.float64,
    )
    convergence_common_pitch = np.asarray(
        [
            (sample["left_target_deg"][1] + sample["right_target_deg"][1]) * 0.5
            for sample in convergence_samples
        ],
        dtype=np.float64,
    )
    convergence_direction_coverage = {
        "yaw_span_deg": float(np.ptp(convergence_common_yaw)),
        "pitch_span_deg": float(np.ptp(convergence_common_pitch)),
    }
    direction_coverage_pass = bool(
        convergence_direction_coverage["yaw_span_deg"] >= 30.0
        and convergence_direction_coverage["pitch_span_deg"] >= 20.0
    )
    gaze_pass = bool(
        left["held_out"]["angular_mae_deg"] <= 5.0
        and right["held_out"]["angular_mae_deg"] <= 5.0
        and left["held_out"]["angular_p95_deg"] <= 10.0
        and right["held_out"]["angular_p95_deg"] <= 10.0
    )
    convergence_holdout = convergence["held_out"]["convergence"]
    fixed_depth_holdout = convergence["held_out"]["gaze"]
    convergence_pass = bool(
        convergence_holdout["mae_deg"] <= 1.5
        and convergence_holdout["p95_deg"] <= 3.0
        and fixed_depth_holdout["mae_deg"] <= 1.5
        and fixed_depth_holdout["p95_deg"] <= 3.0
        and direction_coverage_pass
    )
    return {
        "format": (
            "qpro-independent-neural-pupil-calibration-v7"
            if meta_teacher is not None
            else "qpro-independent-neural-pupil-calibration-v6"
        ),
        "created_unix_ns": time.time_ns(),
        "source": "Quest Pro Seacliff_V1_5 pupil tensor + synchronized BabbleCalibration labels",
        "target_convention": "Baballonia capture convention: overlay yaw negated, pitch unchanged",
        "sample_count": len(samples),
        "fitting_sample_count": len(fitting_samples),
        "phases": {
            name: sum(sample["phase"] == name for sample in samples)
            for name in ("gaze", "convergence")
        },
        "left": left,
        "right": right,
        "stereo": convergence,
        "convergence": convergence,
        "raw_independent_ray_difference_diagnostic": ray_difference,
        "convergence_direction_coverage": convergence_direction_coverage,
        "meta_teacher": meta_teacher,
        "runtime_filter": {
            "type": "per-eye median-3 plus OneEuro",
            "training_samples_filtered": True,
            "median_window": 3,
            "min_cutoff_hz": 1.5,
            "beta": 0.04,
            "derivative_cutoff_hz": 1.0,
        },
        "quality_gate": {
            "status": "pass" if gaze_pass and convergence_pass else (
                "partial_pass" if gaze_pass or convergence_pass else "fail"
            ),
            "gaze_pass": gaze_pass,
            "convergence_pass": convergence_pass,
            "convergence_direction_coverage_pass": direction_coverage_pass,
        },
    }


class NativePupilCalibrationController(StereoEyeCalibrationController):
    """Reuse BabbleCalibration while recording the native pupil tensors."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sample_filter = IndependentEyeFilter(
            3, median_window=3, min_cutoff_hz=1.5, beta=0.04
        )
        self._filter_phase = ""

    def add_pupil_sample(
        self,
        left_raw: tuple[float, float, float],
        right_raw: tuple[float, float, float],
        timestamp_ns: int,
        *,
        kernel_time_s: float | None = None,
        arrival_monotonic_ns: int | None = None,
        factory_sample: dict[str, object] | None = None,
    ) -> None:
        with self._lock:
            phase = self.phase
        target = self.target_at(timestamp_ns)
        if phase not in ("gaze", "convergence") or target is None:
            return
        if phase != self._filter_phase:
            self._sample_filter.reset()
            self._filter_phase = phase
        left_unfiltered = np.asarray(left_raw, dtype=np.float64)
        right_unfiltered = np.asarray(right_raw, dtype=np.float64)
        left_normalized = (
            left_unfiltered - PUPIL_MEAN["left"]
        ) / PUPIL_SCALE["left"]
        right_normalized = (
            right_unfiltered - PUPIL_MEAN["right"]
        ) / PUPIL_SCALE["right"]
        filter_time_s = (
            float(kernel_time_s)
            if kernel_time_s is not None
            else int(timestamp_ns) / 1_000_000_000.0
        )
        left_filtered, right_filtered = self._sample_filter.update(
            left_normalized, right_normalized, filter_time_s
        )
        left_filtered = left_filtered * PUPIL_SCALE["left"] + PUPIL_MEAN["left"]
        right_filtered = right_filtered * PUPIL_SCALE["right"] + PUPIL_MEAN["right"]
        sample = {
            "timestamp_ns": int(timestamp_ns),
            "phase": phase,
            "distance_m": target.distance,
            "left_raw": left_filtered.tolist(),
            "right_raw": right_filtered.tolist(),
            "left_raw_unfiltered": left_unfiltered.tolist(),
            "right_raw_unfiltered": right_unfiltered.tolist(),
            # Baballonia's own FrameCollector negates the Godot overlay yaw
            # before using it as a model target. Preserve that convention.
            "left_target_deg": [-target.left_yaw, target.left_pitch],
            "right_target_deg": [-target.right_yaw, target.right_pitch],
        }
        if kernel_time_s is not None:
            sample["headset_kernel_time_s"] = float(kernel_time_s)
        if arrival_monotonic_ns is not None:
            sample["arrival_monotonic_ns"] = int(arrival_monotonic_ns)
            sample["usb_delivery_delay_ms"] = (
                int(arrival_monotonic_ns) - int(timestamp_ns)
            ) / 1_000_000.0
        if factory_sample is not None:
            factory_arrival_ns = int(factory_sample["arrivalMonotonicNs"])
            sample["factory"] = {
                "alignment_ms": (factory_arrival_ns - timestamp_ns) / 1_000_000.0,
                "source_sequence": int(factory_sample["sourceSequence"]),
                "source_change_sequence": int(
                    factory_sample.get("sourceChangeSequence", 0)
                ),
                "source_unchanged_ms": float(
                    factory_sample.get("sourceUnchangedMs", -1.0)
                ),
                "left_valid": bool(factory_sample.get("leftEyeIsValid", False)),
                "right_valid": bool(factory_sample.get("rightEyeIsValid", False)),
                "left_confidence": float(
                    factory_sample.get("leftEyeConfidence", 0.0)
                ),
                "right_confidence": float(
                    factory_sample.get("rightEyeConfidence", 0.0)
                ),
                "left_orientation": list(
                    factory_sample.get("leftEyeOrientation", [0.0] * 4)
                ),
                "right_orientation": list(
                    factory_sample.get("rightEyeOrientation", [0.0] * 4)
                ),
            }
        with self._lock:
            self.samples.append(sample)

    def _fit_and_save(self) -> None:
        with self._lock:
            self.phase = "fitting"
            self.message = "Fitting independent native pupil-to-gaze mappings..."
            samples = list(self.samples)
        try:
            result = fit_pupil_calibration(samples)
            sample_path = self.output_path.with_suffix(".samples.jsonl")
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            with sample_path.open("w", encoding="utf-8") as output:
                for sample in samples:
                    output.write(json.dumps(sample, separators=(",", ":")) + "\n")
            result["sample_path"] = str(sample_path)
            self.output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            left = result["left"]["held_out"]
            right = result["right"]["held_out"]
            convergence = result["convergence"]
            convergence_score = convergence["held_out"]["convergence"]
            coverage = result["convergence_direction_coverage"]
            with self._lock:
                self.result = result
                self.phase = "done"
                self.message = (
                    f"Saved ({result['quality_gate']['status']}). Gaze MAE "
                    f"L {left['angular_mae_deg']:.1f} deg, R {right['angular_mae_deg']:.1f} deg; "
                    f"convergence {convergence_score['mae_deg']:.2f} deg; "
                    f"coverage {coverage['yaw_span_deg']:.0f}x{coverage['pitch_span_deg']:.0f} deg. Press Q."
                )
            try:
                self._send_routine("close", 1)
            except OSError:
                pass
        except Exception as error:
            with self._lock:
                self.phase = "error"
                self.message = f"Native pupil calibration failed: {error}"

    def status(self) -> tuple[str, str, int]:
        with self._lock:
            return self.phase, self.message, len(self.samples)


__all__ = [
    "NativePupilCalibrationController",
    "apply_direction_aware_convergence_mapping",
    "apply_pupil_eye_mapping",
    "apply_pupil_stereo_mapping",
    "fit_pupil_calibration",
    "fit_direction_aware_convergence_mapping",
    "fit_pupil_eye_mapping",
    "fit_safe_pupil_eye_mapping",
    "fit_pupil_stereo_mapping",
    "filter_recorded_pupil_samples",
    "pupil_eye_in_calibrated_range",
]
