#!/usr/bin/env python3
"""Live preview and fail-safe VRCFT output for the stereo tongue model."""

from __future__ import annotations

import json
import time
import socket
import struct
import subprocess
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from export_tongue_onnx import ONNX_FORMAT, checkpoint_digest, onnx_path

# PyTorch is imported only on the fallback path. Normally each checkpoint's
# ONNX twin runs through ONNX Runtime + DirectML, so the live receiver never
# loads PyTorch/CUDA (~1.1 GB -> ~0.3 GB resident, far less CPU per frame).


TONGUE_PACKET = struct.Struct("<4sBBH12f")
TONGUE_MAGIC = b"QPTO"
TONGUE_VERSION = 1


def vrcft_tongue_values(
    prediction: "TonguePrediction", target_names: list[str]
) -> np.ndarray:
    """Map ten model heads to VRCFT's twelve detailed tongue expressions."""
    values = {
        name: float(prediction.values[index])
        for index, name in enumerate(target_names)
    }
    if not prediction.visible:
        return np.zeros(12, dtype=np.float32)
    horizontal = float(np.clip(values.get("horizontal", 0.0), -1.0, 1.0))
    vertical = float(np.clip(values.get("vertical", 0.0), -1.0, 1.0))
    twist = float(np.clip(values.get("twist", 0.0), -1.0, 1.0))
    tongue_out = max(
        float(np.clip(prediction.fused_visibility, 0.0, 1.0)),
        float(np.clip(values.get("extension", 0.0), 0.0, 1.0)),
    )
    return np.asarray(
        [
            tongue_out,
            max(vertical, 0.0),
            max(-vertical, 0.0),
            max(-horizontal, 0.0),
            max(horizontal, 0.0),
            float(np.clip(values.get("roll", 0.0), 0.0, 1.0)),
            float(np.clip(values.get("bend_down", 0.0), 0.0, 1.0)),
            float(np.clip(values.get("curl_up", 0.0), 0.0, 1.0)),
            float(np.clip(values.get("squish", 0.0), 0.0, 1.0)),
            float(np.clip(values.get("flat", 0.0), 0.0, 1.0)),
            max(-twist, 0.0),
            max(twist, 0.0),
        ],
        dtype=np.float32,
    )


def encode_tongue_packet(values: np.ndarray, enabled: bool) -> bytes:
    values = np.asarray(values, dtype=np.float32)
    if values.shape != (12,):
        raise ValueError("A VRCFT tongue packet needs exactly twelve values")
    return TONGUE_PACKET.pack(
        TONGUE_MAGIC, TONGUE_VERSION, int(enabled), 0,
        *[float(np.clip(value, 0.0, 1.0)) for value in values],
    )


class TongueBroadcaster:
    """Opt-in UDP override; disabled or stale packets restore stock tracking."""

    def __init__(self, port: int = 27276, *, enabled: bool = False) -> None:
        self.enabled = bool(enabled)
        self._address = ("127.0.0.1", int(port))
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._last_values: np.ndarray | None = None
        self._last_sent = 0.0
        self._minimum_interval = 1.0 / 24.0
        self._keepalive_interval = 0.20

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        if not self.enabled:
            self._send(np.zeros(12, dtype=np.float32), enabled=False)
        return self.enabled

    def send_prediction(
        self, prediction: "TonguePrediction", target_names: list[str]
    ) -> None:
        if not self.enabled:
            return
        values = vrcft_tongue_values(prediction, target_names)
        now = time.perf_counter()
        elapsed = now - self._last_sent
        if elapsed < self._minimum_interval:
            return
        changed = (
            self._last_values is None
            or float(np.max(np.abs(values - self._last_values))) >= 0.015
        )
        if not changed and elapsed < self._keepalive_interval:
            return
        self._send(values, enabled=True)
        self._last_values = values.copy()
        self._last_sent = now

    def _send(self, values: np.ndarray, *, enabled: bool) -> None:
        self._socket.sendto(encode_tongue_packet(values, enabled), self._address)

    def close(self) -> None:
        try:
            self._send(np.zeros(12, dtype=np.float32), enabled=False)
        finally:
            self._socket.close()


@dataclass(frozen=True)
class TonguePrediction:
    values: np.ndarray
    native_tongue_out: float
    fused_visibility: float
    visible: bool
    inference_ms: float
    pipeline_ms: float = 0.0
    dropped_frames: int = 0


@dataclass(frozen=True)
class _LoadedModel:
    infer: Callable[[np.ndarray], np.ndarray]  # (1, 2, S, S) float32 -> head values
    target_names: list[str]
    image_size: int
    visibility_gate: dict


class _DirectMLUnavailable(Exception):
    """DirectML cannot run on this PC; converting the model again would not help."""


def _directml_options(ort) -> object:
    options = ort.SessionOptions()
    # DirectML requires sequential execution without memory-pattern planning.
    options.enable_mem_pattern = False
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    # The GPU does the work; a single non-spinning CPU thread keeps CPU use low.
    options.intra_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.log_severity_level = 3
    return options


def _open_onnx_twin(ort, checkpoint: Path, digest: str) -> _LoadedModel | None:
    """The checkpoint's current ONNX twin on DirectML, or None if missing/stale."""
    target = onnx_path(checkpoint)
    if not target.exists():
        return None
    try:
        session = ort.InferenceSession(
            str(target), _directml_options(ort),
            providers=[("DmlExecutionProvider", {"performance_preference": "high_performance"})],
        )
    except Exception:  # noqa: BLE001 - an unreadable twin is simply converted again
        return None
    if "DmlExecutionProvider" not in session.get_providers():
        raise _DirectMLUnavailable("DirectML did not initialize")
    metadata = session.get_modelmeta().custom_metadata_map
    if (metadata.get("qproTongueOnnxFormat") != ONNX_FORMAT
            or metadata.get("sourceSha256") != digest):
        return None
    input_name = session.get_inputs()[0].name

    def infer(cameras: np.ndarray) -> np.ndarray:
        return session.run(None, {input_name: cameras})[0][0]

    return _LoadedModel(
        infer,
        json.loads(metadata["targetNames"]),
        int(metadata["imageSize"]),
        json.loads(metadata.get("visibilityGate", "{}")),
    )


def _convert_to_onnx(checkpoints: list[Path]) -> bool:
    """Convert in a short-lived helper so this process never imports PyTorch."""
    script = Path(__file__).with_name("export_tongue_onnx.py")
    names = ", ".join(checkpoint.name for checkpoint in checkpoints)
    print(f"Preparing {names} for DirectML (one-time conversion)...", flush=True)
    try:
        completed = subprocess.run(
            [sys.executable, str(script), *map(str, checkpoints)], timeout=300
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"ONNX conversion did not finish: {error}", flush=True)
        return False
    return completed.returncode == 0


def _load_directml_models(checkpoints: list[Path]) -> list[_LoadedModel] | None:
    """DirectML models for every checkpoint (converting as needed), or None."""
    try:
        import onnxruntime as ort
    except ImportError:
        return None
    if "DmlExecutionProvider" not in ort.get_available_providers():
        return None
    digests = [checkpoint_digest(checkpoint) for checkpoint in checkpoints]
    try:
        models = [_open_onnx_twin(ort, c, d) for c, d in zip(checkpoints, digests)]
        stale = [c for c, model in zip(checkpoints, models) if model is None]
        if stale:
            if not _convert_to_onnx(stale):
                return None
            models = [
                model or _open_onnx_twin(ort, c, d)
                for c, d, model in zip(checkpoints, digests, models)
            ]
    except _DirectMLUnavailable as error:
        print(f"DirectML is unavailable ({error}); using PyTorch.", flush=True)
        return None
    return models if all(model is not None for model in models) else None


def _load_torch_model(checkpoint: Path, device) -> _LoadedModel:
    import torch

    from train_tongue_model import create_model

    state = torch.load(checkpoint, map_location="cpu", weights_only=False)
    target_names = list(state["targetNames"])
    model = create_model(str(state.get("architecture", "legacy-late-fusion-v1")), target_names)
    model.load_state_dict(state["modelState"])
    model.to(device).eval()

    def infer(cameras: np.ndarray) -> np.ndarray:
        with torch.inference_mode():
            return model(torch.from_numpy(cameras).to(device))[0].float().cpu().numpy()

    return _LoadedModel(
        infer, target_names, int(state["imageSize"]), state.get("visibilityGate", {})
    )


class LiveTongueModelPreview:
    def __init__(
        self,
        checkpoint_path: str | Path,
        device_name: str = "auto",
        direction_checkpoint_path: str | Path | None = None,
        smoothing: float = 0.35,
        visibility_mode: str = "weighted",
        camera_weight: float | None = None,
    ) -> None:
        self.checkpoint_path = Path(checkpoint_path).resolve()
        self.direction_checkpoint_path = (
            Path(direction_checkpoint_path).resolve() if direction_checkpoint_path else None
        )
        checkpoints = [self.checkpoint_path]
        if self.direction_checkpoint_path is not None:
            checkpoints.append(self.direction_checkpoint_path)
        # "auto" prefers the ONNX twins on DirectML (same outputs, no PyTorch in
        # this process) and otherwise falls back to PyTorch on CUDA, else CPU.
        # "dml" requires DirectML; "cuda:0"/"cpu" keep the PyTorch path. Both
        # ensemble halves always share one backend.
        models = None
        if device_name in ("auto", "dml"):
            models = _load_directml_models(checkpoints)
            if models is None and device_name == "dml":
                raise RuntimeError("DirectML tongue inference is unavailable on this PC.")
        if models is not None:
            self.device = "DirectML (ONNX Runtime)"
        else:
            import torch

            if device_name in ("auto", "dml"):
                device_name = "cuda:0" if torch.cuda.is_available() else "cpu"
            self.device = torch.device(device_name)
            models = [_load_torch_model(checkpoint, self.device) for checkpoint in checkpoints]
        gate_model = models[0]
        self.target_names = gate_model.target_names
        self.image_size = gate_model.image_size
        self._infer_gate = gate_model.infer
        self._infer_direction: Callable[[np.ndarray], np.ndarray] | None = None
        self.direction_image_size = self.image_size
        if len(models) > 1:
            if models[1].target_names != self.target_names:
                raise ValueError(
                    "Visibility and direction checkpoints use different target schemas"
                )
            self._infer_direction = models[1].infer
            self.direction_image_size = models[1].image_size
        gate = gate_model.visibility_gate
        self.camera_weight = float(
            gate.get("cameraWeight", 0.5) if camera_weight is None else camera_weight
        )
        self.threshold = float(gate.get("threshold", 0.44))
        self.smoothing = float(np.clip(smoothing, 0.05, 1.0))
        if visibility_mode not in {"camera", "native", "weighted", "agreement"}:
            raise ValueError(f"Unsupported tongue visibility mode: {visibility_mode}")
        self.visibility_mode = visibility_mode
        self._smoothed: np.ndarray | None = None
        self._visible_latched = False

    @staticmethod
    def _inputs(strip: np.ndarray, image_size: int) -> np.ndarray:
        cameras = np.empty((1, 2, image_size, image_size), dtype=np.float32)
        for view in range(2):
            panel = strip[:, view * 400:(view + 1) * 400]
            cameras[0, view] = cv2.resize(
                panel, (image_size, image_size), interpolation=cv2.INTER_AREA
            ).astype(np.float32) / 255.0
        return cameras

    def predict(
        self,
        strip: np.ndarray,
        factory_sample: dict[str, object] | None,
        factory_names: list[str],
    ) -> TonguePrediction:
        if strip.shape not in ((400, 800), (400, 1200)):
            raise ValueError(
                "Tongue preview requires cameras 2 and 3 in a 400x800 mouth "
                f"or 400x1200 face strip, got {strip.shape}"
            )
        inputs = self._inputs(strip, self.image_size)
        started = time.perf_counter()
        values = self._infer_gate(inputs)
        if self._infer_direction is not None:
            visibility_index = self.target_names.index("visibility")
            gate_visibility = float(values[visibility_index])
            values = self._infer_direction(
                self._inputs(strip, self.direction_image_size)
            )
            values[visibility_index] = gate_visibility
        inference_ms = (time.perf_counter() - started) * 1000.0
        self._smoothed = (
            values
            if self._smoothed is None
            else self.smoothing * values + (1.0 - self.smoothing) * self._smoothed
        )
        native = 0.0
        if factory_sample is not None and "TongueOut" in factory_names:
            native = float(factory_sample["values"][factory_names.index("TongueOut")])
        visibility = float(self._smoothed[self.target_names.index("visibility")])
        if self.visibility_mode == "camera":
            fused = visibility
        elif self.visibility_mode == "native":
            fused = native
        elif self.visibility_mode == "agreement":
            fused = min(visibility, native)
        else:
            fused = self.camera_weight * visibility + (1.0 - self.camera_weight) * native
        self._visible_latched = (
            fused >= self.threshold - 0.08
            if self._visible_latched
            else fused >= self.threshold
        )
        return TonguePrediction(
            values=self._smoothed.copy(),
            native_tongue_out=native,
            fused_visibility=fused,
            visible=self._visible_latched,
            inference_ms=inference_ms,
        )

    def render(
        self, prediction: TonguePrediction, *, output_enabled: bool = False
    ) -> np.ndarray:
        image = np.zeros((760, 1100, 3), dtype=np.uint8)
        cv2.putText(image, "Quest Pro personalized stereo tongue preview", (24, 42),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.86, (240, 240, 240), 2, cv2.LINE_AA)
        output_text = (
            "EXPERIMENTAL VRCFT TONGUE OUTPUT ON - T disables immediately"
            if output_enabled
            else "SAFE MODE - stock Virtual Desktop tongue active; T enables experiment"
        )
        cv2.putText(
            image, output_text, (24, 76), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
            (80, 235, 120) if output_enabled else (50, 175, 255), 1, cv2.LINE_AA,
        )
        status = "TONGUE VISIBLE" if prediction.visible else "TONGUE RETRACTED"
        color = (80, 235, 120) if prediction.visible else (170, 170, 170)
        cv2.putText(image, status, (24, 118), cv2.FONT_HERSHEY_SIMPLEX, 0.68,
                    color, 2, cv2.LINE_AA)
        visibility = float(prediction.values[self.target_names.index("visibility")])
        summary = (
            f"camera {visibility:.2f}  native {prediction.native_tongue_out:.2f}  "
            f"{self.visibility_mode} {prediction.fused_visibility:.2f} / threshold {self.threshold:.2f}  "
            f"infer {prediction.inference_ms:.1f} ms  "
            f"pipeline {prediction.pipeline_ms:.1f} ms  dropped {prediction.dropped_frames}"
        )
        cv2.putText(image, summary, (24, 151), cv2.FONT_HERSHEY_SIMPLEX, 0.49,
                    (190, 220, 255), 1, cv2.LINE_AA)
        if self.direction_checkpoint_path is not None:
            cv2.putText(
                image,
                "ENSEMBLE: clean manual visibility gate + dense motion directions",
                (24, 177), cv2.FONT_HERSHEY_SIMPLEX, 0.46,
                (120, 225, 255), 1, cv2.LINE_AA,
            )
        bar_x, bar_width = 260, 560
        for row, (name, value) in enumerate(zip(self.target_names, prediction.values)):
            y = 205 + row * 46
            cv2.putText(image, name, (24, y + 9), cv2.FONT_HERSHEY_SIMPLEX, 0.52,
                        (225, 225, 225), 1, cv2.LINE_AA)
            cv2.rectangle(image, (bar_x, y - 9), (bar_x + bar_width, y + 15),
                          (55, 55, 55), 1)
            if name in {"horizontal", "vertical", "twist"}:
                center = bar_x + bar_width // 2
                cv2.line(image, (center, y - 8), (center, y + 14), (90, 90, 90), 1)
                endpoint = center + round((bar_width / 2) * float(value))
                cv2.rectangle(image, (min(center, endpoint), y - 5),
                              (max(center, endpoint), y + 11), (245, 90, 225), -1)
            else:
                endpoint = bar_x + round(bar_width * float(np.clip(value, 0, 1)))
                cv2.rectangle(image, (bar_x, y - 5), (endpoint, y + 11),
                              (80, 235, 120), -1)
            cv2.putText(image, f"{float(value):+.2f}", (845, y + 9),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.49, (220, 220, 220), 1, cv2.LINE_AA)
        cv2.putText(
            image,
            "T toggles VRCFT output | Q quits and restores stock tongue automatically.",
            (24, 730), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (170, 170, 170), 1, cv2.LINE_AA,
        )
        return image


class TongueInferenceWorker:
    """Runs inference latest-frame-first so brief stalls cannot build latency."""

    def __init__(
        self,
        preview: LiveTongueModelPreview,
        broadcaster: TongueBroadcaster,
    ) -> None:
        self.preview = preview
        self.broadcaster = broadcaster
        self._condition = threading.Condition()
        self._pending: tuple[
            np.ndarray, dict[str, object] | None, list[str], float
        ] | None = None
        self._latest: tuple[TonguePrediction, np.ndarray] | None = None
        self._error: BaseException | None = None
        self._running = True
        self._dropped_frames = 0
        self._thread = threading.Thread(
            target=self._run, name="tongue-inference", daemon=True
        )
        self._thread.start()

    def submit(
        self,
        strip: np.ndarray,
        factory_sample: dict[str, object] | None,
        factory_names: list[str],
    ) -> None:
        with self._condition:
            if self._pending is not None:
                self._dropped_frames += 1
            # The ndarray keeps its immutable payload bytes alive. Replacing
            # this one-slot pending item intentionally discards stale work.
            self._pending = (
                strip, factory_sample, list(factory_names), time.perf_counter()
            )
            self._condition.notify()

    def latest(self) -> tuple[TonguePrediction | None, np.ndarray | None]:
        with self._condition:
            if self._error is not None:
                raise RuntimeError("Tongue inference worker failed") from self._error
            if self._latest is None:
                return None, None
            return self._latest

    def close(self) -> None:
        with self._condition:
            self._running = False
            self._condition.notify_all()
        # Startup-path tests intentionally replace Thread.start; a receiver
        # failure before connection must still clean up without joining a
        # thread that never began.
        if self._thread.ident is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    while self._running and self._pending is None:
                        self._condition.wait()
                    if not self._running:
                        return
                    strip, factory_sample, factory_names, submitted_at = self._pending
                    self._pending = None
                    dropped = self._dropped_frames
                prediction = self.preview.predict(
                    strip, factory_sample, factory_names
                )
                prediction = replace(
                    prediction,
                    pipeline_ms=(time.perf_counter() - submitted_at) * 1000.0,
                    dropped_frames=dropped,
                )
                self.broadcaster.send_prediction(
                    prediction, self.preview.target_names
                )
                image = self.preview.render(
                    prediction, output_enabled=self.broadcaster.enabled
                )
                with self._condition:
                    self._latest = (prediction, image)
        except BaseException as error:
            with self._condition:
                self._error = error
                self._running = False
                self._condition.notify_all()
