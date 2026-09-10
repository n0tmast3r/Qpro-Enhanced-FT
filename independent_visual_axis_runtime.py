#!/usr/bin/env python3
"""Apply the saved independent visual-axis calibration and optionally feed VRCFT."""

from __future__ import annotations

import argparse
import json
import math
import queue
import socket
import struct
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from eye_signal_filter import IndependentEyeFilter
from native_eye_probe import gaze_panel, put_text
from native_raw_eye_probe import RawEyeSample, RawTraceEyeReader
from visual_axis_calibration import apply_axis_eye_mapping


PACKET_MAGIC = b"QPGE"
PACKET_VERSION = 1
PACKET_FORMAT = "<4sBBHffff"
PACKET_SIZE = struct.calcsize(PACKET_FORMAT)
DEFAULT_PORT = 27275


def load_calibration(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("format") not in {
        "qpro-independent-personalized-visual-axis-v1",
        "qpro-independent-personalized-visual-axis-v2",
    }:
        raise ValueError("This is not an independent visual-axis calibration")
    if not payload.get("quality_gate", {}).get("gaze_pass", False):
        raise ValueError("The saved calibration did not pass its absolute-gaze gate")
    for eye in ("left", "right"):
        coefficients = np.asarray(payload[eye]["coefficients"], dtype=np.float64)
        if coefficients.shape != (3, 2) or not np.all(np.isfinite(coefficients)):
            raise ValueError(f"The {eye} calibration coefficients are invalid")
    return payload


def calibrated_angles(
    calibration: dict[str, Any], sample: RawEyeSample
) -> tuple[np.ndarray, np.ndarray]:
    tag_mapping = calibration.get("detector_tag_mapping", {})
    physical_left_source = tag_mapping.get("physical_left", "trace_tag_0")
    physical_right_source = tag_mapping.get("physical_right", "trace_tag_1")
    sources = {
        "trace_tag_0": sample.left_angles,
        "trace_tag_1": sample.right_angles,
    }
    if physical_left_source not in sources or physical_right_source not in sources:
        raise ValueError("The calibration has an unsupported detector-tag mapping")
    left = np.asarray(
        apply_axis_eye_mapping(calibration["left"], sources[physical_left_source]),
        dtype=np.float64,
    )
    right = np.asarray(
        apply_axis_eye_mapping(calibration["right"], sources[physical_right_source]),
        dtype=np.float64,
    )
    return left, right


def vrcft_angles(angles_deg: np.ndarray) -> tuple[float, float]:
    """Convert calibrated yaw/pitch degrees to VRCFT gaze radians.

    The local node-18 target calibration already resolves the headset camera
    convention.  Negating yaw again mirrored both avatar eyes and inverted
    vergence, so each custom X channel is published with the calibrated sign.
    """
    yaw, pitch = map(float, angles_deg)
    return math.radians(yaw), math.radians(pitch)


def encode_packet(
    left_deg: np.ndarray,
    right_deg: np.ndarray,
    left_valid: bool = True,
    right_valid: bool = True,
) -> bytes:
    # Physical testing in VRChat is the authoritative final boundary test.
    # VRCFT/VRChat's exposed Left channel drives the user's physical right eye
    # and vice versa: a same-target near fixation diverged when physical eyes
    # were sent to same-named channels. Cross only at packet publication; the
    # detector ownership, per-eye fits, previews, and filters stay physical.
    left_x, left_y = vrcft_angles(right_deg)
    right_x, right_y = vrcft_angles(left_deg)
    flags = int(right_valid) | (int(left_valid) << 1)
    return struct.pack(
        PACKET_FORMAT,
        PACKET_MAGIC,
        PACKET_VERSION,
        flags,
        0,
        left_x,
        left_y,
        right_x,
        right_y,
    )


class GazeBroadcaster:
    def __init__(self, port: int) -> None:
        self._address = ("127.0.0.1", int(port))
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, left: np.ndarray, right: np.ndarray) -> None:
        self._socket.sendto(encode_packet(left, right), self._address)

    def close(self) -> None:
        self._socket.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", required=True)
    parser.add_argument("--calibration", required=True)
    parser.add_argument("--output-vrcft", action="store_true")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--headless-seconds", type=float, default=0.0)
    parser.add_argument(
        "--stop-file",
        help="exit cleanly when this supervisor-owned file appears",
    )
    arguments = parser.parse_args()
    stop_file = Path(arguments.stop_file).resolve() if arguments.stop_file else None
    if arguments.port < 1024 or arguments.port > 65535:
        raise ValueError("Port is outside the supported range")

    calibration = load_calibration(arguments.calibration)
    reader = RawTraceEyeReader(arguments.adb)
    eye_filter = IndependentEyeFilter(
        2,
        median_window=3,
        min_cutoff_hz=4.0,
        beta=0.15,
        derivative_cutoff_hz=1.5,
    )
    broadcaster = GazeBroadcaster(arguments.port) if arguments.output_vrcft else None
    latest: RawEyeSample | None = None
    filtered_left: np.ndarray | None = None
    filtered_right: np.ndarray | None = None
    rate_count = 0
    rate_hz = 0.0
    rate_start = time.monotonic()
    deadline = (
        time.monotonic() + arguments.headless_seconds
        if arguments.headless_seconds > 0
        else None
    )
    window = "Quest Pro calibrated independent gaze"
    if deadline is None:
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window, 1180, 760)

    try:
        # Keep startup inside the cleanup guard. If trace configuration fails
        # halfway through, close() must still remove the headset reader and
        # trace instance instead of poisoning the next GUI launch.
        reader.start()
        while deadline is None or time.monotonic() < deadline:
            if stop_file is not None and stop_file.exists():
                break
            received = False
            while True:
                try:
                    sample = reader.samples.get_nowait()
                except queue.Empty:
                    break
                latest = sample
                left, right = calibrated_angles(calibration, sample)
                filtered_left, filtered_right = eye_filter.update(
                    left, right, sample.pc_monotonic_ns / 1_000_000_000.0
                )
                if broadcaster is not None:
                    broadcaster.send(filtered_left, filtered_right)
                rate_count += 1
                received = True

            if not reader.errors.empty():
                raise RuntimeError(reader.errors.get_nowait())
            now = time.monotonic()
            elapsed = now - rate_start
            if elapsed >= 1.0:
                rate_hz = rate_count / elapsed
                rate_count = 0
                rate_start = now
            if deadline is not None:
                if not received:
                    time.sleep(0.005)
                continue

            image = np.zeros((760, 1180, 3), dtype=np.uint8)
            put_text(image, "Quest Pro calibrated independent gaze", (24, 40),
                     (245, 245, 245), 0.9, 2)
            mode = "LIVE VRCFT GAZE-ONLY OUTPUT" if broadcaster else "OBSERVATION ONLY"
            mode_color = (80, 235, 120) if broadcaster else (50, 175, 255)
            put_text(image, mode, (24, 74), mode_color, 0.58, 1)
            put_text(
                image,
                "Meta node-18 local branch + per-eye fit + independent mild smoothing",
                (24, 102), (175, 175, 175), 0.5,
            )
            if latest is None or filtered_left is None or filtered_right is None:
                put_text(image, "Waiting for independent detector rays...", (330, 350),
                         (50, 175, 255), 0.7, 1)
            else:
                gaze_panel(image, (24, 160), "LEFT CALIBRATED GAZE",
                           tuple(filtered_left), (255, 210, 45))
                gaze_panel(image, (410, 160), "RIGHT CALIBRATED GAZE",
                           tuple(filtered_right), (245, 70, 235))
                put_text(image, "RAW PERSONALIZED DETECTOR", (805, 160),
                         (220, 220, 220), 0.58, 1)
                put_text(image,
                         f"tag 1 / physical L  yaw {latest.right_angles[0]:+6.2f}  pitch {latest.right_angles[1]:+6.2f}",
                         (805, 204), (255, 210, 45), 0.48, 1)
                put_text(image,
                         f"tag 0 / physical R  yaw {latest.left_angles[0]:+6.2f}  pitch {latest.left_angles[1]:+6.2f}",
                         (805, 238), (245, 70, 235), 0.48, 1)
                difference = filtered_right - filtered_left
                put_text(image, "CALIBRATED DIFFERENCE", (805, 300),
                         (220, 220, 220), 0.55, 1)
                put_text(image, f"horizontal R-L {difference[0]:+6.2f} deg",
                         (805, 340), (80, 235, 120), 0.48, 1)
                put_text(image, f"vertical R-L   {difference[1]:+6.2f} deg",
                         (805, 374), (80, 235, 120), 0.48, 1)
                left_vrcft = vrcft_angles(filtered_left)
                right_vrcft = vrcft_angles(filtered_right)
                put_text(image, "VRCFT radians (calibrated yaw, pitch)", (24, 470),
                         (210, 210, 210), 0.55, 1)
                put_text(image,
                         f"L ({left_vrcft[0]:+.3f}, {left_vrcft[1]:+.3f})   "
                         f"R ({right_vrcft[0]:+.3f}, {right_vrcft[1]:+.3f})",
                         (24, 508), (210, 210, 210), 0.52, 1)
                put_text(image, f"detector {rate_hz:.1f} Hz | two filters remain isolated",
                         (24, 556), (80, 235, 120), 0.52, 1)
                quality = calibration["quality_gate"]
                put_text(
                    image,
                    "absolute gaze passed; exact depth remains experimental "
                    f"(convergence gate: {quality['convergence_pass']})",
                    (24, 596), (50, 175, 255), 0.48, 1,
                )
                put_text(image,
                         "Factory blink/face values are not changed by this process.",
                         (24, 636), (190, 190, 190), 0.48, 1)
            put_text(image, "Q quits and restores the stock headset model.",
                     (24, 720), (155, 155, 155), 0.46, 1)
            cv2.imshow(window, image)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if not received:
                time.sleep(0.002)

        if latest is None or filtered_left is None or filtered_right is None:
            raise RuntimeError("The detector trace produced no paired eye samples")
        if deadline is not None:
            print(json.dumps({
                "rate_hz": rate_hz,
                "trace_tag_0_raw_deg": latest.left_angles,
                "trace_tag_1_raw_deg": latest.right_angles,
                "left_calibrated_deg": filtered_left.tolist(),
                "right_calibrated_deg": filtered_right.tolist(),
                "packet_bytes": PACKET_SIZE,
            }))
        return 0
    finally:
        if broadcaster is not None:
            broadcaster.close()
        reader.close()
        if deadline is None:
            cv2.destroyWindow(window)


if __name__ == "__main__":
    raise SystemExit(main())
