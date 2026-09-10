#!/usr/bin/env python3
"""Prepare manually selected stereo tongue stills for model training."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from dataset_inspect import load_labels
from prepare_training import nearest_label_indices
from prepare_tongue_training import FACE_HEIGHT, FACE_WIDTH, scan_face_frames
from tongue_calibration import TONGUE_TARGET_NAMES


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare manual stereo tongue stills")
    parser.add_argument("capture")
    parser.add_argument("--labels")
    parser.add_argument("--session")
    parser.add_argument("--output")
    parser.add_argument("--size", type=int, default=224)
    arguments = parser.parse_args()
    if not 128 <= arguments.size <= 320:
        parser.error("--size must be between 128 and 320")

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
        else Path("training") / f"{capture_path.stem}-tongue-stills-{arguments.size}px"
    )
    output.mkdir(parents=True, exist_ok=True)

    session = json.loads(session_path.read_text(encoding="utf-8"))
    allowed_session_types = {
        "tongue-stereo-stills-v1",
        "tongue-stereo-corrections-v1",
        "tongue-stereo-refinement-v2",
        "tongue-stereo-arc-v3",
    }
    if session.get("sessionType") not in allowed_session_types:
        raise ValueError("A manual tongue still or correction session is required")
    entries, frame_count = scan_face_frames(capture_path)
    frame_times = [timestamp for _offset, timestamp in entries]
    sample_by_frame = {
        int(sample["frameIndex"]): sample
        for sample in session.get("samples", [])
    }
    if set(sample_by_frame) != set(range(frame_count)):
        missing = sorted(set(range(frame_count)) - set(sample_by_frame))
        raise ValueError(
            f"Session journal does not describe every captured still; missing {missing[:8]}"
        )

    names, labels = load_labels(label_path)
    if "TongueOut" not in names:
        raise ValueError("Factory label stream has no TongueOut channel")
    label_times = [int(value["arrivalMonotonicNs"]) for value in labels]
    label_indices, errors_ms = nearest_label_indices(frame_times, label_times)
    if float(np.max(errors_ms)) > 35.0:
        raise ValueError(
            f"Worst still/factory-label alignment is {float(np.max(errors_ms)):.2f} ms"
        )

    shape = (frame_count, 2, arguments.size, arguments.size)
    images = np.lib.format.open_memmap(
        output / "images.npy", mode="w+", dtype=np.uint8, shape=shape
    )
    targets = np.zeros((frame_count, len(TONGUE_TARGET_NAMES)), dtype=np.float32)
    native = np.zeros(frame_count, dtype=np.float32)
    native_expressions = np.zeros((frame_count, len(names)), dtype=np.float32)
    step_ids = np.zeros(frame_count, dtype=np.int16)
    trainable = np.ones(frame_count, dtype=np.bool_)
    tongue_index = names.index("TongueOut")

    started = time.monotonic()
    with capture_path.open("rb", buffering=4 * 1024 * 1024) as capture:
        for index, ((payload_offset, _timestamp), label_index) in enumerate(
            zip(entries, label_indices)
        ):
            sample = sample_by_frame[index]
            capture.seek(payload_offset)
            raw = capture.read(FACE_WIDTH * FACE_HEIGHT)
            if len(raw) != FACE_WIDTH * FACE_HEIGHT:
                raise ValueError(f"Still {index} is truncated")
            strip = np.frombuffer(raw, dtype=np.uint8).reshape(FACE_HEIGHT, FACE_WIDTH)
            for view in range(2):
                panel = strip[:, view * 400:(view + 1) * 400]
                images[index, view] = cv2.resize(
                    panel,
                    (arguments.size, arguments.size),
                    interpolation=cv2.INTER_AREA,
                )
            for target_index, name in enumerate(TONGUE_TARGET_NAMES):
                targets[index, target_index] = float(sample.get("targets", {}).get(name, 0.0))
            factory = np.asarray(labels[int(label_index)]["values"], dtype=np.float32)
            native_expressions[index] = factory
            native[index] = factory[tongue_index]
            step_ids[index] = int(sample["promptIndex"])
            trainable[index] = not bool(sample.get("excluded", False))
            if (index + 1) % 50 == 0 or index + 1 == frame_count:
                rate = (index + 1) / max(0.001, time.monotonic() - started)
                print(f"Prepared {index + 1}/{frame_count} stills ({rate:.1f}/s)")

    images.flush()
    np.save(output / "targets.npy", targets)
    np.save(output / "native_tongue_out.npy", native)
    np.save(output / "native_expressions.npy", native_expressions)
    np.save(output / "timestamps.npy", np.asarray(frame_times, dtype=np.int64))
    np.save(output / "step_ids.npy", step_ids)
    np.save(output / "trainable.npy", trainable)
    metadata = {
        "version": 2,
        "datasetType": "manual-stereo-stills",
        "sessionType": session.get("sessionType"),
        "complete": bool(session.get("completed")),
        "capture": str(capture_path),
        "labels": str(label_path),
        "session": str(session_path),
        "frames": frame_count,
        "trainableFrames": int(np.count_nonzero(trainable)),
        "excludedFrames": int(np.count_nonzero(~trainable)),
        "imageSize": arguments.size,
        "cameraOrder": ["left_face_camera2", "right_face_camera3"],
        "targetNames": list(TONGUE_TARGET_NAMES),
        "factoryExpressionNames": names,
        "capturePolicy": "manually triggered exact synchronized stereo frames",
        "medianLabelErrorMs": float(np.median(errors_ms)),
        "maximumLabelErrorMs": float(np.max(errors_ms)),
    }
    (output / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    size_bytes = sum(path.stat().st_size for path in output.iterdir() if path.is_file())
    print(f"Manual tongue cache complete: {output.resolve()}")
    print(
        f"Usable stills: {int(np.count_nonzero(trainable))}/{frame_count}; "
        f"cache: {size_bytes / 1_000_000:.1f} MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
