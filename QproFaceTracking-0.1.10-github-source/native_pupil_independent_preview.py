#!/usr/bin/env python3
"""Observation-only gaze preview driven solely by independent pupil tensors."""

from __future__ import annotations

import argparse
import json
import queue
import time
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np

from eye_signal_filter import IndependentEyeFilter, OneEuroVectorFilter
from hybrid_preview import convergence_distance_m, gaze_marker_position
from native_eye_probe import put_text
from native_eye_pupil_probe import (
    LEFT_MEAN,
    LEFT_SCALE,
    RIGHT_MEAN,
    RIGHT_SCALE,
    NeuralPupilReader,
)
from pupil_gaze_calibration import (
    apply_direction_aware_convergence_mapping,
    apply_pupil_eye_mapping,
    pupil_eye_in_calibrated_range,
)


@dataclass(frozen=True)
class IndependentPupilPrediction:
    left_gaze_deg: tuple[float, float]
    right_gaze_deg: tuple[float, float]
    disparity_deg: float
    raw_ray_disparity_deg: float
    estimated_distance_m: float | None
    in_calibrated_range: tuple[bool, bool]
    convergence_in_calibrated_range: bool
    fixation_status: str
    calibration_status: str


class IndependentPupilGazeModel:
    """Two isolated pupil-to-ray mappings with no binocular correction path."""

    def __init__(self, calibration_path: str | Path) -> None:
        document = json.loads(Path(calibration_path).read_text(encoding="utf-8"))
        self.format = str(document.get("format", ""))
        if self.format not in (
            "qpro-independent-neural-pupil-calibration-v5",
            "qpro-independent-neural-pupil-calibration-v6",
            "qpro-independent-neural-pupil-calibration-v7",
        ):
            raise ValueError(
                "Independent preview requires a filter-matched v5 or v6 pupil calibration"
            )
        self.left = document["left"]
        self.right = document["right"]
        self.quality = document.get("quality_gate", {})
        self.calibration_status = str(self.quality.get("status", "unknown"))
        self.depth_available = bool(self.quality.get("convergence_pass", False))
        self.convergence = (
            document.get("convergence")
            if self.format.endswith(("v6", "v7")) else None
        )

    @staticmethod
    def depth_from_disparity(disparity: float, ipd_m: float) -> tuple[float | None, str]:
        # Baballonia's capture convention yields negative disparity for inward
        # vergence. Never turn divergent rays into a plausible distance via abs().
        if disparity >= -0.25:
            return None, "diverging / no valid fixation"
        distance = convergence_distance_m(disparity, ipd_m)
        if distance is None:
            return None, "far / unresolved"
        return distance, "geometric estimate"

    @staticmethod
    def depth_from_convergence(convergence: float, ipd_m: float) -> tuple[float | None, str]:
        if convergence <= 0.25:
            return None, "far / unresolved"
        distance = convergence_distance_m(convergence, ipd_m)
        if distance is None:
            return None, "far / unresolved"
        return distance, "experimental binocular estimate"

    def predict(
        self,
        left_pupil: np.ndarray,
        right_pupil: np.ndarray,
        ipd_m: float = 0.065,
    ) -> IndependentPupilPrediction:
        left_gaze = apply_pupil_eye_mapping(self.left, left_pupil)
        right_gaze = apply_pupil_eye_mapping(self.right, right_pupil)
        raw_disparity = right_gaze[0] - left_gaze[0]
        convergence_in_range = False
        if self.depth_available and isinstance(self.convergence, dict):
            disparity, convergence_in_range = apply_direction_aware_convergence_mapping(
                self.convergence, left_gaze, right_gaze
            )
            if convergence_in_range:
                distance, fixation_status = self.depth_from_convergence(disparity, ipd_m)
            else:
                distance, fixation_status = None, "outside calibrated direction/depth volume"
        else:
            disparity = raw_disparity
            distance, fixation_status = None, "disabled - off-axis convergence calibration required"
        return IndependentPupilPrediction(
            left_gaze_deg=left_gaze,
            right_gaze_deg=right_gaze,
            disparity_deg=disparity,
            raw_ray_disparity_deg=raw_disparity,
            estimated_distance_m=distance,
            in_calibrated_range=(
                pupil_eye_in_calibrated_range(self.left, left_pupil),
                pupil_eye_in_calibrated_range(self.right, right_pupil),
            ),
            convergence_in_calibrated_range=convergence_in_range,
            fixation_status=fixation_status,
            calibration_status=self.calibration_status,
        )


def _draw_panel(
    image: np.ndarray,
    origin: tuple[int, int],
    title: str,
    gaze: tuple[float, float],
    color: tuple[int, int, int],
    in_range: bool,
) -> None:
    x, y = origin
    width, height = 430, 315
    put_text(image, title, (x, y - 15), color, 0.62, 1)
    cv2.rectangle(image, (x, y), (x + width, y + height), (70, 70, 70), 1)
    cv2.line(image, (x + width // 2, y), (x + width // 2, y + height), (45, 45, 45), 1)
    cv2.line(image, (x, y + height // 2), (x + width, y + height // 2), (45, 45, 45), 1)
    cv2.circle(
        image,
        gaze_marker_position(gaze[0], gaze[1], x, y, width, height),
        12,
        color,
        3,
        cv2.LINE_AA,
    )
    put_text(
        image,
        f"yaw {gaze[0]:+.1f}  pitch {gaze[1]:+.1f} deg",
        (x + 12, y + height - 18),
        color,
        0.49,
        1,
    )
    if not in_range:
        put_text(image, "PUPIL OUTSIDE TRAINED RANGE", (x + 12, y + 28), (40, 175, 255), 0.43, 1)


def render(prediction: IndependentPupilPrediction | None, rate_hz: float) -> np.ndarray:
    image = np.zeros((720, 1120, 3), dtype=np.uint8)
    put_text(image, "Quest Pro independent pupil-gaze preview", (22, 42), (245, 245, 245), 0.82, 2)
    put_text(image, "OBSERVATION ONLY - no Meta gaze, OSC, or VRCFT output", (22, 76), (40, 175, 255), 0.53, 1)
    put_text(image, "Separate pupil mapping/filtering per eye; binocular convergence is estimated afterward.",
             (22, 105), (175, 175, 175), 0.47, 1)
    if prediction is None:
        put_text(image, "Waiting for independent pupil samples...", (335, 345), (50, 175, 255), 0.62, 1)
        return image
    _draw_panel(image, (22, 155), "LEFT INDEPENDENT GAZE", prediction.left_gaze_deg,
                (255, 210, 45), prediction.in_calibrated_range[0])
    _draw_panel(image, (472, 155), "RIGHT INDEPENDENT GAZE", prediction.right_gaze_deg,
                (245, 70, 235), prediction.in_calibrated_range[1])
    put_text(image, "BINOCULAR CONVERGENCE", (925, 155), (230, 230, 230), 0.48, 1)
    put_text(image, f"calibrated {prediction.disparity_deg:+.2f} deg", (925, 205),
             (90, 235, 120), 0.45, 1)
    put_text(image, f"raw ray difference {prediction.raw_ray_disparity_deg:+.2f} deg", (925, 225),
             (145, 145, 145), 0.35, 1)
    distance = prediction.fixation_status if prediction.estimated_distance_m is None else f"{prediction.estimated_distance_m:.2f} m"
    depth_color = (90, 235, 120) if prediction.estimated_distance_m is not None else (40, 175, 255)
    put_text(image, f"fixation {distance}", (925, 255), depth_color, 0.36, 1)
    put_text(image, f"native pupil {rate_hz:.1f} Hz", (22, 535), (90, 235, 120), 0.50, 1)
    if prediction.calibration_status == "partial_pass":
        put_text(
            image,
            "ABSOLUTE GAZE PASSED - redo v6 calibration with off-axis near/far coverage to enable depth",
            (22, 565),
            (40, 175, 255),
            0.44,
            1,
        )
    elif prediction.calibration_status != "pass":
        put_text(
            image,
            "CALIBRATION QUALITY FAILED - gaze and convergence remain experimental",
            (22, 565),
            (40, 175, 255),
            0.44,
            1,
        )
    put_text(image, "Validation: close one eye or drift only the right eye; the other marker must remain fixed.",
             (22, 605), (220, 220, 220), 0.50, 1)
    put_text(image, "Convergence never feeds back into either eye marker; unilateral motion remains independent.",
             (22, 640), (180, 180, 180), 0.47, 1)
    put_text(image, "Q quits and removes the temporary tracepoint.", (22, 682), (150, 150, 150), 0.45)
    return image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", required=True)
    parser.add_argument("--calibration", required=True)
    parser.add_argument("--headless-seconds", type=float, default=0.0)
    arguments = parser.parse_args()

    model = IndependentPupilGazeModel(arguments.calibration)
    reader = NeuralPupilReader(arguments.adb)
    eye_filter = IndependentEyeFilter(3, min_cutoff_hz=1.5, beta=0.04)
    left_gaze_filter = OneEuroVectorFilter(2, min_cutoff_hz=0.8, beta=0.025)
    right_gaze_filter = OneEuroVectorFilter(2, min_cutoff_hz=0.8, beta=0.025)
    depth_filter = OneEuroVectorFilter(1, min_cutoff_hz=0.35, beta=0.015)
    latest: IndependentPupilPrediction | None = None
    sample_count = 0
    rate_count = 0
    rate_hz = 0.0
    rate_started = time.monotonic()
    deadline = time.monotonic() + arguments.headless_seconds if arguments.headless_seconds > 0 else None
    window = "Quest Pro independent pupil gaze"
    reader.start()
    if deadline is None:
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window, 1120, 720)
    try:
        while deadline is None or time.monotonic() < deadline:
            got_sample = False
            while True:
                try:
                    sample = reader.samples.get_nowait()
                except queue.Empty:
                    break
                got_sample = True
                sample_count += 1
                rate_count += 1
                left_normalized = (np.asarray(sample.left_pupil_3d) - LEFT_MEAN) / LEFT_SCALE
                right_normalized = (np.asarray(sample.right_pupil_3d) - RIGHT_MEAN) / RIGHT_SCALE
                left_filtered, right_filtered = eye_filter.update(
                    left_normalized, right_normalized, sample.kernel_time_s
                )
                latest = model.predict(
                    left_filtered * LEFT_SCALE + LEFT_MEAN,
                    right_filtered * RIGHT_SCALE + RIGHT_MEAN,
                )
                left_display = left_gaze_filter.update(
                    np.asarray(latest.left_gaze_deg), sample.kernel_time_s
                )
                right_display = right_gaze_filter.update(
                    np.asarray(latest.right_gaze_deg), sample.kernel_time_s
                )
                smoothed_disparity = float(depth_filter.update(
                    np.asarray([latest.disparity_deg]), sample.kernel_time_s
                )[0])
                if model.depth_available and latest.convergence_in_calibrated_range:
                    distance, status = model.depth_from_convergence(smoothed_disparity, 0.065)
                else:
                    distance, status = None, latest.fixation_status
                latest = replace(
                    latest,
                    left_gaze_deg=(float(left_display[0]), float(left_display[1])),
                    right_gaze_deg=(float(right_display[0]), float(right_display[1])),
                    disparity_deg=smoothed_disparity,
                    estimated_distance_m=distance,
                    fixation_status=status,
                )
            if not reader.errors.empty():
                raise RuntimeError(reader.errors.get_nowait())
            now = time.monotonic()
            elapsed = now - rate_started
            if elapsed >= 1.0:
                rate_hz = rate_count / elapsed
                rate_count = 0
                rate_started = now
            if deadline is not None:
                if not got_sample:
                    time.sleep(0.005)
                continue
            cv2.imshow(window, render(latest, rate_hz))
            key = cv2.waitKeyEx(8)
            if key in (ord("q"), ord("Q"), 27):
                break
        if deadline is not None:
            if sample_count == 0 or latest is None:
                raise RuntimeError("The native pupil source produced no samples")
            print(json.dumps({"samples": sample_count, "prediction": latest.__dict__}))
        return 0
    finally:
        reader.close()
        if deadline is None:
            cv2.destroyWindow(window)


if __name__ == "__main__":
    raise SystemExit(main())
