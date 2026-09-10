#!/usr/bin/env python3
"""Observation-only fusion of Meta gaze/face signals with camera convergence/tongue."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from open_source_preview import FACE_OUTPUT_NAMES, TONGUE_START, OpenSourcePrediction
from stereo_eye_calibration import (
    apply_convergence_mapping,
    quaternion_yaw_pitch,
)


@dataclass(frozen=True)
class HybridPrediction:
    left_gaze_deg: tuple[float, float] | None
    right_gaze_deg: tuple[float, float] | None
    camera_disparity_deg: float
    estimated_distance_m: float | None
    factory_confidence: tuple[float, float]
    factory_expressions: dict[str, float]
    camera: OpenSourcePrediction


def apply_factory_mapping(
    mapping: dict[str, Any], orientation_xyzw: list[float]
) -> tuple[float, float]:
    yaw, pitch = quaternion_yaw_pitch(orientation_xyzw)
    coefficients = np.asarray(mapping["coefficients"], dtype=np.float64)
    output = np.asarray([1.0, yaw, pitch], dtype=np.float64) @ coefficients
    return float(output[0]), float(output[1])


def convergence_distance_m(disparity_deg: float, ipd_m: float = 0.065) -> float | None:
    vergence = abs(math.radians(disparity_deg))
    if vergence < math.radians(0.25):
        return None
    distance = ipd_m / (2.0 * math.tan(vergence / 2.0))
    if not math.isfinite(distance):
        return None
    return float(np.clip(distance, 0.15, 10.0))


def gaze_marker_position(
    yaw_deg: float,
    pitch_deg: float,
    x: int,
    y: int,
    width: int,
    height: int,
) -> tuple[int, int]:
    """Map Meta/Babble angles to an intuitive viewer-facing gaze panel.

    The calibration target uses headset coordinates: negative yaw is the
    wearer's right and positive pitch is up. Pixel X/Y grow right/down, so both
    axes must be inverted for the dashboard.
    """
    marker_x = int(np.clip(x + (40.0 - yaw_deg) / 80.0 * width, x, x + width))
    marker_y = int(np.clip(y + (35.0 - pitch_deg) / 70.0 * height, y, y + height))
    return marker_x, marker_y


class HybridModelPreview:
    """Use Meta for proven signals and cameras only for the missing components."""

    def __init__(self, calibration_path: str | Path) -> None:
        self.calibration_path = Path(calibration_path).resolve()
        calibration = json.loads(self.calibration_path.read_text(encoding="utf-8"))
        factory = calibration.get("factory_baseline")
        stereo = calibration.get("stereo")
        if not isinstance(factory, dict) or not isinstance(stereo, dict):
            raise ValueError(
                "Hybrid preview requires a calibration containing factory_baseline "
                "and stereo models"
            )
        self.left_factory = factory["left_gaze"]
        self.right_factory = factory["right_gaze"]
        self.stereo = stereo

    def predict(
        self,
        camera: OpenSourcePrediction,
        factory_sample: dict[str, object] | None,
        schema_names: list[str],
    ) -> HybridPrediction:
        disparity = apply_convergence_mapping(
            self.stereo,
            camera.left_eye[3:5],
            camera.right_eye[3:5],
        )
        expressions: dict[str, float] = {}
        left_gaze = None
        right_gaze = None
        confidence = (0.0, 0.0)
        ipd_m = 0.065
        if factory_sample is not None:
            values = factory_sample.get("values", [])
            if len(schema_names) == len(values):
                expressions = {
                    name: float(value) for name, value in zip(schema_names, values)
                }
            left_valid = bool(factory_sample.get("leftEyeIsValid", False))
            right_valid = bool(factory_sample.get("rightEyeIsValid", False))
            confidence = (
                float(factory_sample.get("leftEyeConfidence", 0.0)),
                float(factory_sample.get("rightEyeConfidence", 0.0)),
            )
            if left_valid and right_valid:
                native_left = apply_factory_mapping(
                    self.left_factory,
                    list(factory_sample.get("leftEyeOrientation", [0.0] * 4)),
                )
                native_right = apply_factory_mapping(
                    self.right_factory,
                    list(factory_sample.get("rightEyeOrientation", [0.0] * 4)),
                )
                center_yaw = (native_left[0] + native_right[0]) * 0.5
                # The calibrated camera model supplies only vergence. Meta keeps
                # the much more accurate cyclopean gaze direction and both pitches.
                left_gaze = (center_yaw - disparity * 0.5, native_left[1])
                right_gaze = (center_yaw + disparity * 0.5, native_right[1])
                left_position = np.asarray(
                    factory_sample.get("leftEyePosition", [-0.0325, 0.0, 0.0]),
                    dtype=np.float64,
                )
                right_position = np.asarray(
                    factory_sample.get("rightEyePosition", [0.0325, 0.0, 0.0]),
                    dtype=np.float64,
                )
                measured_ipd = float(np.linalg.norm(right_position - left_position))
                if 0.04 <= measured_ipd <= 0.09:
                    ipd_m = measured_ipd
        return HybridPrediction(
            left_gaze_deg=left_gaze,
            right_gaze_deg=right_gaze,
            camera_disparity_deg=disparity,
            estimated_distance_m=convergence_distance_m(disparity, ipd_m),
            factory_confidence=confidence,
            factory_expressions=expressions,
            camera=camera,
        )

    @staticmethod
    def _text(
        canvas: np.ndarray,
        text: str,
        position: tuple[int, int],
        color: tuple[int, int, int] = (220, 220, 220),
        scale: float = 0.52,
        thickness: int = 1,
    ) -> None:
        cv2.putText(
            canvas, text, position, cv2.FONT_HERSHEY_SIMPLEX, scale, color,
            thickness, cv2.LINE_AA,
        )

    @classmethod
    def _gaze_panel(
        cls,
        canvas: np.ndarray,
        origin: tuple[int, int],
        title: str,
        gaze: tuple[float, float] | None,
        color: tuple[int, int, int],
    ) -> None:
        x, y = origin
        width, height = 330, 230
        cls._text(canvas, title, (x, y - 12), color, 0.58, 1)
        cv2.rectangle(canvas, (x, y), (x + width, y + height), (75, 75, 75), 1)
        cv2.line(canvas, (x + width // 2, y), (x + width // 2, y + height),
                 (50, 50, 50), 1)
        cv2.line(canvas, (x, y + height // 2), (x + width, y + height // 2),
                 (50, 50, 50), 1)
        if gaze is None:
            cls._text(canvas, "WAITING FOR META EYE POSES", (x + 34, y + 120),
                      (60, 170, 255), 0.48, 1)
            return
        yaw, pitch = gaze
        marker_x, marker_y = gaze_marker_position(
            yaw, pitch, x, y, width, height
        )
        cv2.circle(canvas, (marker_x, marker_y), 10, color, 2, cv2.LINE_AA)
        cls._text(canvas, f"view right {-yaw:+.1f}  up {pitch:+.1f} deg",
                  (x + 10, y + height - 12), color, 0.48)

    @classmethod
    def _bar(
        cls,
        canvas: np.ndarray,
        label: str,
        value: float,
        x: int,
        y: int,
        color: tuple[int, int, int],
    ) -> None:
        cls._text(canvas, label, (x, y + 15), (210, 210, 210), 0.43)
        bar_x = x + 190
        width = 155
        cv2.rectangle(canvas, (bar_x, y + 2), (bar_x + width, y + 16),
                      (65, 65, 65), 1)
        fill = int(np.clip(value, 0.0, 1.0) * width)
        if fill:
            cv2.rectangle(canvas, (bar_x + 1, y + 3),
                          (bar_x + fill, y + 15), color, -1)
        cls._text(canvas, f"{value:.2f}", (bar_x + width + 8, y + 15), color, 0.43)

    @classmethod
    def render(cls, prediction: HybridPrediction) -> np.ndarray:
        canvas = np.zeros((880, 1240, 3), dtype=np.uint8)
        cyan = (255, 210, 45)
        magenta = (245, 70, 235)
        green = (80, 235, 120)
        orange = (50, 175, 255)
        cls._text(canvas, "Quest Pro hybrid tracking preview", (24, 38),
                  (245, 245, 245), 0.9, 2)
        cls._text(canvas, "OBSERVATION ONLY - no OSC or VRCFT output", (24, 68),
                  orange, 0.58, 1)
        cls._text(
            canvas,
            "Meta gaze + standard expressions | stereo-camera convergence | camera tongue",
            (24, 94), (165, 165, 165), 0.48,
        )
        cls._gaze_panel(canvas, (24, 130), "LEFT HYBRID GAZE",
                        prediction.left_gaze_deg, cyan)
        cls._gaze_panel(canvas, (380, 130), "RIGHT HYBRID GAZE",
                        prediction.right_gaze_deg, magenta)

        cls._text(canvas, "Convergence", (760, 130), (230, 230, 230), 0.62, 1)
        cls._text(canvas, f"camera vergence  {abs(prediction.camera_disparity_deg):.2f} deg",
                  (760, 170), green, 0.56, 1)
        distance = prediction.estimated_distance_m
        distance_text = ">10 m / parallel" if distance is None else f"{distance:.2f} m"
        cls._text(canvas, f"estimated fixation  {distance_text}",
                  (760, 205), green, 0.68, 1)
        cls._text(canvas,
                  f"Meta confidence  L {prediction.factory_confidence[0]:.3f}  "
                  f"R {prediction.factory_confidence[1]:.3f}",
                  (760, 245), (190, 190, 190), 0.48)
        cls._text(canvas, "Meta's own combined point is intentionally not used for depth.",
                  (760, 285), orange, 0.43)

        cv2.line(canvas, (24, 390), (1216, 390), (60, 60, 60), 1)
        cls._text(canvas, "META FOUNDATION: brows and standard mouth", (24, 425),
                  (230, 230, 230), 0.6, 1)
        expression_names = (
            "InnerBrowRaiserL", "InnerBrowRaiserR", "OuterBrowRaiserL",
            "OuterBrowRaiserR", "BrowLowererL", "BrowLowererR", "JawDrop",
            "LipCornerPullerL", "LipCornerPullerR", "TongueOut", "TongueRetreat",
        )
        for row, name in enumerate(expression_names):
            cls._bar(canvas, name, prediction.factory_expressions.get(name, 0.0),
                     24, 450 + row * 34, cyan)

        cls._text(canvas, "CAMERA ADD-ON: detailed tongue candidates", (650, 425),
                  (230, 230, 230), 0.6, 1)
        for row, index in enumerate(range(TONGUE_START, len(FACE_OUTPUT_NAMES))):
            cls._bar(canvas, FACE_OUTPUT_NAMES[index],
                     float(prediction.camera.fused_face[index]),
                     650, 450 + row * 31, green)
        cls._text(
            canvas,
            "Test gaze, near/far focus, brows, then each tongue direction. Q quits.",
            (24, 858), (160, 160, 160), 0.48,
        )
        return canvas
