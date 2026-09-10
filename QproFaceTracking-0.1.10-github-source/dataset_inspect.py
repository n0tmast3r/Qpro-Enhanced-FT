#!/usr/bin/env python3
"""Audit synchronization and label coverage for a Qpro training recording."""

from __future__ import annotations

import argparse
import bisect
import json
from pathlib import Path

import numpy as np

from capture_format import (
    FILE_HEADER,
    FILE_MAGIC,
    FRAME_HEADER,
    FRAME_MAGIC,
    TRANSPORT_HEADER,
)


def camera_timestamps(path: str | Path) -> list[int]:
    result: list[int] = []
    with Path(path).open("rb") as capture:
        raw_file_header = capture.read(FILE_HEADER.size)
        if len(raw_file_header) != FILE_HEADER.size:
            raise ValueError("Capture file header is missing")
        if FILE_HEADER.unpack(raw_file_header)[0] != FILE_MAGIC:
            raise ValueError("Unrecognized capture file")
        while True:
            raw_frame_header = capture.read(FRAME_HEADER.size)
            if not raw_frame_header:
                break
            if len(raw_frame_header) != FRAME_HEADER.size:
                raise ValueError("Truncated capture frame header")
            (magic, _record_size, _source_size, pc_monotonic_ns, _pc_wall_ns,
             transport_header) = FRAME_HEADER.unpack(raw_frame_header)
            if magic != FRAME_MAGIC:
                raise ValueError("Invalid capture frame record")
            payload_size = TRANSPORT_HEADER.unpack(transport_header)[9]
            capture.seek(payload_size, 1)
            result.append(pc_monotonic_ns)
    return result


def load_labels(path: str | Path) -> tuple[list[str], list[dict[str, object]]]:
    names: list[str] = []
    samples: list[dict[str, object]] = []
    with Path(path).open("r", encoding="utf-8") as sidecar:
        for line in sidecar:
            record = json.loads(line)
            if record.get("type") == "schema":
                names = record["names"]
            elif record.get("type") == "sample":
                samples.append(record)
    return names, samples


def percentile(values: np.ndarray, amount: float) -> float:
    return float(np.percentile(values, amount)) if values.size else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect a labeled Qpro dataset")
    parser.add_argument("capture")
    parser.add_argument("--labels")
    arguments = parser.parse_args()

    capture_path = Path(arguments.capture).resolve()
    label_path = (
        Path(arguments.labels).resolve()
        if arguments.labels
        else capture_path.with_suffix(".qplabel.jsonl")
    )
    frame_times = camera_timestamps(capture_path)
    names, samples = load_labels(label_path)
    if not frame_times:
        raise ValueError("Capture contains no frames")
    if not samples:
        raise ValueError("Label sidecar contains no samples")

    label_times = [int(sample["arrivalMonotonicNs"]) for sample in samples]
    offsets_ms: list[float] = []
    for frame_time in frame_times:
        insertion = bisect.bisect_left(label_times, frame_time)
        candidates = []
        if insertion < len(label_times):
            candidates.append(label_times[insertion])
        if insertion:
            candidates.append(label_times[insertion - 1])
        offsets_ms.append(min(abs(candidate - frame_time) for candidate in candidates) / 1e6)

    offsets = np.asarray(offsets_ms, dtype=np.float64)
    weights = np.asarray([sample["values"] for sample in samples], dtype=np.float32)
    ranges = np.ptp(weights, axis=0)
    order = np.argsort(ranges)[::-1]
    face_valid = np.asarray(
        [bool(int(sample.get("faceFlags", 0)) & 1) for sample in samples]
    )
    eye_expression_valid = np.asarray(
        [bool(sample.get("isEyeFollowingBlendshapesValid")) for sample in samples]
    )
    left_eye_valid = np.asarray(
        [bool(sample.get("leftEyeIsValid")) for sample in samples]
    )
    right_eye_valid = np.asarray(
        [bool(sample.get("rightEyeIsValid")) for sample in samples]
    )
    source_change_sequences = [
        int(sample.get("sourceChangeSequence", 0)) for sample in samples
    ]

    print(f"Capture: {capture_path}")
    print(f"Labels: {label_path}")
    print(f"Camera frames: {len(frame_times)}; label samples: {len(samples)}")
    print(
        f"Nearest-label error: median {percentile(offsets, 50):.2f} ms; "
        f"p95 {percentile(offsets, 95):.2f} ms; max {offsets.max():.2f} ms"
    )
    print(
        f"Frames within 10 ms: {int(np.sum(offsets <= 10.0))}/{len(frame_times)}; "
        f"within 20 ms: {int(np.sum(offsets <= 20.0))}/{len(frame_times)}"
    )
    print(
        "Validity: "
        f"face {face_valid.mean() * 100:.1f}%, "
        f"eye expressions {eye_expression_valid.mean() * 100:.1f}%, "
        f"left pose {left_eye_valid.mean() * 100:.1f}%, "
        f"right pose {right_eye_valid.mean() * 100:.1f}%"
    )
    print(
        f"Expressions showing >0.001 range: {int(np.sum(ranges > 0.001))}/"
        f"{len(names)}"
    )
    if max(source_change_sequences, default=0):
        print(
            "Label-source state changes: "
            f"{max(source_change_sequences) - min(source_change_sequences)}"
        )
    if not np.any(ranges > 0.001):
        print(
            "WARNING: factory expression labels are frozen; do not use this "
            "capture as supervised expression training data"
        )
    print("Largest expression ranges:")
    for index in order[:10]:
        name = names[index] if index < len(names) else f"expression_{index}"
        print(f"  {name}: {ranges[index]:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
