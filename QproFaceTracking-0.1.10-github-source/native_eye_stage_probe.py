#!/usr/bin/env python3
"""Compare independent neural pupils with the already-fused DNN gaze axes."""

from __future__ import annotations

import argparse
import math
import queue

import cv2
import numpy as np

from native_eye_probe import put_text
from native_eye_pupil_probe import (
    LEFT_MEAN,
    LEFT_SCALE,
    RIGHT_MEAN,
    RIGHT_SCALE,
    NeuralPupilReader,
    PupilSample,
)


def axis_angles(vector: tuple[float, float, float]) -> tuple[float, float]:
    x, y, z = vector
    return (
        math.degrees(math.atan2(x, z)),
        math.degrees(math.atan2(-y, math.hypot(x, z))),
    )


def marker_panel(
    image: np.ndarray,
    origin: tuple[int, int],
    title: str,
    x_value: float,
    y_value: float,
    x_limit: float,
    y_limit: float,
    color: tuple[int, int, int],
    value_text: str,
) -> None:
    x, y = origin
    width, height = 400, 235
    put_text(image, title, (x, y - 12), color, 0.55, 1)
    cv2.rectangle(image, (x, y), (x + width, y + height), (70, 70, 70), 1)
    cv2.line(image, (x + width // 2, y), (x + width // 2, y + height), (45, 45, 45), 1)
    cv2.line(image, (x, y + height // 2), (x + width, y + height // 2), (45, 45, 45), 1)
    px = x + width // 2 + int(np.clip(x_value / x_limit, -1, 1) * (width // 2 - 10))
    py = y + height // 2 - int(np.clip(y_value / y_limit, -1, 1) * (height // 2 - 10))
    cv2.circle(image, (px, py), 10, color, 3, cv2.LINE_AA)
    put_text(image, value_text, (x + 10, y + height - 12), color, 0.42, 1)


def render(
    sample: PupilSample | None,
    rate_hz: float,
    title: str = "Quest Pro eye-pipeline stage comparison",
    notice: str = "READ ONLY - temporary tracepoint, no tracking output",
    instruction: str = (
        "Drift only the right eye: top-right should move alone; "
        "bottom axes reveal where fusion begins."
    ),
) -> np.ndarray:
    image = np.zeros((820, 900, 3), dtype=np.uint8)
    put_text(image, title, (22, 38), (245, 245, 245), 0.76, 2)
    put_text(image, notice, (22, 68), (80, 190, 255), 0.48, 1)
    if sample is None:
        put_text(image, "Waiting for SocialGazeDNN...", (275, 390), (50, 175, 255), 0.6)
        return image

    left_pupil = (np.asarray(sample.left_pupil_3d) - LEFT_MEAN) / LEFT_SCALE
    right_pupil = (np.asarray(sample.right_pupil_3d) - RIGHT_MEAN) / RIGHT_SCALE
    left_axis = axis_angles(sample.left_model_axis)
    right_axis = axis_angles(sample.right_model_axis)
    marker_panel(image, (22, 115), "LEFT INDEPENDENT PUPIL", left_pupil[0], left_pupil[1], 3, 3,
                 (255, 210, 45), f"normalized X {left_pupil[0]:+.2f}  Y {left_pupil[1]:+.2f}")
    marker_panel(image, (478, 115), "RIGHT INDEPENDENT PUPIL", right_pupil[0], right_pupil[1], 3, 3,
                 (245, 70, 235), f"normalized X {right_pupil[0]:+.2f}  Y {right_pupil[1]:+.2f}")
    marker_panel(image, (22, 425), "LEFT DNN OPTICAL AXIS", left_axis[0], left_axis[1], 40, 35,
                 (255, 210, 45), f"yaw {left_axis[0]:+.2f}  pitch {left_axis[1]:+.2f} deg")
    marker_panel(image, (478, 425), "RIGHT DNN OPTICAL AXIS", right_axis[0], right_axis[1], 40, 35,
                 (245, 70, 235), f"yaw {right_axis[0]:+.2f}  pitch {right_axis[1]:+.2f} deg")
    separation = math.hypot(right_axis[0] - left_axis[0], right_axis[1] - left_axis[1])
    put_text(image, f"{rate_hz:.1f} Hz | DNN axis separation {separation:.3f} deg", (22, 710),
             (90, 235, 120), 0.5, 1)
    put_text(image, instruction, (22, 752), (210, 210, 210), 0.44, 1)
    put_text(image, "Q quits and removes the tracepoint.", (22, 790), (150, 150, 150), 0.43)
    return image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", required=True)
    parser.add_argument("--title", default="Quest Pro eye-pipeline stage comparison")
    parser.add_argument(
        "--notice", default="READ ONLY - temporary tracepoint, no tracking output"
    )
    parser.add_argument(
        "--instruction",
        default=(
            "Drift only the right eye: top-right should move alone; "
            "bottom axes reveal where fusion begins."
        ),
    )
    arguments = parser.parse_args()
    reader = NeuralPupilReader(arguments.adb)
    reader.start()
    latest = None
    rate_count = 0
    rate_hz = 0.0
    import time
    rate_start = time.monotonic()
    window = "Quest Pro eye-pipeline stages"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, 900, 820)
    try:
        while True:
            while True:
                try:
                    latest = reader.samples.get_nowait()
                    rate_count += 1
                except queue.Empty:
                    break
            if not reader.errors.empty():
                raise RuntimeError(reader.errors.get_nowait())
            now = time.monotonic()
            if now - rate_start >= 1.0:
                rate_hz = rate_count / (now - rate_start)
                rate_count = 0
                rate_start = now
            cv2.imshow(
                window,
                render(
                    latest,
                    rate_hz,
                    arguments.title,
                    arguments.notice,
                    arguments.instruction,
                ),
            )
            key = cv2.waitKeyEx(8)
            if key in (ord("q"), ord("Q"), 27):
                return 0
    finally:
        reader.close()
        cv2.destroyWindow(window)


if __name__ == "__main__":
    raise SystemExit(main())
