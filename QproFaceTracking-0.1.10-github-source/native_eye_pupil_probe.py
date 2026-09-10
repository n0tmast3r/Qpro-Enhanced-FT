#!/usr/bin/env python3
"""Display Meta's decoded per-eye neural pupil estimates before gaze fusion."""

from __future__ import annotations

import argparse
import base64
import collections
import json
import queue
import re
import struct
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from eye_signal_filter import IndependentEyeFilter
from label_capture import LabelSidecarRecorder
from native_eye_probe import put_text


ENGINE_PATH = "/odm/lib64/libtrackingengines.so"
EXPECTED_ENGINE_SIZE = 47_724_232
TRACE_ROOT = "/sys/kernel/tracing"
TRACE_INSTANCE = "qpro_neural_pupil"
TRACE_GROUP = "qpro_neural_pupil"

# SocialGazeDNN::forward, after all output tensors have been decoded and before
# MLSocialGaze copies them into EyeData or gaze/convergence detectors run.
PROBE_OFFSET = 0xB4DE78

# The model's own postprocessing parameters from Seacliff_V1_5 config.json.
LEFT_MEAN = np.asarray(
    (-0.0017055189605983, 0.0045290040805944, -0.01040792463589),
    dtype=np.float64,
)
LEFT_SCALE = np.asarray(
    (0.0035429674058232, 0.0050770516145521, 0.0042806321969521),
    dtype=np.float64,
)
RIGHT_MEAN = np.asarray(
    (0.00046130682644319, 0.0017377919915684, -0.010235720690707),
    dtype=np.float64,
)
RIGHT_SCALE = np.asarray(
    (0.0047817454012475, 0.0055140730704888, 0.0051256563049549),
    dtype=np.float64,
)

TRACE_SAMPLE = re.compile(
    r"(?P<time>\d+\.\d+): neural_pupil: .*?"
    r"lx=0x(?P<lx>[0-9a-fA-F]+) ly=0x(?P<ly>[0-9a-fA-F]+) "
    r"lz=0x(?P<lz>[0-9a-fA-F]+) rx=0x(?P<rx>[0-9a-fA-F]+) "
    r"ry=0x(?P<ry>[0-9a-fA-F]+) rz=0x(?P<rz>[0-9a-fA-F]+) "
    r"lax=0x(?P<lax>[0-9a-fA-F]+) lay=0x(?P<lay>[0-9a-fA-F]+) "
    r"laz=0x(?P<laz>[0-9a-fA-F]+) rax=0x(?P<rax>[0-9a-fA-F]+) "
    r"ray=0x(?P<ray>[0-9a-fA-F]+) raz=0x(?P<raz>[0-9a-fA-F]+)"
)


def float_from_trace_hex(value: str) -> float:
    return struct.unpack("<f", struct.pack("<I", int(value, 16)))[0]


@dataclass(frozen=True)
class PupilSample:
    # Estimated PC monotonic time at which the headset produced this sample.
    # This is deliberately not the later time at which ADB delivered the line.
    pc_monotonic_ns: int
    arrival_monotonic_ns: int
    kernel_time_s: float
    left_pupil_3d: tuple[float, float, float]
    right_pupil_3d: tuple[float, float, float]
    left_model_axis: tuple[float, float, float]
    right_model_axis: tuple[float, float, float]


class KernelToPcMonotonicClock:
    """Translate headset kernel timestamps onto the PC monotonic clock.

    ADB trace delivery is bursty, but can only deliver a trace line after it was
    produced.  The lowest recent (arrival - kernel) offset is therefore the
    best available estimate of the two clocks' offset; larger values are USB
    queueing delay, not later capture times.
    """

    def __init__(self, window: int = 1024) -> None:
        if window < 1:
            raise ValueError("window must be positive")
        self._offsets: collections.deque[int] = collections.deque(maxlen=window)

    def translate(self, kernel_time_s: float, arrival_monotonic_ns: int) -> int:
        kernel_ns = round(float(kernel_time_s) * 1_000_000_000)
        self._offsets.append(int(arrival_monotonic_ns) - kernel_ns)
        return kernel_ns + min(self._offsets)

    @property
    def estimated_offset_ns(self) -> int | None:
        return min(self._offsets) if self._offsets else None


class NeuralPupilReader:
    def __init__(self, adb: str) -> None:
        self.adb = adb
        self.samples: queue.Queue[PupilSample] = queue.Queue(maxsize=512)
        self.errors: queue.Queue[str] = queue.Queue(maxsize=16)
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._configured = False
        self._clock = KernelToPcMonotonicClock()

    @property
    def instance_path(self) -> str:
        return f"{TRACE_ROOT}/instances/{TRACE_INSTANCE}"

    def _adb_root(self, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.adb, "shell", "su", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=check,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _cleanup(self) -> None:
        self._adb_root(f"echo 0 '>' {self.instance_path}/tracing_on", check=False)
        self._adb_root(
            f"echo 0 '>' {self.instance_path}/events/{TRACE_GROUP}/neural_pupil/enable",
            check=False,
        )
        self._adb_root(
            f"echo '-:{TRACE_GROUP}/neural_pupil' '>' {TRACE_ROOT}/uprobe_events",
            check=False,
        )
        self._adb_root(f"rmdir {self.instance_path}", check=False)
        self._configured = False

    def start(self) -> None:
        state = subprocess.run(
            [self.adb, "get-state"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if state.returncode != 0 or state.stdout.strip() != "device":
            raise RuntimeError("No authorized Quest was found over ADB")
        size_result = self._adb_root(f"stat -c %s {ENGINE_PATH}")
        try:
            engine_size = int(size_result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as error:
            raise RuntimeError("Could not verify the headset tracking-engine build") from error
        if engine_size != EXPECTED_ENGINE_SIZE:
            raise RuntimeError(
                f"Tracking-engine size changed ({engine_size}, expected "
                f"{EXPECTED_ENGINE_SIZE}); the firmware-specific offset is unsafe"
            )

        self._cleanup()
        self._adb_root(f"mkdir {self.instance_path}")
        event = (
            f"p:{TRACE_GROUP}/neural_pupil {ENGINE_PATH}:0x{PROBE_OFFSET:x} "
            "lx=+0xd4(%x19):x32 ly=+0xd8(%x19):x32 lz=+0xdc(%x19):x32 "
            "rx=+0xe0(%x19):x32 ry=+0xe4(%x19):x32 rz=+0xe8(%x19):x32 "
            "lax=+0x58(%x19):x32 lay=+0x5c(%x19):x32 laz=+0x60(%x19):x32 "
            "rax=+0x64(%x19):x32 ray=+0x68(%x19):x32 raz=+0x6c(%x19):x32"
        )
        encoded = base64.b64encode(event.encode()).decode()
        try:
            self._adb_root(
                f"echo {encoded} '|' base64 -d '>>' {TRACE_ROOT}/uprobe_events"
            )
            self._adb_root(
                f"echo 1 '>' {self.instance_path}/events/{TRACE_GROUP}/neural_pupil/enable"
            )
            self._adb_root(f"echo 1 '>' {self.instance_path}/tracing_on")
            self._configured = True
            self._process = subprocess.Popen(
                [self.adb, "shell", f"su -c 'cat {self.instance_path}/trace_pipe'"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._thread = threading.Thread(target=self._read, daemon=True)
            self._thread.start()
        except Exception:
            self._cleanup()
            raise

    def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            for line in self._process.stdout:
                if self._stopping:
                    break
                match = TRACE_SAMPLE.search(line)
                if match is None:
                    continue
                arrival_monotonic_ns = time.monotonic_ns()
                kernel_time_s = float(match.group("time"))
                sample = PupilSample(
                    pc_monotonic_ns=self._clock.translate(
                        kernel_time_s, arrival_monotonic_ns
                    ),
                    arrival_monotonic_ns=arrival_monotonic_ns,
                    kernel_time_s=kernel_time_s,
                    left_pupil_3d=tuple(
                        float_from_trace_hex(match.group("l" + axis))
                        for axis in ("x", "y", "z")
                    ),
                    right_pupil_3d=tuple(
                        float_from_trace_hex(match.group("r" + axis))
                        for axis in ("x", "y", "z")
                    ),
                    left_model_axis=tuple(
                        float_from_trace_hex(match.group("la" + axis))
                        for axis in ("x", "y", "z")
                    ),
                    right_model_axis=tuple(
                        float_from_trace_hex(match.group("ra" + axis))
                        for axis in ("x", "y", "z")
                    ),
                )
                try:
                    self.samples.put_nowait(sample)
                except queue.Full:
                    self.samples.get_nowait()
                    self.samples.put_nowait(sample)
        except Exception as error:
            try:
                self.errors.put_nowait(str(error))
            except queue.Full:
                pass

    def close(self) -> None:
        self._stopping = True
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._configured:
            self._cleanup()


def pupil_panel(
    image: np.ndarray,
    origin: tuple[int, int],
    title: str,
    pupil: tuple[float, float, float],
    mean: np.ndarray,
    scale: np.ndarray,
    color: tuple[int, int, int],
    raw_pupil: tuple[float, float, float] | None = None,
) -> None:
    x0, y0 = origin
    width, height = 430, 330
    put_text(image, title, (x0, y0 - 16), color, 0.62, 1)
    cv2.rectangle(image, (x0, y0), (x0 + width, y0 + height), (65, 65, 65), 1)
    center = (x0 + width // 2, y0 + height // 2)
    cv2.line(image, (center[0], y0), (center[0], y0 + height), (45, 45, 45), 1)
    cv2.line(image, (x0, center[1]), (x0 + width, center[1]), (45, 45, 45), 1)
    nx = (pupil[0] - mean[0]) / scale[0]
    ny = (pupil[1] - mean[1]) / scale[1]
    px = center[0] + int(np.clip(nx / 3.0, -1.0, 1.0) * (width // 2 - 10))
    py = center[1] - int(np.clip(ny / 3.0, -1.0, 1.0) * (height // 2 - 10))
    cv2.circle(image, (px, py), 10, color, 3, cv2.LINE_AA)
    if raw_pupil is not None:
        raw_nx = (raw_pupil[0] - mean[0]) / scale[0]
        raw_ny = (raw_pupil[1] - mean[1]) / scale[1]
        raw_x = center[0] + int(
            np.clip(raw_nx / 3.0, -1.0, 1.0) * (width // 2 - 10)
        )
        raw_y = center[1] - int(
            np.clip(raw_ny / 3.0, -1.0, 1.0) * (height // 2 - 10)
        )
        cv2.circle(image, (raw_x, raw_y), 3, (110, 110, 110), -1, cv2.LINE_AA)
    put_text(
        image,
        f"raw pupil X {pupil[0] * 1000:+.3f}  Y {pupil[1] * 1000:+.3f}  Z {pupil[2] * 1000:+.3f} mm",
        (x0 + 12, y0 + height - 16),
        color,
        0.45,
        1,
    )


def axis_span(samples: collections.deque[PupilSample], side: str, axis: int) -> float:
    values = [getattr(sample, f"{side}_pupil_3d")[axis] for sample in samples]
    return (max(values) - min(values)) * 1000.0 if values else 0.0


def save_capture(path: Path, samples: list[PupilSample]) -> None:
    payload = {
        "format": "qpro-socialgaze-neural-pupil-v1",
        "created_unix_ns": time.time_ns(),
        "engine_size": EXPECTED_ENGINE_SIZE,
        "probe_offset": PROBE_OFFSET,
        "source": "decoded SocialGazeDNN pupil tensor before EyeData/gaze/convergence",
        "samples": [asdict(sample) for sample in samples],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", required=True)
    parser.add_argument("--headless-seconds", type=float, default=0.0)
    parser.add_argument("--calibration-overlay", default="")
    parser.add_argument("--calibration-output", default="")
    parser.add_argument("--gaze-seconds", type=int, default=60)
    parser.add_argument("--convergence-seconds", type=int, default=80)
    parser.add_argument("--labels-port", type=int, default=0)
    arguments = parser.parse_args()

    reader = NeuralPupilReader(arguments.adb)
    reader.start()
    calibration = None
    label_recorder = (
        LabelSidecarRecorder(None, arguments.labels_port)
        if arguments.labels_port > 0 else None
    )
    if arguments.calibration_overlay:
        if arguments.headless_seconds > 0:
            if label_recorder is not None:
                label_recorder.close()
            reader.close()
            raise ValueError("Calibration cannot run in headless mode")
        if not arguments.calibration_output:
            if label_recorder is not None:
                label_recorder.close()
            reader.close()
            raise ValueError("Calibration requires --calibration-output")
        from pupil_gaze_calibration import NativePupilCalibrationController

        calibration = NativePupilCalibrationController(
            arguments.calibration_overlay,
            arguments.calibration_output,
            gaze_seconds=arguments.gaze_seconds,
            convergence_seconds=arguments.convergence_seconds,
        )
        try:
            calibration.start()
        except Exception:
            if label_recorder is not None:
                label_recorder.close()
            reader.close()
            raise
    captured: list[PupilSample] = []
    if arguments.headless_seconds > 0:
        deadline = time.monotonic() + arguments.headless_seconds
        try:
            while time.monotonic() < deadline:
                try:
                    captured.append(reader.samples.get(timeout=0.25))
                except queue.Empty:
                    pass
            if not captured:
                raise RuntimeError("The neural pupil trace produced no samples")
            print(json.dumps({"samples": len(captured), "latest": asdict(captured[-1])}))
            return 0
        finally:
            if label_recorder is not None:
                label_recorder.close()
            reader.close()

    recent: collections.deque[PupilSample] = collections.deque(maxlen=800)
    latest: PupilSample | None = None
    filtered_left: tuple[float, float, float] | None = None
    filtered_right: tuple[float, float, float] | None = None
    eye_filter = IndependentEyeFilter(3)
    rate_samples = 0
    rate_started = time.monotonic()
    sample_rate = 0.0
    saved_message = ""
    window = "Quest Pro pre-fusion neural pupil probe"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, 1000, 800)
    try:
        while True:
            while True:
                try:
                    sample = reader.samples.get_nowait()
                except queue.Empty:
                    break
                latest = sample
                captured.append(sample)
                recent.append(sample)
                rate_samples += 1
                if calibration is not None:
                    factory_sample = (
                        label_recorder.nearest_sample(sample.pc_monotonic_ns)
                        if label_recorder is not None else None
                    )
                    calibration.add_pupil_sample(
                        sample.left_pupil_3d,
                        sample.right_pupil_3d,
                        sample.pc_monotonic_ns,
                        kernel_time_s=sample.kernel_time_s,
                        arrival_monotonic_ns=sample.arrival_monotonic_ns,
                        factory_sample=factory_sample,
                    )
                left_normalized = (
                    np.asarray(sample.left_pupil_3d, dtype=np.float64) - LEFT_MEAN
                ) / LEFT_SCALE
                right_normalized = (
                    np.asarray(sample.right_pupil_3d, dtype=np.float64) - RIGHT_MEAN
                ) / RIGHT_SCALE
                left_value, right_value = eye_filter.update(
                    left_normalized,
                    right_normalized,
                    sample.pc_monotonic_ns / 1_000_000_000.0,
                )
                filtered_left = tuple(left_value * LEFT_SCALE + LEFT_MEAN)
                filtered_right = tuple(right_value * RIGHT_SCALE + RIGHT_MEAN)
            if not reader.errors.empty():
                raise RuntimeError(reader.errors.get_nowait())
            now = time.monotonic()
            elapsed = now - rate_started
            if elapsed >= 1.0:
                sample_rate = rate_samples / elapsed
                rate_samples = 0
                rate_started = now

            image = np.zeros((800, 1000, 3), dtype=np.uint8)
            put_text(image, "Quest Pro pre-fusion neural pupil probe", (22, 40),
                     (245, 245, 245), 0.86, 2)
            put_text(
                image,
                f"decoded model pupil tensor | {sample_rate:.1f} Hz | READ ONLY",
                (22, 72), (80, 235, 120), 0.5, 1,
            )
            put_text(
                image,
                "These are pupil coordinates, not gaze angles. No gaze or convergence code has run yet.",
                (22, 100), (175, 175, 175), 0.46,
            )
            if latest is None or filtered_left is None or filtered_right is None:
                put_text(image, "Waiting for neural pupil frames...", (330, 340),
                         (50, 175, 255), 0.65, 1)
            else:
                pupil_panel(image, (22, 145), "LEFT MODEL PUPIL", filtered_left,
                            LEFT_MEAN, LEFT_SCALE, (255, 210, 45), latest.left_pupil_3d)
                pupil_panel(image, (548, 145), "RIGHT MODEL PUPIL", filtered_right,
                            RIGHT_MEAN, RIGHT_SCALE, (245, 70, 235), latest.right_pupil_3d)

            cutoff = time.monotonic_ns() - 5_000_000_000
            while recent and recent[0].pc_monotonic_ns < cutoff:
                recent.popleft()
            cv2.line(image, (22, 505), (978, 505), (60, 60, 60), 1)
            if calibration is None:
                put_text(image, "Physical independence gate", (22, 545),
                         (235, 235, 235), 0.63, 1)
                put_text(image,
                         "1. Keep the left eye fixed and let only the right eye drift. Only the RIGHT dot should move.",
                         (22, 582), (190, 190, 190), 0.47)
                put_text(image,
                         "2. Close one eye and move the open eye. The closed-eye dot must not copy the open-eye motion.",
                         (22, 616), (190, 190, 190), 0.47)
                if recent:
                    put_text(
                        image,
                        "5-second pupil span (X/Y mm): "
                        f"L {axis_span(recent, 'left', 0):.2f}/{axis_span(recent, 'left', 1):.2f}   "
                        f"R {axis_span(recent, 'right', 0):.2f}/{axis_span(recent, 'right', 1):.2f}",
                        (22, 665), (80, 235, 120), 0.48, 1,
                    )
                control_text = "S saves raw pupil samples. Q quits and removes tracepoints."
            else:
                phase, message, count = calibration.status()
                status_color = (80, 235, 120) if phase == "done" else (50, 175, 255)
                if phase == "error":
                    status_color = (70, 70, 255)
                put_text(
                    image,
                    f"IN-VR INDEPENDENT PUPIL CALIBRATION [{phase.upper()}] | {count} samples",
                    (22, 545), status_color, 0.58, 1,
                )
                put_text(image, message, (22, 585), (210, 210, 210), 0.45, 1)
                put_text(
                    image,
                    "Space starts the stage shown in VR. R resends its tutorial.",
                    (22, 630), (190, 190, 190), 0.47,
                )
                put_text(
                    image,
                    "Gaze: move head slowly. Near/far: hold the dot and sweep head in a wide slow figure-eight.",
                    (22, 665), (190, 190, 190), 0.47,
                )
                control_text = "SPACE starts | R resends tutorial | Q quits and removes tracepoints"
            put_text(image, control_text, (22, 748), (160, 160, 160), 0.47)
            put_text(image, "large colored ring = filtered | small gray dot = raw",
                     (22, 718), (145, 145, 145), 0.43)
            if saved_message:
                put_text(image, saved_message, (580, 748), (80, 235, 120), 0.42)
            cv2.imshow(window, image)
            key = cv2.waitKeyEx(10)
            if key < 0:
                continue
            character = chr(key & 0xFF).lower()
            if character == "q":
                break
            if calibration is not None:
                factory_ready = bool(
                    label_recorder is not None
                    and label_recorder.source_is_live()
                    and label_recorder.sample_count >= 10
                )
                calibration.handle_key(character, factory_ready=factory_ready)
            if character == "s":
                stamp = time.strftime("%Y%m%d-%H%M%S")
                path = Path("captures") / f"neural-pupil-{stamp}.json"
                save_capture(path, captured)
                saved_message = f"Saved {path}"
    finally:
        if calibration is not None:
            calibration.close()
        if label_recorder is not None:
            label_recorder.close()
        reader.close()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
