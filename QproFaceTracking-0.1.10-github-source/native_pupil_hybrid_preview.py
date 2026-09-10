#!/usr/bin/env python3
"""Observation-only hybrid: Meta gaze midpoint plus native binocular disparity."""

from __future__ import annotations

import argparse
import json
import queue
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from eye_signal_filter import IndependentEyeFilter
from hybrid_preview import apply_factory_mapping, convergence_distance_m, gaze_marker_position
from label_capture import LabelSidecarRecorder
from native_eye_probe import put_text
from native_eye_pupil_probe import (
    LEFT_MEAN,
    LEFT_SCALE,
    RIGHT_MEAN,
    RIGHT_SCALE,
    NeuralPupilReader,
)
from pupil_gaze_calibration import apply_pupil_stereo_mapping


@dataclass(frozen=True)
class NativePupilHybridPrediction:
    left_gaze_deg: tuple[float, float] | None
    right_gaze_deg: tuple[float, float] | None
    meta_center_deg: tuple[float, float] | None
    pupil_disparity_deg: float
    estimated_distance_m: float | None
    confidence: tuple[float, float]
    stereo_in_calibrated_range: bool


class NativePupilHybridModel:
    def __init__(self, pupil_path: str | Path, factory_path: str | Path) -> None:
        pupil = json.loads(Path(pupil_path).read_text(encoding="utf-8"))
        factory_document = json.loads(Path(factory_path).read_text(encoding="utf-8"))
        if pupil.get("format") != "qpro-independent-neural-pupil-calibration-v2":
            raise ValueError("The native pupil preview requires the v2 calibration")
        factory = factory_document.get("factory_baseline")
        if not isinstance(factory, dict):
            raise ValueError("Factory calibration has no factory_baseline")
        self.stereo = pupil["stereo"]
        self.left_factory = factory["left_gaze"]
        self.right_factory = factory["right_gaze"]

    def predict(
        self,
        left_pupil: np.ndarray,
        right_pupil: np.ndarray,
        factory_sample: dict[str, object] | None,
    ) -> NativePupilHybridPrediction:
        disparity = apply_pupil_stereo_mapping(self.stereo, left_pupil, right_pupil)
        target_range = self.stereo.get("target_range_deg", [-8.0, -1.8])
        in_range = float(target_range[0]) - 0.75 <= disparity <= float(target_range[1]) + 0.75
        if factory_sample is None:
            return NativePupilHybridPrediction(
                None, None, None, disparity, None, (0.0, 0.0), in_range
            )
        confidence = (
            float(factory_sample.get("leftEyeConfidence", 0.0)),
            float(factory_sample.get("rightEyeConfidence", 0.0)),
        )
        if not (
            bool(factory_sample.get("leftEyeIsValid", False))
            and bool(factory_sample.get("rightEyeIsValid", False))
        ):
            return NativePupilHybridPrediction(
                None, None, None, disparity, None, confidence, in_range
            )
        meta_left = apply_factory_mapping(
            self.left_factory, list(factory_sample.get("leftEyeOrientation", [0.0] * 4))
        )
        meta_right = apply_factory_mapping(
            self.right_factory, list(factory_sample.get("rightEyeOrientation", [0.0] * 4))
        )
        center = (
            (meta_left[0] + meta_right[0]) * 0.5,
            (meta_left[1] + meta_right[1]) * 0.5,
        )
        left_gaze = (center[0] - disparity * 0.5, center[1])
        right_gaze = (center[0] + disparity * 0.5, center[1])
        ipd_m = 0.065
        left_position = np.asarray(
            factory_sample.get("leftEyePosition", [-0.0325, 0.0, 0.0]), dtype=np.float64
        )
        right_position = np.asarray(
            factory_sample.get("rightEyePosition", [0.0325, 0.0, 0.0]), dtype=np.float64
        )
        measured_ipd = float(np.linalg.norm(right_position - left_position))
        if 0.04 <= measured_ipd <= 0.09:
            ipd_m = measured_ipd
        return NativePupilHybridPrediction(
            left_gaze,
            right_gaze,
            center,
            disparity,
            convergence_distance_m(disparity, ipd_m),
            confidence,
            in_range,
        )


def draw_gaze_panel(
    canvas: np.ndarray,
    origin: tuple[int, int],
    title: str,
    gaze: tuple[float, float] | None,
    color: tuple[int, int, int],
) -> None:
    x, y = origin
    width, height = 390, 280
    put_text(canvas, title, (x, y - 14), color, 0.6, 1)
    cv2.rectangle(canvas, (x, y), (x + width, y + height), (70, 70, 70), 1)
    cv2.line(canvas, (x + width // 2, y), (x + width // 2, y + height), (45, 45, 45), 1)
    cv2.line(canvas, (x, y + height // 2), (x + width, y + height // 2), (45, 45, 45), 1)
    if gaze is None:
        put_text(canvas, "WAITING FOR LIVE META GAZE", (x + 45, y + 145), (60, 175, 255), 0.5)
        return
    marker = gaze_marker_position(gaze[0], gaze[1], x, y, width, height)
    cv2.circle(canvas, marker, 11, color, 3, cv2.LINE_AA)
    put_text(
        canvas, f"yaw {gaze[0]:+.1f}  pitch {gaze[1]:+.1f} deg",
        (x + 12, y + height - 14), color, 0.48, 1,
    )


def render(prediction: NativePupilHybridPrediction, rate_hz: float, label_age: float | None) -> np.ndarray:
    canvas = np.zeros((720, 1120, 3), dtype=np.uint8)
    put_text(canvas, "Quest Pro independent-eye hybrid preview", (22, 42), (245, 245, 245), 0.84, 2)
    put_text(canvas, "OBSERVATION ONLY - no OSC or VRCFT output", (22, 76), (40, 175, 255), 0.55, 1)
    put_text(
        canvas, "Meta fused midpoint + calibrated pre-fusion pupil disparity",
        (22, 106), (175, 175, 175), 0.5, 1,
    )
    draw_gaze_panel(canvas, (22, 155), "LEFT RECONSTRUCTED GAZE", prediction.left_gaze_deg, (255, 210, 45))
    draw_gaze_panel(canvas, (450, 155), "RIGHT RECONSTRUCTED GAZE", prediction.right_gaze_deg, (245, 70, 235))
    put_text(canvas, "STEREO BREAKDOWN", (875, 155), (230, 230, 230), 0.58, 1)
    put_text(canvas, f"pupil disparity", (875, 205), (180, 180, 180), 0.46)
    put_text(canvas, f"{prediction.pupil_disparity_deg:+.2f} deg", (875, 238), (90, 235, 120), 0.65, 1)
    if not prediction.stereo_in_calibrated_range:
        put_text(canvas, "OUTSIDE CALIBRATED RANGE", (875, 265), (40, 175, 255), 0.40, 1)
    distance = "beyond 10 m" if prediction.estimated_distance_m is None else f"{prediction.estimated_distance_m:.2f} m"
    put_text(canvas, "estimated fixation", (875, 290), (180, 180, 180), 0.46)
    put_text(canvas, distance, (875, 323), (90, 235, 120), 0.62, 1)
    center = prediction.meta_center_deg
    center_text = "waiting" if center is None else f"yaw {center[0]:+.1f}, pitch {center[1]:+.1f}"
    put_text(canvas, "Meta common center", (875, 375), (180, 180, 180), 0.46)
    put_text(canvas, center_text, (875, 405), (210, 210, 210), 0.45)
    label_text = "waiting" if label_age is None else f"{label_age * 1000:.0f} ms old"
    put_text(canvas, f"native pupil {rate_hz:.1f} Hz | Meta labels {label_text}", (22, 500), (90, 235, 120), 0.5, 1)
    put_text(
        canvas,
        f"Meta confidence L {prediction.confidence[0]:.3f}  R {prediction.confidence[1]:.3f}",
        (22, 535), (190, 190, 190), 0.48,
    )
    put_text(canvas, "Validation: relax/drift only the right eye; the left marker should stay nearly fixed.", (22, 595), (225, 225, 225), 0.52, 1)
    put_text(canvas, "Then alternate near and far focus. This preview does not modify headset or VRCFT output.", (22, 632), (180, 180, 180), 0.48)
    put_text(canvas, "Q quits and removes the temporary tracepoint.", (22, 683), (150, 150, 150), 0.46)
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", required=True)
    parser.add_argument("--pupil-calibration", required=True)
    parser.add_argument("--factory-calibration", required=True)
    parser.add_argument("--labels-port", type=int, default=27274)
    parser.add_argument("--headless-seconds", type=float, default=0.0)
    arguments = parser.parse_args()

    model = NativePupilHybridModel(arguments.pupil_calibration, arguments.factory_calibration)
    labels = LabelSidecarRecorder(None, arguments.labels_port)
    reader = NeuralPupilReader(arguments.adb)
    eye_filter = IndependentEyeFilter(3, min_cutoff_hz=1.5, beta=0.04)
    latest = NativePupilHybridPrediction(None, None, None, 0.0, None, (0.0, 0.0), True)
    sample_count = 0
    rate_count = 0
    rate_hz = 0.0
    rate_started = time.monotonic()
    deadline = time.monotonic() + arguments.headless_seconds if arguments.headless_seconds > 0 else None
    window = "Quest Pro independent-eye hybrid preview"
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
                left_raw = left_filtered * LEFT_SCALE + LEFT_MEAN
                right_raw = right_filtered * RIGHT_SCALE + RIGHT_MEAN
                factory_sample = labels.nearest_sample(sample.pc_monotonic_ns)
                latest = model.predict(left_raw, right_raw, factory_sample)
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
            cv2.imshow(window, render(latest, rate_hz, labels.label_age_seconds()))
            key = cv2.waitKeyEx(8)
            if key in (ord("q"), ord("Q"), 27):
                break
        if deadline is not None:
            if sample_count == 0:
                raise RuntimeError("The native pupil source produced no samples")
            print(json.dumps({"samples": sample_count, "prediction": latest.__dict__}))
        return 0
    finally:
        reader.close()
        labels.close()
        if deadline is None:
            cv2.destroyWindow(window)


if __name__ == "__main__":
    raise SystemExit(main())
