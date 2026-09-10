#!/usr/bin/env python3
"""Build a compact, timestamp-aligned training cache from a Qpro capture."""

from __future__ import annotations

import argparse
import bisect
import json
import time
from pathlib import Path

import cv2
import numpy as np

from calibration_inspect import step_intervals
from capture_format import (
    FILE_HEADER,
    FILE_MAGIC,
    FRAME_HEADER,
    FRAME_MAGIC,
    TRANSPORT_HEADER,
)
from dataset_inspect import load_labels


def scan_frames(path: Path) -> tuple[list[tuple[int, int, int, int]], int]:
    entries: list[tuple[int, int, int, int]] = []
    with path.open("rb") as capture:
        raw_file_header = capture.read(FILE_HEADER.size)
        if len(raw_file_header) != FILE_HEADER.size:
            raise ValueError("Capture file header is missing")
        fields = FILE_HEADER.unpack(raw_file_header)
        if fields[0] != FILE_MAGIC:
            raise ValueError("Unrecognized capture file")
        declared_frames = int(fields[5])
        while True:
            raw_frame_header = capture.read(FRAME_HEADER.size)
            if not raw_frame_header:
                break
            if len(raw_frame_header) != FRAME_HEADER.size:
                raise ValueError("Truncated capture frame header")
            magic, _record_size, _source_size, timestamp, _wall, raw_transport = (
                FRAME_HEADER.unpack(raw_frame_header)
            )
            if magic != FRAME_MAGIC:
                raise ValueError("Invalid frame record")
            transport = TRANSPORT_HEADER.unpack(raw_transport)
            width, height, stride, payload_size, camera_mask = (
                int(transport[5]), int(transport[6]), int(transport[7]),
                int(transport[9]), int(transport[10]),
            )
            if camera_mask != 0x1F or width != 2000 or height != 400:
                raise ValueError("Training cache currently requires all five 400x400 cameras")
            if stride != width or payload_size != width * height:
                raise ValueError("Unsupported frame payload layout")
            entries.append((capture.tell(), int(timestamp), width, height))
            capture.seek(payload_size, 1)
    if len(entries) != declared_frames:
        raise ValueError(
            f"Capture declares {declared_frames} frames but scanned {len(entries)}"
        )
    return entries, declared_frames


def nearest_label_indices(
    frame_times: list[int], label_times: list[int]
) -> tuple[np.ndarray, np.ndarray]:
    indices = np.empty(len(frame_times), dtype=np.int32)
    errors_ms = np.empty(len(frame_times), dtype=np.float32)
    for output_index, frame_time in enumerate(frame_times):
        right = bisect.bisect_left(label_times, frame_time)
        candidates = []
        if right < len(label_times):
            candidates.append(right)
        if right:
            candidates.append(right - 1)
        if not candidates:
            raise ValueError("No factory labels are available")
        chosen = min(candidates, key=lambda index: abs(label_times[index] - frame_time))
        indices[output_index] = chosen
        errors_ms[output_index] = abs(label_times[chosen] - frame_time) / 1e6
    return indices, errors_ms


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a Qpro training cache")
    parser.add_argument("capture")
    parser.add_argument("--labels")
    parser.add_argument("--session")
    parser.add_argument("--output")
    parser.add_argument("--size", type=int, default=96)
    arguments = parser.parse_args()
    if not 48 <= arguments.size <= 256:
        parser.error("--size must be between 48 and 256")

    capture_path = Path(arguments.capture).resolve()
    label_path = (
        Path(arguments.labels).resolve()
        if arguments.labels else capture_path.with_suffix(".qplabel.jsonl")
    )
    session_path = (
        Path(arguments.session).resolve()
        if arguments.session else capture_path.with_suffix(".qpsession.json")
    )
    output = (
        Path(arguments.output).resolve()
        if arguments.output
        else Path("training") / f"{capture_path.stem}-{arguments.size}px"
    )
    metadata_path = output / "metadata.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            metadata.get("complete")
            and metadata.get("version") == 2
            and (output / "step_ids.npy").exists()
        ):
            print(f"Training cache already complete: {output.resolve()}")
            return 0
    output.mkdir(parents=True, exist_ok=True)

    print("Scanning camera frame offsets...")
    entries, frame_count = scan_frames(capture_path)
    names, labels = load_labels(label_path)
    if len(names) != 70:
        raise ValueError(f"Expected 70 factory expressions, found {len(names)}")
    label_times = [int(sample["arrivalMonotonicNs"]) for sample in labels]
    frame_times = [entry[1] for entry in entries]
    label_indices, errors_ms = nearest_label_indices(frame_times, label_times)
    if float(np.max(errors_ms)) > 20.0:
        raise ValueError(
            f"Worst frame/label alignment is {float(np.max(errors_ms)):.2f} ms"
        )

    images = np.lib.format.open_memmap(
        output / "images.npy", mode="w+", dtype=np.uint8,
        shape=(frame_count, 5, arguments.size, arguments.size),
    )
    expressions = np.lib.format.open_memmap(
        output / "expressions.npy", mode="w+", dtype=np.float32,
        shape=(frame_count, 70),
    )
    eye_orientations = np.lib.format.open_memmap(
        output / "eye_orientations.npy", mode="w+", dtype=np.float32,
        shape=(frame_count, 8),
    )
    timestamps = np.lib.format.open_memmap(
        output / "timestamps.npy", mode="w+", dtype=np.int64,
        shape=(frame_count,),
    )
    step_ids = np.lib.format.open_memmap(
        output / "step_ids.npy", mode="w+", dtype=np.int16,
        shape=(frame_count,),
    )
    step_ids[:] = -1
    session = json.loads(session_path.read_text(encoding="utf-8"))
    for step, intervals in enumerate(step_intervals(session)):
        for start, finish in intervals:
            first = bisect.bisect_left(frame_times, start)
            last = bisect.bisect_right(frame_times, finish)
            step_ids[first:last] = step
    if np.any(step_ids < 0):
        raise ValueError(
            f"{int(np.sum(step_ids < 0))} camera frames are outside calibration steps"
        )

    started = time.monotonic()
    with capture_path.open("rb", buffering=4 * 1024 * 1024) as capture:
        for index, ((payload_offset, timestamp, width, height), label_index) in enumerate(
            zip(entries, label_indices)
        ):
            capture.seek(payload_offset)
            raw = capture.read(width * height)
            if len(raw) != width * height:
                raise ValueError(f"Frame {index} payload is truncated")
            strip = np.frombuffer(raw, dtype=np.uint8).reshape(height, width)
            for camera in range(5):
                panel = strip[:, camera * 400:(camera + 1) * 400]
                images[index, camera] = cv2.resize(
                    panel,
                    (arguments.size, arguments.size),
                    interpolation=cv2.INTER_AREA,
                )
            label = labels[int(label_index)]
            expressions[index] = np.asarray(label["values"], dtype=np.float32)
            eye_orientations[index] = np.asarray(
                label["leftEyeOrientation"] + label["rightEyeOrientation"],
                dtype=np.float32,
            )
            timestamps[index] = timestamp
            if (index + 1) % 500 == 0 or index + 1 == frame_count:
                rate = (index + 1) / max(0.001, time.monotonic() - started)
                print(f"Prepared {index + 1}/{frame_count} frames ({rate:.1f}/s)")

    images.flush()
    expressions.flush()
    eye_orientations.flush()
    timestamps.flush()
    step_ids.flush()
    metadata_path.write_text(
        json.dumps(
            {
                "version": 2,
                "complete": True,
                "capture": str(capture_path),
                "labels": str(label_path),
                "session": str(session_path),
                "frames": frame_count,
                "imageSize": arguments.size,
                "cameraOrder": [
                    "left_eye", "right_eye", "left_face", "right_face", "eyebrow"
                ],
                "expressionNames": names,
                "medianLabelErrorMs": float(np.median(errors_ms)),
                "maximumLabelErrorMs": float(np.max(errors_ms)),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    cache_bytes = sum(path.stat().st_size for path in output.iterdir() if path.is_file())
    print(
        f"Training cache complete: {output.resolve()}\n"
        f"Frames: {frame_count}; size: {cache_bytes / 1_000_000:.1f} MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
