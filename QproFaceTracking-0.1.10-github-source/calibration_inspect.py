#!/usr/bin/env python3
"""Audit a user-paced calibration by active step."""

from __future__ import annotations

import argparse
import bisect
import json
from pathlib import Path

import numpy as np

from dataset_inspect import camera_timestamps, load_labels


EXPECTED: dict[str, tuple[str, ...]] = {
    "Look left": ("EyesLookLeftL", "EyesLookLeftR"),
    "Look right": ("EyesLookRightL", "EyesLookRightR"),
    "Look up": ("EyesLookUpL", "EyesLookUpR"),
    "Look down": ("EyesLookDownL", "EyesLookDownR"),
    "Natural blinks": ("EyesClosedL", "EyesClosedR"),
    "Slow blinks": ("EyesClosedL", "EyesClosedR"),
    "Left wink": ("EyesClosedL",),
    "Right wink": ("EyesClosedR",),
    "Eyes wide": ("UpperLidRaiserL", "UpperLidRaiserR"),
    "Eye squint": ("LidTightenerL", "LidTightenerR"),
    "Brows up": (
        "InnerBrowRaiserL", "InnerBrowRaiserR",
        "OuterBrowRaiserL", "OuterBrowRaiserR",
    ),
    "Inner brows": ("InnerBrowRaiserL", "InnerBrowRaiserR"),
    "Outer brows": ("OuterBrowRaiserL", "OuterBrowRaiserR"),
    "Brows down": ("BrowLowererL", "BrowLowererR"),
    "Left brow": ("InnerBrowRaiserL", "OuterBrowRaiserL"),
    "Right brow": ("InnerBrowRaiserR", "OuterBrowRaiserR"),
    "Jaw open": ("JawDrop",),
    "Jaw left": ("JawSidewaysLeft",),
    "Jaw right": ("JawSidewaysRight",),
    "Jaw forward": ("JawThrust",),
    "Smile": ("LipCornerPullerL", "LipCornerPullerR"),
    "Frown": ("LipCornerDepressorL", "LipCornerDepressorR"),
    "Smile sides": ("LipCornerPullerL", "LipCornerPullerR"),
    "Pucker": ("LipPuckerL", "LipPuckerR"),
    "Funnel O": (
        "LipFunnelerLb", "LipFunnelerLt", "LipFunnelerRb", "LipFunnelerRt",
    ),
    "Stretch": ("LipStretcherL", "LipStretcherR"),
    "Press lips": ("LipPressorL", "LipPressorR"),
    "Suck lips": ("LipSuckLb", "LipSuckLt", "LipSuckRb", "LipSuckRt"),
    "Upper lip": (
        "UpperLipRaiserL", "UpperLipRaiserR", "NoseWrinklerL", "NoseWrinklerR",
    ),
    "Lower lip": ("LowerLipDepressorL", "LowerLipDepressorR"),
    "Cheeks puff": ("CheekPuffL", "CheekPuffR"),
    "Cheeks suck": ("CheekSuckL", "CheekSuckR"),
    "Tongue out": ("TongueOut",),
    "Tongue motion": (
        "TongueTipInterdental", "TongueTipAlveolar", "TongueFrontDorsalPalate",
        "TongueMidDorsalPalate", "TongueBackDorsalVelar", "TongueOut",
        "TongueRetreat",
    ),
}


def step_intervals(session: dict[str, object]) -> list[list[tuple[int, int]]]:
    intervals: list[list[tuple[int, int]]] = [
        [] for _step in session["steps"]
    ]
    active_step: int | None = None
    active_start: int | None = None
    for event in session["events"]:
        step = int(event["step"])
        timestamp = int(event["monotonicNs"])
        if event["event"] == "step_started":
            if active_step is not None and active_start is not None:
                intervals[active_step].append((active_start, timestamp))
            active_step = step
            active_start = timestamp
        elif event["event"] == "step_restarted":
            if active_step is not None and active_start is not None:
                intervals[active_step].append((active_start, timestamp))
            active_step = step
            active_start = timestamp
        elif event["event"] == "step_finished":
            if active_step is not None and active_start is not None:
                intervals[active_step].append((active_start, timestamp))
            active_step = None
            active_start = None
        elif event["event"] == "step_back":
            if active_step is not None and active_start is not None:
                intervals[active_step].append((active_start, timestamp))
            active_step = None
            active_start = None
        elif event["event"] == "step_skipped":
            if active_step is not None and active_start is not None:
                intervals[active_step].append((active_start, timestamp))
            active_step = None
            active_start = None
    if active_step is not None:
        raise ValueError(f"Step {active_step + 1} has an unfinished active interval")
    skipped = {int(value) for value in session.get("skippedSteps", [])}
    for step, values in enumerate(intervals):
        if not values and step not in skipped:
            raise ValueError(f"Step {step + 1} has no complete active interval")
    return intervals


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect calibration step coverage")
    parser.add_argument("capture")
    parser.add_argument("--labels")
    parser.add_argument("--session")
    arguments = parser.parse_args()

    capture_path = Path(arguments.capture).resolve()
    label_path = (
        Path(arguments.labels).resolve()
        if arguments.labels else capture_path.with_suffix(".qplabel.jsonl")
    )
    session_path = (
        Path(arguments.session).resolve()
        if arguments.session else capture_path.with_suffix(".qpsession.json")
    )
    session = json.loads(session_path.read_text(encoding="utf-8"))
    if not session.get("completed"):
        raise ValueError("Calibration session did not complete")

    frame_times = camera_timestamps(capture_path)
    names, samples = load_labels(label_path)
    name_to_index = {name: index for index, name in enumerate(names)}
    label_times = [int(sample["arrivalMonotonicNs"]) for sample in samples]
    weights = np.asarray([sample["values"] for sample in samples], dtype=np.float32)
    intervals = step_intervals(session)

    total_duration = 0.0
    total_frames = 0
    weak_expected: list[str] = []
    print("Step coverage (active interval includes repeats and post-cue time):")
    print(" #  step                 sec tries frames   fps expected-range  strongest-label")
    for index, (active_intervals, step) in enumerate(zip(intervals, session["steps"])):
        frame_count = 0
        duration = 0.0
        label_segments: list[np.ndarray] = []
        for start, finish in active_intervals:
            frame_start = bisect.bisect_left(frame_times, start)
            frame_finish = bisect.bisect_right(frame_times, finish)
            label_start = bisect.bisect_left(label_times, start)
            label_finish = bisect.bisect_right(label_times, finish)
            frame_count += frame_finish - frame_start
            duration += (finish - start) / 1e9
            if label_finish > label_start:
                label_segments.append(weights[label_start:label_finish])
        fps = frame_count / duration if duration else 0.0
        segment = (
            np.concatenate(label_segments, axis=0)
            if label_segments else np.empty((0, len(names)), dtype=np.float32)
        )
        ranges = np.ptp(segment, axis=0) if len(segment) else np.zeros(len(names))
        strongest_index = int(np.argmax(ranges)) if len(ranges) else 0
        strongest = (
            f"{names[strongest_index]}={ranges[strongest_index]:.2f}"
            if names else "none"
        )
        step_name = str(step["name"])
        expected_names = EXPECTED.get(step_name, ())
        expected_ranges = [
            float(ranges[name_to_index[name]])
            for name in expected_names if name in name_to_index
        ]
        expected_text = (
            f"{max(expected_ranges):.2f}" if expected_ranges else "mixed"
        )
        if expected_ranges and max(expected_ranges) < 0.10:
            weak_expected.append(step_name)
        print(
            f"{index + 1:2d}  {step_name[:20]:20s} {duration:5.1f} "
            f"{len(active_intervals):5d} {frame_count:6d} {fps:5.1f} "
            f"{expected_text:>14s}  {strongest}"
        )
        total_duration += duration
        total_frames += frame_count

    print(
        f"Active total: {total_duration:.1f} s; frames: {total_frames}; "
        f"active average: {total_frames / total_duration:.2f} FPS"
    )
    if weak_expected:
        print("Expected range below 0.10: " + ", ".join(weak_expected))
    else:
        print("Every single-expression step reached at least 0.10 expected range.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
