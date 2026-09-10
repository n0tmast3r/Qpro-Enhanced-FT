#!/usr/bin/env python3
"""Bridge BabbleCalibration's VR targets to Quest Pro per-eye NEXT signals."""

from __future__ import annotations

import json
import collections
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from open_source_preview import OpenSourcePrediction


def polynomial_features(x: float, y: float) -> np.ndarray:
    """EyeTrackVR-compatible degree-2 feature vector."""
    return np.array([1.0, x, y, x * x, x * y, y * y], dtype=np.float64)


def _ridge_fit(features: np.ndarray, targets: np.ndarray) -> np.ndarray:
    penalty = np.diag([0.0, 0.02, 0.02, 0.02, 0.02, 0.02])
    return np.linalg.solve(features.T @ features + penalty, features.T @ targets)


def _error_metrics(predicted: np.ndarray, expected: np.ndarray) -> dict[str, float]:
    errors = np.abs(predicted - expected)
    angular = np.linalg.norm(predicted - expected, axis=1)
    return {
        "yaw_mae_deg": float(np.mean(errors[:, 0])),
        "pitch_mae_deg": float(np.mean(errors[:, 1])),
        "angular_mae_deg": float(np.mean(angular)),
        "angular_p95_deg": float(np.percentile(angular, 95)),
    }


def fit_eye_mapping(
    samples: list[dict[str, Any]], eye: str
) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    """Fit and block-validate raw NEXT XY -> per-eye target yaw/pitch."""
    if eye not in ("left", "right"):
        raise ValueError("eye must be left or right")
    if len(samples) < 60:
        raise ValueError(f"Need at least 60 calibration samples, got {len(samples)}")

    raw = np.asarray([sample[f"{eye}_raw"] for sample in samples], dtype=np.float64)
    target = np.asarray(
        [sample[f"{eye}_target_deg"] for sample in samples], dtype=np.float64
    )
    features = np.asarray(
        [polynomial_features(float(x), float(y)) for x, y in raw],
        dtype=np.float64,
    )

    # Hold out complete one-second blocks. Neighboring 30 FPS frames never land
    # on opposite sides merely because their index is adjacent.
    started_ns = min(int(sample["timestamp_ns"]) for sample in samples)
    block_ids = np.asarray(
        [(int(sample["timestamp_ns"]) - started_ns) // 1_000_000_000
         for sample in samples],
        dtype=np.int64,
    )
    holdout = block_ids % 5 == 4
    if int(np.count_nonzero(holdout)) < 12:
        holdout = np.arange(len(samples)) % 5 == 4
    training = ~holdout
    if int(np.count_nonzero(training)) < 48:
        raise ValueError("Not enough non-holdout calibration samples")

    coefficients = _ridge_fit(features[training], target[training])
    initial_prediction = features[training] @ coefficients
    initial_error = np.linalg.norm(initial_prediction - target[training], axis=1)
    median = float(np.median(initial_error))
    mad = float(np.median(np.abs(initial_error - median)))
    cutoff = max(2.0, median + 4.0 * max(mad, 0.25))
    clean_training_indices = np.flatnonzero(training)[initial_error <= cutoff]
    if len(clean_training_indices) >= 48:
        coefficients = _ridge_fit(
            features[clean_training_indices], target[clean_training_indices]
        )

    held_prediction = features[holdout] @ coefficients
    held_metrics = _error_metrics(held_prediction, target[holdout])

    # The saved runtime mapping uses every non-outlier sample after validation.
    all_prediction = features @ coefficients
    all_error = np.linalg.norm(all_prediction - target, axis=1)
    all_median = float(np.median(all_error))
    all_mad = float(np.median(np.abs(all_error - all_median)))
    all_cutoff = max(2.0, all_median + 4.0 * max(all_mad, 0.25))
    clean = all_error <= all_cutoff
    if int(np.count_nonzero(clean)) >= 60:
        coefficients = _ridge_fit(features[clean], target[clean])

    final_prediction = features @ coefficients
    result = {
        "coefficients": coefficients.tolist(),
        "feature_order": ["1", "x", "y", "x2", "xy", "y2"],
        "output_order": ["yaw_deg", "pitch_deg"],
        "sample_count": len(samples),
        "retained_count": int(np.count_nonzero(clean)),
        "holdout_count": int(np.count_nonzero(holdout)),
        "held_out": held_metrics,
        "all_samples": _error_metrics(final_prediction, target),
        "raw_range": {
            "x": [float(np.min(raw[:, 0])), float(np.max(raw[:, 0]))],
            "y": [float(np.min(raw[:, 1])), float(np.max(raw[:, 1]))],
        },
        "target_range_deg": {
            "yaw": [float(np.min(target[:, 0])), float(np.max(target[:, 0]))],
            "pitch": [float(np.min(target[:, 1])), float(np.max(target[:, 1]))],
        },
    }
    return result, final_prediction, target


def apply_eye_mapping(mapping: dict[str, Any], raw_x: float, raw_y: float) -> tuple[float, float]:
    coefficients = np.asarray(mapping["coefficients"], dtype=np.float64)
    output = polynomial_features(raw_x, raw_y) @ coefficients
    return float(output[0]), float(output[1])


def _ema(values: np.ndarray, alpha: float) -> np.ndarray:
    filtered = np.empty_like(values, dtype=np.float64)
    filtered[0] = values[0]
    for index in range(1, len(values)):
        filtered[index] = filtered[index - 1] + alpha * (
            values[index] - filtered[index - 1]
        )
    return filtered


def _multi_polynomial_features(values: np.ndarray, degree: int = 2) -> np.ndarray:
    """Polynomial features for stereo inputs, ordered deterministically."""
    import itertools

    columns = [np.ones(len(values), dtype=np.float64)]
    for current_degree in range(1, degree + 1):
        for combination in itertools.combinations_with_replacement(
            range(values.shape[1]), current_degree
        ):
            columns.append(np.prod(values[:, combination], axis=1))
    return np.column_stack(columns)


def fit_convergence_mapping(
    samples: list[dict[str, Any]], smoothing: float = 0.35
) -> dict[str, Any]:
    """Fit stereo raw features directly to binocular yaw disparity."""
    convergence = [sample for sample in samples if sample["phase"] == "convergence"]
    if len(convergence) < 60:
        raise ValueError(
            f"Need at least 60 convergence samples, got {len(convergence)}"
        )
    left = np.asarray([sample["left_raw"] for sample in convergence], dtype=np.float64)
    right = np.asarray([sample["right_raw"] for sample in convergence], dtype=np.float64)
    values = np.column_stack([_ema(left, smoothing), _ema(right, smoothing)])
    target = np.asarray(
        [
            sample["right_target_deg"][0] - sample["left_target_deg"][0]
            for sample in convergence
        ],
        dtype=np.float64,
    )
    timestamps = np.asarray(
        [sample["timestamp_ns"] for sample in convergence], dtype=np.int64
    )
    blocks = (timestamps - timestamps.min()) // 1_000_000_000
    holdout = blocks % 5 == 4
    if int(np.count_nonzero(holdout)) < 12:
        holdout = np.arange(len(convergence)) % 5 == 4
    training = ~holdout
    features = _multi_polynomial_features(values, degree=2)
    penalty = np.eye(features.shape[1], dtype=np.float64) * 0.02
    penalty[0, 0] = 0.0

    def fit(indices: np.ndarray) -> np.ndarray:
        selected = features[indices]
        return np.linalg.solve(
            selected.T @ selected + penalty,
            selected.T @ target[indices],
        )

    coefficients = fit(training)
    training_error = np.abs(features[training] @ coefficients - target[training])
    median = float(np.median(training_error))
    mad = float(np.median(np.abs(training_error - median)))
    cutoff = max(0.5, median + 4.0 * max(mad, 0.05))
    clean_training = np.flatnonzero(training)[training_error <= cutoff]
    if len(clean_training) >= 48:
        coefficients = fit(clean_training)

    held_error = np.abs(features[holdout] @ coefficients - target[holdout])
    all_error = np.abs(features @ coefficients - target)
    clean_all = all_error <= max(
        0.5,
        float(np.median(all_error))
        + 4.0 * max(float(np.median(np.abs(all_error - np.median(all_error)))), 0.05),
    )
    if int(np.count_nonzero(clean_all)) >= 60:
        coefficients = fit(clean_all)
        all_error = np.abs(features @ coefficients - target)

    predicted = features @ coefficients
    return {
        "model": "stereo-degree2-ridge",
        "input_order": ["left_x", "left_y", "right_x", "right_y"],
        "feature_order": (
            ["1"]
            + ["left_x", "left_y", "right_x", "right_y"]
            + [
                "left_x2", "left_x_left_y", "left_x_right_x",
                "left_x_right_y", "left_y2", "left_y_right_x",
                "left_y_right_y", "right_x2", "right_x_right_y", "right_y2",
            ]
        ),
        "output": "right_yaw_minus_left_yaw_deg",
        "smoothing_alpha": smoothing,
        "coefficients": coefficients.tolist(),
        "sample_count": len(convergence),
        "retained_count": int(np.count_nonzero(clean_all)),
        "holdout_count": int(np.count_nonzero(holdout)),
        "held_out": {
            "mae_deg": float(np.mean(held_error)),
            "p95_deg": float(np.percentile(held_error, 95)),
        },
        "all_samples": {
            "mae_deg": float(np.mean(all_error)),
            "p95_deg": float(np.percentile(all_error, 95)),
        },
        "target_disparity_span_deg": float(np.ptp(target)),
        "predicted_disparity_span_deg": float(np.ptp(predicted)),
        "target_distance_range_m": [
            float(min(sample["distance_m"] for sample in convergence)),
            float(max(sample["distance_m"] for sample in convergence)),
        ],
    }


def apply_convergence_mapping(
    mapping: dict[str, Any],
    left_raw: tuple[float, float] | list[float] | np.ndarray,
    right_raw: tuple[float, float] | list[float] | np.ndarray,
) -> float:
    """Apply a fitted stereo model to one already-smoothed pair of eye signals."""
    values = np.asarray(
        [[left_raw[0], left_raw[1], right_raw[0], right_raw[1]]],
        dtype=np.float64,
    )
    features = _multi_polynomial_features(values, degree=2)
    coefficients = np.asarray(mapping["coefficients"], dtype=np.float64)
    if features.shape[1] != len(coefficients):
        raise ValueError(
            "Stereo calibration coefficient count does not match its feature order"
        )
    return float(features[0] @ coefficients)


def quaternion_yaw_pitch(quaternion: list[float]) -> tuple[float, float]:
    """Convert Virtual Desktop's XYZW eye orientation to yaw/pitch degrees."""
    xyz = np.asarray(quaternion[:3], dtype=np.float64)
    scalar = float(quaternion[3])
    forward = np.array([0.0, 0.0, -1.0], dtype=np.float64)
    intermediate = 2.0 * np.cross(xyz, forward)
    direction = forward + scalar * intermediate + np.cross(xyz, intermediate)
    yaw = np.degrees(np.arctan2(direction[0], -direction[2]))
    pitch = np.degrees(
        np.arctan2(direction[1], np.hypot(direction[0], direction[2]))
    )
    return float(yaw), float(pitch)


def _fit_factory_angles(
    samples: list[dict[str, Any]], eye: str
) -> dict[str, Any]:
    usable = [
        sample for sample in samples
        if sample["phase"] == "gaze"
        and "factory" in sample
        and bool(sample["factory"].get(f"{eye}_valid", False))
    ]
    if len(usable) < 60:
        raise ValueError(f"Only {len(usable)} valid factory {eye}-eye samples")
    raw = np.asarray(
        [
            quaternion_yaw_pitch(sample["factory"][f"{eye}_orientation"])
            for sample in usable
        ],
        dtype=np.float64,
    )
    target = np.asarray(
        [sample[f"{eye}_target_deg"] for sample in usable], dtype=np.float64
    )
    features = np.column_stack([np.ones(len(raw)), raw])
    timestamps = np.asarray(
        [sample["timestamp_ns"] for sample in usable], dtype=np.int64
    )
    blocks = (timestamps - timestamps.min()) // 1_000_000_000
    holdout = blocks % 5 == 4
    training = ~holdout
    penalty = np.diag([0.0, 0.001, 0.001])
    coefficients = np.linalg.solve(
        features[training].T @ features[training] + penalty,
        features[training].T @ target[training],
    )
    prediction = features[holdout] @ coefficients
    return {
        "model": "factory-yaw-pitch-affine",
        "coefficients": coefficients.tolist(),
        "input_order": ["1", "factory_yaw_deg", "factory_pitch_deg"],
        "output_order": ["target_yaw_deg", "target_pitch_deg"],
        "sample_count": len(usable),
        "holdout_count": int(np.count_nonzero(holdout)),
        "held_out": _error_metrics(prediction, target[holdout]),
        "native_range_deg": {
            "yaw": [float(np.min(raw[:, 0])), float(np.max(raw[:, 0]))],
            "pitch": [float(np.min(raw[:, 1])), float(np.max(raw[:, 1]))],
        },
    }


def evaluate_factory_baseline(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure Meta gaze and whether its binocular poses encode convergence."""
    left = _fit_factory_angles(samples, "left")
    right = _fit_factory_angles(samples, "right")
    convergence = [
        sample for sample in samples
        if sample["phase"] == "convergence"
        and "factory" in sample
        and bool(sample["factory"].get("left_valid", False))
        and bool(sample["factory"].get("right_valid", False))
    ]
    native_disparity = []
    target_disparity = []
    timestamps = []
    for sample in convergence:
        left_angles = quaternion_yaw_pitch(
            sample["factory"]["left_orientation"]
        )
        right_angles = quaternion_yaw_pitch(
            sample["factory"]["right_orientation"]
        )
        native_disparity.append(right_angles[0] - left_angles[0])
        target_disparity.append(
            sample["right_target_deg"][0] - sample["left_target_deg"][0]
        )
        timestamps.append(sample["timestamp_ns"])
    if len(native_disparity) < 60:
        raise ValueError(
            f"Only {len(native_disparity)} valid factory convergence samples"
        )
    native = np.asarray(native_disparity, dtype=np.float64)
    target = np.asarray(target_disparity, dtype=np.float64)
    times = np.asarray(timestamps, dtype=np.int64)
    blocks = (times - times.min()) // 1_000_000_000
    holdout = blocks % 5 == 4
    training = ~holdout
    features = np.column_stack([np.ones(len(native)), native])
    coefficients = np.linalg.lstsq(
        features[training], target[training], rcond=None
    )[0]
    held_error = np.abs(features[holdout] @ coefficients - target[holdout])
    correlation = float(np.corrcoef(native, target)[0, 1])
    if not np.isfinite(correlation):
        correlation = 0.0
    return {
        "left_gaze": left,
        "right_gaze": right,
        "convergence": {
            "model": "factory-disparity-affine",
            "coefficients": coefficients.tolist(),
            "sample_count": len(convergence),
            "holdout_count": int(np.count_nonzero(holdout)),
            "held_out": {
                "mae_deg": float(np.mean(held_error)),
                "p95_deg": float(np.percentile(held_error, 95)),
            },
            "native_target_correlation": correlation,
            "native_disparity_span_deg": float(np.ptp(native)),
            "target_disparity_span_deg": float(np.ptp(target)),
        },
    }


class _JsonObjectStream:
    def __init__(self) -> None:
        self.buffer = ""
        self.depth = 0
        self.in_string = False
        self.escape = False

    def feed(self, chunk: str) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        for character in chunk:
            if self.depth == 0 and character.isspace():
                continue
            self.buffer += character
            if self.in_string:
                if self.escape:
                    self.escape = False
                elif character == "\\":
                    self.escape = True
                elif character == '"':
                    self.in_string = False
            elif character == '"':
                self.in_string = True
            elif character == "{":
                self.depth += 1
            elif character == "}":
                self.depth -= 1
                if self.depth == 0 and self.buffer:
                    objects.append(json.loads(self.buffer))
                    self.buffer = ""
        return objects


@dataclass(frozen=True)
class TargetState:
    received_ns: int
    left_pitch: float
    left_yaw: float
    right_pitch: float
    right_yaw: float
    distance: float


class StereoEyeCalibrationController:
    """Own the official VR overlay session and fit a mapping for each eye."""

    def __init__(
        self,
        overlay_executable: str | Path,
        output_path: str | Path,
        gaze_seconds: int = 60,
        convergence_seconds: int = 80,
        port: int = 2425,
        openvr: bool = True,
    ) -> None:
        self.overlay_executable = Path(overlay_executable).resolve()
        if not self.overlay_executable.is_file():
            raise FileNotFoundError(
                f"BabbleCalibration executable not found: {self.overlay_executable}"
            )
        self.output_path = Path(output_path).resolve()
        self.gaze_seconds = gaze_seconds
        self.convergence_seconds = convergence_seconds
        self.port = port
        self.openvr = openvr
        self.phase = "starting"
        self.message = "Starting the in-VR calibrator..."
        self.samples: list[dict[str, Any]] = []
        self.latest_target: TargetState | None = None
        self.target_history: collections.deque[TargetState] = collections.deque(
            maxlen=4096
        )
        self.result: dict[str, Any] | None = None
        self._server: socket.socket | None = None
        self._client: socket.socket | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._process_output = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._stopping = False

    def start(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", self.port))
        self._server.listen(1)
        self._server.settimeout(20.0)
        arguments = [str(self.overlay_executable)]
        arguments.append("--use-openvr" if self.openvr else "--use-debug")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_log = self.output_path.parent / "babble-calibration-overlay.log"
        self._process_output = overlay_log.open("w", encoding="utf-8")
        self._process = subprocess.Popen(
            arguments,
            cwd=str(self.overlay_executable.parent),
            creationflags=creation_flags,
            stdout=self._process_output,
            stderr=subprocess.STDOUT,
        )
        print(
            f"BABBLE_OVERLAY_STARTED mode={'openvr' if self.openvr else 'debug'} "
            f"log={overlay_log}"
        )
        self._thread = threading.Thread(
            target=self._connection_worker,
            name="babble-calibration-bridge",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _duration(seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _send(self, packet_name: str, packet_data: dict[str, Any]) -> None:
        payload = json.dumps(
            {"PacketName": packet_name, "PacketData": packet_data},
            separators=(",", ":"),
        ).encode("utf-8")
        with self._lock:
            if self._client is None:
                raise ConnectionError("BabbleCalibration is not connected")
            self._client.sendall(payload)

    def _send_routine(self, name: str, seconds: int) -> None:
        self._send(
            "RunVariableLenghtRoutinePacket",
            {"RoutineName": name, "Time": self._duration(seconds)},
        )

    def _connection_worker(self) -> None:
        try:
            assert self._server is not None
            client, _address = self._server.accept()
            client.settimeout(1.0)
            with self._lock:
                self._client = client
                self.phase = "initializing_overlay"
                self.message = "VR calibrator connected; waiting for its routine handler..."
            print("BABBLE_OVERLAY_CONNECTED")
            # EventDrivenTcpClient begins receiving before GodotPacketHandler is
            # registered during MainScene._Ready(). Sending in the accept call's
            # wakeup can therefore be parsed and silently dropped. Give Godot's
            # main scene a bounded moment to finish registering the adapter.
            time.sleep(1.25)
            self._send_routine("gazetutorialshort", 3600)
            with self._lock:
                self.phase = "ready_gaze"
                self.message = "In VR: read the gaze tutorial. Press Space when ready."
            print("BABBLE_ROUTINE_READY name=gazetutorialshort key=Space")
            decoder = _JsonObjectStream()
            while not self._stopping:
                try:
                    chunk = client.recv(65536)
                except socket.timeout:
                    continue
                except (ConnectionResetError, ConnectionAbortedError):
                    with self._lock:
                        completed = self.phase == "done"
                    if self._stopping or completed:
                        break
                    raise
                if not chunk:
                    if not self._stopping and self.phase != "done":
                        raise ConnectionError("BabbleCalibration disconnected")
                    break
                for packet in decoder.feed(chunk.decode("utf-8")):
                    self._handle_packet(packet)
        except Exception as error:  # Keep the camera receiver alive for diagnosis.
            with self._lock:
                if not self._stopping and self.phase != "done":
                    self.phase = "error"
                    self.message = f"VR calibration error: {error}"

    def _handle_packet(self, packet: dict[str, Any]) -> None:
        name = packet.get("PacketName")
        data = packet.get("PacketData") or {}
        if name == "HmdPositionalDataPacket":
            target = TargetState(
                received_ns=time.monotonic_ns(),
                left_pitch=float(data.get("LeftEyePitch", 0.0)),
                left_yaw=float(data.get("LeftEyeYaw", 0.0)),
                right_pitch=float(data.get("RightEyePitch", 0.0)),
                right_yaw=float(data.get("RightEyeYaw", 0.0)),
                distance=float(data.get("RoutineDistance", 0.0)),
            )
            with self._lock:
                self.latest_target = target
                self.target_history.append(target)
        elif name == "RoutineFinishedPacket":
            routine = str(data.get("RoutineName", "")).lower()
            if routine == "gaze" and self.phase == "gaze":
                with self._lock:
                    self.phase = "ready_convergence"
                    self.message = (
                        "Gaze capture complete. Read the convergence tutorial; "
                        "press Space when ready."
                    )
                self._send_routine("convergencetutorial", 3600)
            elif routine == "convergence" and self.phase == "convergence":
                self._fit_and_save()

    def handle_key(self, key: str, factory_ready: bool = True) -> None:
        normalized = key.lower()
        with self._lock:
            phase = self.phase
        if normalized == "r" and phase == "ready_gaze":
            self._send_routine("gazetutorialshort", 3600)
            print("BABBLE_ROUTINE_RESENT name=gazetutorialshort")
            return
        if normalized == "r" and phase == "ready_convergence":
            self._send_routine("convergencetutorial", 3600)
            print("BABBLE_ROUTINE_RESENT name=convergencetutorial")
            return
        if normalized not in (" ", "\r", "\n"):
            return
        if phase in ("ready_gaze", "ready_convergence") and not factory_ready:
            with self._lock:
                self.message = (
                    "Waiting for valid changing Meta eye poses. Move your eyes, "
                    "then press Space again."
                )
            print("FACTORY_EYE_PREFLIGHT_WAITING")
            return
        if phase == "ready_gaze":
            with self._lock:
                self.phase = "gaze"
                self.latest_target = None
                self.target_history.clear()
                self.message = "Follow the target naturally while moving your head slowly."
            self._send_routine("gaze", self.gaze_seconds)
            print(f"BABBLE_ROUTINE_STARTED name=gaze seconds={self.gaze_seconds}")
        elif phase == "ready_convergence":
            with self._lock:
                self.phase = "convergence"
                self.latest_target = None
                self.target_history.clear()
                self.message = (
                    "Keep both eyes on the target. Slowly turn/nod your head in a wide figure-eight "
                    "while it moves near and far; let your eyes counter-rotate to hold fixation."
                )
            self._send_routine("convergence", self.convergence_seconds)
            print(
                "BABBLE_ROUTINE_STARTED name=convergence "
                f"seconds={self.convergence_seconds} instruction=slow_head_figure_eight"
            )

    def target_at(
        self, timestamp_ns: int, max_distance_ns: int = 100_000_000
    ) -> TargetState | None:
        """Return/interpolate the VR target at a capture-time timestamp."""
        with self._lock:
            history = list(self.target_history)
        if not history:
            return None
        timestamp_ns = int(timestamp_ns)
        after_index = next(
            (
                index
                for index, target in enumerate(history)
                if target.received_ns >= timestamp_ns
            ),
            len(history),
        )
        before = history[after_index - 1] if after_index > 0 else None
        after = history[after_index] if after_index < len(history) else None
        if before is not None and after is not None:
            before_delta = timestamp_ns - before.received_ns
            after_delta = after.received_ns - timestamp_ns
            if before_delta <= max_distance_ns and after_delta <= max_distance_ns:
                span = after.received_ns - before.received_ns
                fraction = 0.0 if span <= 0 else before_delta / span

                def blend(first: float, second: float) -> float:
                    return first + (second - first) * fraction

                return TargetState(
                    received_ns=timestamp_ns,
                    left_pitch=blend(before.left_pitch, after.left_pitch),
                    left_yaw=blend(before.left_yaw, after.left_yaw),
                    right_pitch=blend(before.right_pitch, after.right_pitch),
                    right_yaw=blend(before.right_yaw, after.right_yaw),
                    distance=blend(before.distance, after.distance),
                )
        nearest = min(history, key=lambda target: abs(target.received_ns - timestamp_ns))
        if abs(nearest.received_ns - timestamp_ns) <= max_distance_ns:
            return nearest
        return None

    def add_sample(
        self,
        prediction: OpenSourcePrediction,
        timestamp_ns: int,
        factory_sample: dict[str, object] | None = None,
    ) -> None:
        with self._lock:
            phase = self.phase
            target = self.latest_target
        if phase not in ("gaze", "convergence") or target is None:
            return
        if time.monotonic_ns() - target.received_ns > 200_000_000:
            return
        sample = {
            "timestamp_ns": int(timestamp_ns),
            "phase": phase,
            "distance_m": target.distance,
            "left_raw": [float(prediction.left_eye[3]), float(prediction.left_eye[4])],
            "right_raw": [float(prediction.right_eye[3]), float(prediction.right_eye[4])],
            "left_target_deg": [target.left_yaw, target.left_pitch],
            "right_target_deg": [target.right_yaw, target.right_pitch],
        }
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
                "left_position": list(
                    factory_sample.get("leftEyePosition", [0.0] * 3)
                ),
                "right_position": list(
                    factory_sample.get("rightEyePosition", [0.0] * 3)
                ),
            }
        with self._lock:
            self.samples.append(sample)

    def _fit_and_save(self) -> None:
        with self._lock:
            self.phase = "fitting"
            self.message = "Fitting and validating independent eye mappings..."
            samples = list(self.samples)
        try:
            gaze_samples = [
                sample for sample in samples if sample["phase"] == "gaze"
            ]
            left, _left_prediction, _left_target = fit_eye_mapping(
                gaze_samples, "left"
            )
            right, _right_prediction, _right_target = fit_eye_mapping(
                gaze_samples, "right"
            )
            stereo_metrics = fit_convergence_mapping(samples)
            factory_baseline = None
            if sum("factory" in sample for sample in samples) >= 60:
                try:
                    factory_baseline = evaluate_factory_baseline(samples)
                except ValueError as error:
                    print(f"FACTORY_BASELINE_INCOMPLETE reason={error}")
            gaze_pass = bool(
                left["held_out"]["angular_mae_deg"] <= 5.0
                and right["held_out"]["angular_mae_deg"] <= 5.0
                and left["held_out"]["angular_p95_deg"] <= 10.0
                and right["held_out"]["angular_p95_deg"] <= 10.0
            )
            convergence_pass = bool(
                stereo_metrics["held_out"]["mae_deg"] <= 1.5
                and stereo_metrics["held_out"]["p95_deg"] <= 3.0
                and stereo_metrics["target_disparity_span_deg"] >= 3.0
            )
            quality_status = (
                "pass" if gaze_pass and convergence_pass
                else "partial_pass" if convergence_pass
                else "fail"
            )
            result = {
                "format": "qpro-stereo-eye-calibration-v2",
                "created_unix_ns": time.time_ns(),
                "source": "EyeTrackVR Beta 5 NEXT + BabbleCalibration 1.0.8",
                "camera0_horizontal_sign_corrected": True,
                "sample_count": len(samples),
                "factory_sample_count": sum(
                    "factory" in sample for sample in samples
                ),
                "factory_valid_binocular_count": sum(
                    bool(sample.get("factory", {}).get("left_valid", False))
                    and bool(sample.get("factory", {}).get("right_valid", False))
                    for sample in samples
                ),
                "phases": {
                    name: sum(sample["phase"] == name for sample in samples)
                    for name in ("gaze", "convergence")
                },
                "left": left,
                "right": right,
                "stereo": stereo_metrics,
                "factory_baseline": factory_baseline,
                "quality_gate": {
                    "status": quality_status,
                    "gaze_pass": gaze_pass,
                    "convergence_pass": convergence_pass,
                    "gaze_limits_deg": {"angular_mae": 5.0, "angular_p95": 10.0},
                    "convergence_limits_deg": {"mae": 1.5, "p95": 3.0},
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
                    f"Saved ({quality_status}). Gaze MAE: "
                    f"L {left['held_out']['angular_mae_deg']:.1f} deg, "
                    f"R {right['held_out']['angular_mae_deg']:.1f} deg; "
                    f"convergence {stereo_metrics['held_out']['mae_deg']:.2f} deg. "
                    "Press Q."
                )
            try:
                self._send_routine("close", 1)
            except OSError:
                # Closing the overlay is expected to tear down its socket.
                pass
        except Exception as error:
            with self._lock:
                self.phase = "error"
                self.message = f"Calibration fit failed: {error}"

    def mapped_gaze(
        self, prediction: OpenSourcePrediction
    ) -> tuple[tuple[float, float], tuple[float, float]] | None:
        with self._lock:
            result = self.result
        if result is None:
            return None
        left = apply_eye_mapping(
            result["left"], float(prediction.left_eye[3]), float(prediction.left_eye[4])
        )
        right = apply_eye_mapping(
            result["right"], float(prediction.right_eye[3]), float(prediction.right_eye[4])
        )
        return left, right

    def decorate(
        self, image: np.ndarray, prediction: OpenSourcePrediction
    ) -> np.ndarray:
        with self._lock:
            phase = self.phase
            message = self.message
            count = len(self.samples)
            target = self.latest_target
        cv2.rectangle(image, (0, 824), (image.shape[1] - 1, 879), (12, 12, 12), -1)
        color = (80, 235, 120) if phase == "done" else (50, 175, 255)
        if phase == "error":
            color = (70, 70, 255)
        cv2.putText(
            image, f"STEREO CALIBRATION [{phase.upper()}] | {count} samples",
            (20, 846), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA,
        )
        cv2.putText(
            image, message, (20, 870), cv2.FONT_HERSHEY_SIMPLEX, 0.47,
            (220, 220, 220), 1, cv2.LINE_AA,
        )
        if target is not None and phase in ("gaze", "convergence"):
            target_text = (
                f"target L({target.left_yaw:+.1f},{target.left_pitch:+.1f}) "
                f"R({target.right_yaw:+.1f},{target.right_pitch:+.1f}) deg "
                f"distance {target.distance:.2f} m"
            )
            cv2.putText(
                image, target_text, (720, 846), cv2.FONT_HERSHEY_SIMPLEX,
                0.43, (180, 180, 180), 1, cv2.LINE_AA,
            )
        mapped = self.mapped_gaze(prediction)
        if mapped is not None:
            cv2.putText(
                image,
                f"mapped L yaw/pitch {mapped[0][0]:+.1f}/{mapped[0][1]:+.1f}  "
                f"R {mapped[1][0]:+.1f}/{mapped[1][1]:+.1f} deg",
                (720, 870), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (80, 235, 120),
                1, cv2.LINE_AA,
            )
        return image

    def close(self) -> None:
        self._stopping = True
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        if self._process_output is not None:
            self._process_output.close()
            self._process_output = None
