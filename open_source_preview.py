#!/usr/bin/env python3
"""Observation-only preview of reusable EyeTrackVR and Project Babble models."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


EYE_OUTPUT_NAMES = ("brow", "lid", "squint", "gaze X", "gaze Y")
FACE_OUTPUT_NAMES = (
    "CheekPuffLeft", "CheekPuffRight", "CheekSuckLeft", "CheekSuckRight",
    "JawOpen", "JawForward", "JawLeft", "JawRight", "NoseSneerLeft",
    "NoseSneerRight", "MouthFunnel", "MouthPucker", "MouthLeft",
    "MouthRight", "MouthRollUpper", "MouthRollLower", "MouthShrugUpper",
    "MouthShrugLower", "MouthClose", "MouthSmileLeft", "MouthSmileRight",
    "MouthFrownLeft", "MouthFrownRight", "MouthDimpleLeft",
    "MouthDimpleRight", "MouthUpperUpLeft", "MouthUpperUpRight",
    "MouthLowerDownLeft", "MouthLowerDownRight", "MouthPressLeft",
    "MouthPressRight", "MouthStretchLeft", "MouthStretchRight",
    "TongueOut", "TongueUp", "TongueDown", "TongueLeft", "TongueRight",
    "TongueRoll", "TongueBendDown", "TongueCurlUp", "TongueSquish",
    "TongueFlat", "TongueTwistLeft", "TongueTwistRight",
)
TONGUE_START = 33


@dataclass(frozen=True)
class OpenSourcePrediction:
    left_eye: np.ndarray
    right_eye: np.ndarray
    left_face: np.ndarray
    right_face: np.ndarray
    fused_face: np.ndarray
    inference_ms: float


def eye_tensor(image: np.ndarray) -> np.ndarray:
    """Apply EyeTrackVR NEXT's ImageNet preprocessing to one grayscale eye."""
    resized = cv2.resize(image, (224, 224), interpolation=cv2.INTER_AREA)
    tensor = np.repeat(resized[:, :, None], 3, axis=2).astype(np.float32) / 255.0
    tensor = (tensor - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
        [0.229, 0.224, 0.225], dtype=np.float32
    )
    return np.ascontiguousarray(tensor.transpose(2, 0, 1)[None, ...])


def face_tensor(image: np.ndarray) -> np.ndarray:
    """Apply Project Babble's grayscale preprocessing to one lower-face view."""
    resized = cv2.resize(image, (224, 224), interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(resized[None, None, ...], dtype=np.float32) / 255.0


def fuse_face_outputs(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """Keep the strongest view for each diagnostic channel; no stereo stitching."""
    return np.maximum(left, right)


class OpenSourceModelPreview:
    """Run upstream ONNX models without emitting tracking data anywhere."""

    def __init__(
        self,
        eye_model_path: str | Path,
        face_model_path: str | Path,
        smoothing: float = 0.35,
    ) -> None:
        if not 0 < smoothing <= 1:
            raise ValueError("smoothing must be in (0, 1]")
        self.eye_model_path = Path(eye_model_path).resolve()
        self.face_model_path = Path(face_model_path).resolve()
        if not self.eye_model_path.is_file():
            raise FileNotFoundError(f"EyeTrackVR NEXT model not found: {self.eye_model_path}")
        if not self.face_model_path.is_file():
            raise FileNotFoundError(
                f"Project Babble face model not found: {self.face_model_path}"
            )

        try:
            import onnxruntime as ort
        except (ImportError, OSError) as error:
            raise RuntimeError(
                "ONNX Runtime is unavailable. Re-run the launcher without "
                "-SkipPythonSetup so it can install the preview dependency."
            ) from error
        if not hasattr(ort, "InferenceSession"):
            raise RuntimeError("The installed onnxruntime package is incomplete")

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ["CPUExecutionProvider"]
        self.eye_session = ort.InferenceSession(
            str(self.eye_model_path), sess_options=options, providers=providers
        )
        self.face_session = ort.InferenceSession(
            str(self.face_model_path), sess_options=options, providers=providers
        )
        self.eye_input = self.eye_session.get_inputs()[0].name
        self.eye_output = self.eye_session.get_outputs()[0].name
        self.face_input = self.face_session.get_inputs()[0].name
        self.face_output = self.face_session.get_outputs()[0].name
        self.smoothing = smoothing
        self._previous: tuple[np.ndarray, ...] | None = None

        eye_shape = self.eye_session.get_outputs()[0].shape
        face_shape = self.face_session.get_outputs()[0].shape
        if eye_shape[-1] != 5:
            raise ValueError(f"Expected five NEXT outputs, got {eye_shape}")
        if face_shape[-1] != len(FACE_OUTPUT_NAMES):
            raise ValueError(
                f"Expected {len(FACE_OUTPUT_NAMES)} Babble outputs, got {face_shape}"
            )

    @staticmethod
    def _panel(strip: np.ndarray, camera_id: int) -> np.ndarray:
        if strip.ndim != 2 or strip.shape[0] != 400 or strip.shape[1] < 2000:
            raise ValueError(
                "Open-source preview requires the all-camera 400x2000 stream"
            )
        start = camera_id * 400
        return strip[:, start : start + 400]

    def _run_eye(self, image: np.ndarray) -> np.ndarray:
        result = self.eye_session.run(
            [self.eye_output], {self.eye_input: eye_tensor(image)}
        )[0]
        output = np.asarray(result, dtype=np.float32).reshape(-1)
        output[:3] = np.clip(output[:3], 0.0, 1.0)
        output[3:] = np.clip(output[3:], -1.0, 1.0)
        return output

    def _run_face(self, image: np.ndarray) -> np.ndarray:
        result = self.face_session.run(
            [self.face_output], {self.face_input: face_tensor(image)}
        )[0]
        return np.clip(np.asarray(result, dtype=np.float32).reshape(-1), 0.0, 1.0)

    def _smooth(self, values: tuple[np.ndarray, ...]) -> tuple[np.ndarray, ...]:
        if self._previous is None or self.smoothing == 1.0:
            smoothed = tuple(value.copy() for value in values)
        else:
            alpha = self.smoothing
            smoothed = tuple(
                previous + alpha * (value - previous)
                for value, previous in zip(values, self._previous)
            )
        self._previous = tuple(value.copy() for value in smoothed)
        return smoothed

    def predict(self, strip: np.ndarray) -> OpenSourcePrediction:
        started = time.perf_counter()
        left_eye = self._run_eye(self._panel(strip, 0))
        right_eye = self._run_eye(self._panel(strip, 1))
        # Camera 0 is mirrored relative to NEXT's gaze convention. The sign
        # correction was measured against the synchronized factory capture.
        left_eye[3] *= -1.0
        left_face = self._run_face(self._panel(strip, 2))
        right_face = self._run_face(self._panel(strip, 3))
        left_eye, right_eye, left_face, right_face = self._smooth(
            (left_eye, right_eye, left_face, right_face)
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return OpenSourcePrediction(
            left_eye=left_eye,
            right_eye=right_eye,
            left_face=left_face,
            right_face=right_face,
            fused_face=fuse_face_outputs(left_face, right_face),
            inference_ms=elapsed_ms,
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
            thickness, cv2.LINE_AA
        )

    @classmethod
    def _gaze_panel(
        cls,
        canvas: np.ndarray,
        origin: tuple[int, int],
        title: str,
        eye: np.ndarray,
        color: tuple[int, int, int],
    ) -> None:
        x, y = origin
        width, height = 280, 205
        cls._text(canvas, title, (x, y - 12), color, 0.6, 1)
        cv2.rectangle(canvas, (x, y), (x + width, y + height), (75, 75, 75), 1)
        cv2.line(canvas, (x + width // 2, y), (x + width // 2, y + height),
                 (50, 50, 50), 1)
        cv2.line(canvas, (x, y + height // 2), (x + width, y + height // 2),
                 (50, 50, 50), 1)
        gaze_x, gaze_y = float(eye[3]), float(eye[4])
        marker_x = x + round((gaze_x + 1.0) * 0.5 * width)
        marker_y = y + round((gaze_y + 1.0) * 0.5 * height)
        marker_x = int(np.clip(marker_x, x, x + width))
        marker_y = int(np.clip(marker_y, y, y + height))
        cv2.circle(canvas, (marker_x, marker_y), 9, color, 2, cv2.LINE_AA)
        cls._text(
            canvas,
            f"X {gaze_x:+.3f}   Y {gaze_y:+.3f}",
            (x + 10, y + height - 12), color, 0.48,
        )

    @classmethod
    def _bar(
        cls,
        canvas: np.ndarray,
        label: str,
        value: float,
        x: int,
        y: int,
        width: int,
        color: tuple[int, int, int],
        secondary: str = "",
    ) -> None:
        cls._text(canvas, label, (x, y + 15), (215, 215, 215), 0.43)
        bar_x = x + 170
        cv2.rectangle(canvas, (bar_x, y + 2), (bar_x + width, y + 16),
                      (65, 65, 65), 1)
        fill = round(np.clip(value, 0.0, 1.0) * width)
        if fill:
            cv2.rectangle(canvas, (bar_x + 1, y + 3),
                          (bar_x + fill, y + 15), color, -1)
        suffix = f"{value:.2f}"
        if secondary:
            suffix += f"  {secondary}"
        cls._text(canvas, suffix, (bar_x + width + 10, y + 15), color, 0.43)

    @classmethod
    def render(cls, prediction: OpenSourcePrediction) -> np.ndarray:
        canvas = np.zeros((880, 1240, 3), dtype=np.uint8)
        cyan = (255, 210, 45)
        magenta = (245, 70, 235)
        green = (80, 235, 120)
        orange = (50, 175, 255)
        cls._text(canvas, "Quest Pro open-source model preview", (24, 38),
                  (245, 245, 245), 0.9, 2)
        cls._text(
            canvas,
            "OBSERVATION ONLY - no OSC or VRCFT output",
            (24, 68), orange, 0.58, 1,
        )
        cls._text(
            canvas,
            f"EyeTrackVR NEXT + Project Babble | "
            f"four-model inference {prediction.inference_ms:.1f} ms",
            (24, 94), (165, 165, 165), 0.48,
        )

        cls._gaze_panel(canvas, (24, 126), "LEFT EYE - camera 0 (X corrected)",
                        prediction.left_eye, cyan)
        cls._gaze_panel(canvas, (328, 126), "RIGHT EYE - camera 1",
                        prediction.right_eye, magenta)

        cls._text(canvas, "Independent eye signals", (650, 126),
                  (230, 230, 230), 0.62, 1)
        metrics = (("Brow", 0), ("Lid", 1), ("Squint", 2))
        for row, (name, index) in enumerate(metrics):
            y = 150 + row * 48
            cls._text(canvas, name, (650, y + 17), (210, 210, 210), 0.48)
            cls._text(canvas, f"L {prediction.left_eye[index]:.3f}",
                      (760, y + 17), cyan, 0.48)
            cls._text(canvas, f"R {prediction.right_eye[index]:.3f}",
                      (885, y + 17), magenta, 0.48)
        disparity = float(prediction.right_eye[3] - prediction.left_eye[3])
        cls._text(canvas, "Raw horizontal disparity (R - L)", (650, 312),
                  (200, 200, 200), 0.48)
        cls._text(canvas, f"{disparity:+.4f}", (925, 312), green, 0.62, 1)
        cls._text(canvas, "Not depth-calibrated yet", (650, 340), orange, 0.45)
        cls._text(canvas, "Camera 4 is reserved for later brow/ROI refinement.",
                  (650, 367), (140, 140, 140), 0.43)

        cv2.line(canvas, (24, 392), (1216, 392), (60, 60, 60), 1)
        cls._text(canvas, "FACE: strongest current non-tongue channels",
                  (24, 425), (230, 230, 230), 0.6, 1)
        cls._text(canvas, "max(camera 2, camera 3) - diagnostic fusion only",
                  (24, 450), green, 0.43)
        strongest = np.argsort(prediction.fused_face[:TONGUE_START])[-10:][::-1]
        for row, index in enumerate(strongest):
            cls._bar(
                canvas,
                FACE_OUTPUT_NAMES[int(index)],
                float(prediction.fused_face[index]),
                24,
                467 + row * 34,
                180,
                green,
                f"L {prediction.left_face[index]:.2f}  "
                f"R {prediction.right_face[index]:.2f}",
            )

        cls._text(canvas, "TONGUE: all Project Babble channels", (650, 425),
                  (230, 230, 230), 0.6, 1)
        cls._text(canvas, "green=max  cyan=cam2  magenta=cam3", (650, 450),
                  (150, 150, 150), 0.43)
        for row, index in enumerate(range(TONGUE_START, len(FACE_OUTPUT_NAMES))):
            cls._bar(
                canvas,
                FACE_OUTPUT_NAMES[index],
                float(prediction.fused_face[index]),
                650,
                467 + row * 31,
                135,
                green,
                f"L {prediction.left_face[index]:.2f}  "
                f"R {prediction.right_face[index]:.2f}",
            )

        cls._text(
            canvas,
            "Move one eye/brow or tongue direction at a time. Q closes both windows.",
            (24, 858), (160, 160, 160), 0.48,
        )
        return canvas
