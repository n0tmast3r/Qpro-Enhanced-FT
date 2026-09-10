#!/usr/bin/env python3
"""Continuously inspect Quest's native per-eye shared-memory tracking streams."""

from __future__ import annotations

import argparse
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

from stereo_eye_calibration import quaternion_yaw_pitch


STREAM_NAMES = {
    0: "social",
    1: "interaction",
    2: "foveation",
    3: "internal slot 3 (candidate pre-fusion)",
}
VECTOR = re.compile(r"\(([-+0-9.eE]+), ([-+0-9.eE]+), ([-+0-9.eE]+)\)")
QUATERNION = re.compile(
    r"\(([-+0-9.eE]+), ([-+0-9.eE]+), ([-+0-9.eE]+), ([-+0-9.eE]+)\)"
)
VALID = re.compile(
    r"Valid: (\d+) Time: ([-+0-9.eE]+), Arrival: ([-+0-9.eE]+), "
    r"Processing end: ([-+0-9.eE]+)"
)


@dataclass(frozen=True)
class NativeEyeSample:
    pc_monotonic_ns: int
    mode: int
    valid: bool
    source_time: float
    arrival_time: float
    processing_end_time: float
    left_position: tuple[float, float, float]
    left_orientation: tuple[float, float, float, float]
    right_position: tuple[float, float, float]
    right_orientation: tuple[float, float, float, float]
    combined_point: tuple[float, float, float]

    @property
    def left_angles(self) -> tuple[float, float]:
        return quaternion_yaw_pitch(list(self.left_orientation))

    @property
    def right_angles(self) -> tuple[float, float]:
        return quaternion_yaw_pitch(list(self.right_orientation))

    @property
    def disparity(self) -> float:
        return self.right_angles[0] - self.left_angles[0]


class NativeEyeReader:
    def __init__(self, adb: str, remote: str, mode: int) -> None:
        self.adb = adb
        self.remote = remote
        self.mode = mode
        self.samples: queue.Queue[NativeEyeSample] = queue.Queue(maxsize=512)
        self.errors: queue.Queue[str] = queue.Queue(maxsize=16)
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    def start(self) -> None:
        # Meta's supplied diagnostic opens and closes its client per invocation.
        remote_command = (
            f"su -c 'while true; do "
            f"{self.remote} getEyeTrackingData {self.mode}; done'"
        )
        self._process = subprocess.Popen(
            [self.adb, "shell", remote_command], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        current: dict[str, object] = {}
        eye = ""
        try:
            for source_line in self._process.stdout:
                if self._stopping:
                    break
                line = source_line.strip()
                valid_match = VALID.fullmatch(line)
                if valid_match:
                    current = {
                        "pc_monotonic_ns": time.monotonic_ns(), "mode": self.mode,
                        "valid": valid_match.group(1) == "1",
                        "source_time": float(valid_match.group(2)),
                        "arrival_time": float(valid_match.group(3)),
                        "processing_end_time": float(valid_match.group(4)),
                    }
                    eye = ""
                elif line == "Left eye:":
                    eye = "left"
                elif line == "Right eye:":
                    eye = "right"
                elif line.startswith("Gaze translation:") and eye:
                    match = VECTOR.search(line)
                    if match:
                        current[f"{eye}_position"] = tuple(map(float, match.groups()))
                elif line.startswith("Gaze rotation:") and eye:
                    match = QUATERNION.search(line)
                    if match:
                        current[f"{eye}_orientation"] = tuple(map(float, match.groups()))
                elif line.startswith("Combined gaze point:"):
                    match = VECTOR.search(line)
                    if match:
                        current["combined_point"] = tuple(map(float, match.groups()))
                    required = {
                        "pc_monotonic_ns", "mode", "valid", "source_time",
                        "arrival_time", "processing_end_time", "left_position",
                        "left_orientation", "right_position", "right_orientation",
                        "combined_point",
                    }
                    if required.issubset(current):
                        self._put(NativeEyeSample(**current))  # type: ignore[arg-type]
                    current = {}
        except Exception as error:
            self._put_error(error)

    def _put(self, sample: NativeEyeSample) -> None:
        try:
            self.samples.put_nowait(sample)
        except queue.Full:
            try:
                self.samples.get_nowait()
            except queue.Empty:
                pass
            self.samples.put_nowait(sample)

    def _put_error(self, error: Exception) -> None:
        try:
            self.errors.put_nowait(str(error))
        except queue.Full:
            pass

    def close(self) -> None:
        self._stopping = True
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        subprocess.run(
            [self.adb, "shell", f"su -c 'pkill -f {Path(self.remote).name}'"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )


MEMORY_LINE = re.compile(r"kind=client1 mode=(\d+) status=(\d+) bytes=([0-9a-f]+)")


def parse_memory_client1(line: str, pc_monotonic_ns: int) -> NativeEyeSample | None:
    """Decode the stable 256-byte EyeClient-v1 view of Meta's eye memory."""
    match = MEMORY_LINE.fullmatch(line.strip())
    if not match or match.group(2) != "1":
        return None
    payload = bytes.fromhex(match.group(3))
    if len(payload) != 256:
        return None

    def floats(offset: int, count: int) -> tuple[float, ...]:
        return struct.unpack_from("<" + "f" * count, payload, offset)

    return NativeEyeSample(
        pc_monotonic_ns=pc_monotonic_ns, mode=int(match.group(1)),
        valid=payload[0] != 0,
        source_time=struct.unpack_from("<Q", payload, 8)[0] / 1e9,
        arrival_time=struct.unpack_from("<Q", payload, 16)[0] / 1e9,
        processing_end_time=struct.unpack_from("<Q", payload, 24)[0] / 1e9,
        left_position=floats(56, 3), left_orientation=floats(40, 4),
        right_position=floats(120, 3), right_orientation=floats(104, 4),
        combined_point=floats(200, 3),
    )


class MemoryEyeReader(NativeEyeReader):
    """Persistent reader for all four EyeClient shared-memory policy slots."""

    def start(self) -> None:
        remote_command = f"su -c '{self.remote} 2147483647 14 {self.mode}'"
        self._process = subprocess.Popen(
            [self.adb, "shell", remote_command], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._thread = threading.Thread(target=self._read_memory, daemon=True)
        self._thread.start()

    def _read_memory(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            for line in self._process.stdout:
                if self._stopping:
                    break
                sample = parse_memory_client1(line, time.monotonic_ns())
                if sample is not None and sample.mode == self.mode:
                    self._put(sample)
        except Exception as error:
            self._put_error(error)


def put_text(
    image: np.ndarray,
    text: str,
    position: tuple[int, int],
    color: tuple[int, int, int] = (225, 225, 225),
    scale: float = 0.55,
    thickness: int = 1,
) -> None:
    cv2.putText(image, text, position, cv2.FONT_HERSHEY_SIMPLEX, scale,
                color, thickness, cv2.LINE_AA)


def gaze_panel(
    image: np.ndarray,
    origin: tuple[int, int],
    title: str,
    angles: tuple[float, float],
    color: tuple[int, int, int],
) -> None:
    x, y = origin
    width, height = 360, 250
    put_text(image, title, (x, y - 12), color, 0.62, 1)
    cv2.rectangle(image, (x, y), (x + width, y + height), (75, 75, 75), 1)
    cv2.line(image, (x + width // 2, y), (x + width // 2, y + height),
             (50, 50, 50), 1)
    cv2.line(image, (x, y + height // 2), (x + width, y + height // 2),
             (50, 50, 50), 1)
    yaw, pitch = angles
    # Quest convention: negative yaw is wearer's right; positive pitch is up.
    marker_x = int(np.clip(x + (40.0 - yaw) / 80.0 * width, x, x + width))
    marker_y = int(np.clip(y + (35.0 - pitch) / 70.0 * height, y, y + height))
    cv2.circle(image, (marker_x, marker_y), 10, color, 2, cv2.LINE_AA)
    put_text(image, f"yaw {yaw:+.2f}  pitch {pitch:+.2f} deg",
             (x + 12, y + height - 14), color, 0.5)


def summary(values: list[float]) -> str:
    if not values:
        return "no samples"
    array = np.asarray(values, dtype=np.float64)
    return (
        f"n={len(array)} mean={np.mean(array):+.3f}  "
        f"sd={np.std(array):.3f}  span={np.ptp(array):.3f} deg"
    )


def save_capture(
    path: Path,
    samples: list[NativeEyeSample],
    labeled: dict[str, list[NativeEyeSample]],
) -> None:
    payload = {
        "format": "qpro-native-eye-probe-v1",
        "created_unix_ns": time.time_ns(),
        "samples": [asdict(sample) for sample in samples],
        "labeled": {
            name: [asdict(sample) for sample in values]
            for name, values in labeled.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--mode", type=int, choices=(0, 1, 2, 3), default=0)
    parser.add_argument("--memory", action="store_true")
    arguments = parser.parse_args()

    mode = arguments.mode
    reader_type = MemoryEyeReader if arguments.memory else NativeEyeReader
    if mode == 3 and not arguments.memory:
        parser.error("mode 3 is available only through --memory")
    reader = reader_type(arguments.adb, arguments.remote, mode)
    reader.start()
    latest: NativeEyeSample | None = None
    all_samples: list[NativeEyeSample] = []
    recent: collections.deque[NativeEyeSample] = collections.deque(maxlen=1200)
    labeled: dict[str, list[NativeEyeSample]] = {"near": [], "mid": [], "far": []}
    active_label = ""
    label_deadline = 0.0
    saved_message = ""
    rate_started = time.monotonic()
    rate_samples = 0
    sample_rate = 0.0
    window = "Quest Pro direct native eye probe"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, 1180, 760)
    try:
        while True:
            while True:
                try:
                    sample = reader.samples.get_nowait()
                except queue.Empty:
                    break
                latest = sample
                all_samples.append(sample)
                recent.append(sample)
                rate_samples += 1
                if active_label and time.monotonic() <= label_deadline:
                    labeled[active_label].append(sample)
            now = time.monotonic()
            if active_label and now > label_deadline:
                active_label = ""
                try:
                    import winsound
                    winsound.MessageBeep(winsound.MB_OK)
                except (ImportError, RuntimeError):
                    pass
            elapsed = now - rate_started
            if elapsed >= 1.0:
                sample_rate = rate_samples / elapsed
                rate_samples = 0
                rate_started = now

            image = np.zeros((760, 1180, 3), dtype=np.uint8)
            put_text(image, "Quest Pro direct native eye probe", (24, 40),
                     (245, 245, 245), 0.9, 2)
            put_text(image,
                     f"Meta eye memory | {STREAM_NAMES[mode]} ({mode}) | "
                     f"{sample_rate:.1f} samples/s",
                     (24, 72), (170, 170, 170), 0.5)
            put_text(image, "READ ONLY - behavioral independence is the test, not an assumption",
                     (24, 100), (50, 175, 255), 0.52, 1)
            if latest is None:
                put_text(image, "Waiting for valid native eye samples...", (330, 340),
                         (50, 175, 255), 0.7, 1)
            else:
                gaze_panel(image, (24, 150), "LEFT NATIVE EYE",
                           latest.left_angles, (255, 210, 45))
                gaze_panel(image, (410, 150), "RIGHT NATIVE EYE",
                           latest.right_angles, (245, 70, 235))
                disparity = latest.disparity
                put_text(image, "Native binocular breakdown", (805, 150),
                         (230, 230, 230), 0.62, 1)
                put_text(image, f"R yaw - L yaw  {disparity:+.3f} deg", (805, 194),
                         (80, 235, 120), 0.58, 1)
                point = latest.combined_point
                put_text(image,
                         f"Combined point ({point[0]:+.3f}, {point[1]:+.3f}, {point[2]:+.3f}) m",
                         (805, 230), (180, 180, 180), 0.47)
                put_text(image, "Combined Z is usually fixed near -1.1 m.", (805, 262),
                         (50, 175, 255), 0.44)
                cutoff = time.monotonic_ns() - 10_000_000_000
                recent_values = [sample.disparity for sample in recent
                                 if sample.pc_monotonic_ns >= cutoff]
                put_text(image, "Last 10 seconds", (805, 310),
                         (220, 220, 220), 0.5)
                put_text(image, summary(recent_values), (805, 340),
                         (80, 235, 120), 0.43)

            cv2.line(image, (24, 445), (1156, 445), (60, 60, 60), 1)
            put_text(image, "Near/far validation", (24, 485),
                     (235, 235, 235), 0.65, 1)
            put_text(image,
                     "Keep gaze centered. Press N while focusing near, M at ~1 m, "
                     "and F on something far. Each records 5 seconds.",
                     (24, 518), (175, 175, 175), 0.47)
            colors = {"near": (80, 235, 120), "mid": (255, 210, 45),
                      "far": (245, 70, 235)}
            for row, label in enumerate(("near", "mid", "far")):
                prefix = "> " if label == active_label else "  "
                put_text(image, f"{prefix}{label.upper():4s}  " +
                         summary([sample.disparity for sample in labeled[label]]),
                         (45, 560 + row * 38), colors[label], 0.49,
                         2 if label == active_label else 1)
            if labeled["near"] and labeled["far"]:
                difference = (
                    np.mean([sample.disparity for sample in labeled["near"]])
                    - np.mean([sample.disparity for sample in labeled["far"]])
                )
                put_text(image, f"near - far mean disparity: {difference:+.3f} deg",
                         (620, 575), (80, 235, 120), 0.58, 1)
            put_text(image,
                     ("Keys 0/1/2/3 switch streams. N/M/F capture. " if arguments.memory else
                      "Keys 0/1/2 switch streams. N/M/F capture. ") +
                     "S saves. Q quits.",
                     (24, 725), (160, 160, 160), 0.48)
            if saved_message:
                put_text(image, saved_message, (620, 620), (80, 235, 120), 0.43)
            cv2.imshow(window, image)
            key = cv2.waitKeyEx(10)
            if key < 0:
                continue
            character = chr(key & 0xFF).lower()
            if character == "q":
                break
            available_keys = "0123" if arguments.memory else "012"
            if character in available_keys and int(character) != mode:
                reader.close()
                mode = int(character)
                reader = reader_type(arguments.adb, arguments.remote, mode)
                reader.start()
                latest = None
                recent.clear()
                labeled = {"near": [], "mid": [], "far": []}
                active_label = ""
            elif character in ("n", "m", "f"):
                active_label = {"n": "near", "m": "mid", "f": "far"}[character]
                labeled[active_label].clear()
                label_deadline = time.monotonic() + 5.0
            elif character == "s":
                stamp = time.strftime("%Y%m%d-%H%M%S")
                path = Path("calibration") / f"native-eye-probe-{stamp}.json"
                save_capture(path, all_samples, labeled)
                saved_message = f"Saved {path.resolve()}"
    finally:
        reader.close()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
