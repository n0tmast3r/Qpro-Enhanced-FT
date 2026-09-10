#!/usr/bin/env python3
"""Prompt set for a stereo Quest Pro tongue/mouth training session."""

from calibration import CalibrationStep


def step(
    name: str,
    instruction: str,
    targets: dict[str, float] | None = None,
    *tags: str,
    seconds: float = 8.0,
    pattern: str = "pulse",
) -> CalibrationStep:
    return CalibrationStep(
        name=name,
        instruction=instruction,
        seconds=seconds,
        targets=targets or {},
        tags=tuple(tags),
        pattern=pattern,
    )


OUT = {"visibility": 1.0, "extension": 1.0}
LEFT = {**OUT, "horizontal": -1.0}
RIGHT = {**OUT, "horizontal": 1.0}
UP = {**OUT, "vertical": 1.0}
DOWN = {**OUT, "vertical": -1.0}


TONGUE_STEPS = [
    step("Neutral closed", "Relax your jaw and keep the tongue fully inside.", None,
         "negative", "closed_lips", seconds=6, pattern="hold"),
    step("Natural speech", "Speak naturally without deliberately showing your tongue.", None,
         "negative", "speech", seconds=10, pattern="hold"),
    step("Smile with teeth", "Smile and show your teeth, then relax; keep tongue inside.", None,
         "negative", "teeth_visible"),
    step("Wide jaw, no tongue", "Open your mouth very wide and close it; keep tongue behind teeth.", None,
         "negative", "jaw_wide"),
    step("Pucker, no tongue", "Pucker and relax while keeping tongue fully inside.", None,
         "negative", "pucker"),
    step("Pressed lips", "Press lips firmly, release, and repeat; keep tongue inside.", None,
         "negative", "closed_lips"),
    step("Tongue inside left cheek", "Press tongue into the inside of your left cheek without exposing it.", None,
         "negative", "occluded", "left"),
    step("Tongue inside right cheek", "Press tongue into the inside of your right cheek without exposing it.", None,
         "negative", "occluded", "right"),
    step("Tongue tip barely visible", "Follow the meter: show only the tip, then fully retract.",
         {"visibility": 1.0, "extension": 0.2}, "tip", "jaw_relaxed"),
    step("Extension sweep", "Follow the meter from fully retracted to maximum extension.", OUT,
         "extension", "jaw_relaxed"),
    step("Maximum out, relaxed jaw", "Extend straight out as far as comfortable, then retract.", OUT,
         "extension", "jaw_relaxed"),
    step("Tongue between closed lips", "Gently close your lips around the extended tongue, then retract.", OUT,
         "closed_lips", "edge_case"),
    step("Tongue out, teeth visible", "Show your teeth with tongue extended straight through them, then retract.", OUT,
         "teeth_visible", "edge_case"),
    step("Tongue out, jaw wide", "Open wide and extend straight out, then fully retract.", OUT,
         "jaw_wide", "edge_case"),
    step("Tongue out while smiling", "Smile broadly with tongue straight out, then retract and relax.", OUT,
         "smile", "edge_case"),
    step("Tongue left, relaxed jaw", "Move the visible tip toward your left, following the meter.", LEFT,
         "jaw_relaxed", "direction"),
    step("Tongue right, relaxed jaw", "Move the visible tip toward your right, following the meter.", RIGHT,
         "jaw_relaxed", "direction"),
    step("Tongue up, relaxed jaw", "Lift the visible tip upward, following the meter.", UP,
         "jaw_relaxed", "direction"),
    step("Tongue down, relaxed jaw", "Lower the visible tip, following the meter.", DOWN,
         "jaw_relaxed", "direction"),
    step("Tongue left, jaw wide", "Open wide and move the tongue toward your left.", LEFT,
         "jaw_wide", "direction"),
    step("Tongue right, jaw wide", "Open wide and move the tongue toward your right.", RIGHT,
         "jaw_wide", "direction"),
    step("Tongue up, jaw wide", "Open wide and lift the tongue tip upward.", UP,
         "jaw_wide", "direction"),
    step("Tongue down, jaw wide", "Open wide and lower the tongue tip.", DOWN,
         "jaw_wide", "direction"),
    step("Tongue upper-left", "Move the visible tip diagonally up and toward your left.",
         {**LEFT, "vertical": 1.0}, "diagonal", "direction"),
    step("Tongue upper-right", "Move the visible tip diagonally up and toward your right.",
         {**RIGHT, "vertical": 1.0}, "diagonal", "direction"),
    step("Tongue lower-left", "Move the visible tip diagonally down and toward your left.",
         {**LEFT, "vertical": -1.0}, "diagonal", "direction"),
    step("Tongue lower-right", "Move the visible tip diagonally down and toward your right.",
         {**RIGHT, "vertical": -1.0}, "diagonal", "direction"),
    step("Curl tongue upward", "Extend and curl the tongue tip upward, then flatten and retract.",
         {**OUT, "curl_up": 1.0}, "shape"),
    step("Bend tongue downward", "Extend and bend the tongue tip downward, then retract.",
         {**OUT, "bend_down": 1.0}, "shape"),
    step("Roll tongue", "Extend your tongue rolled into a tube if possible, then relax.",
         {**OUT, "roll": 1.0}, "shape"),
    step("Tongue flat and wide", "Extend your tongue as flat and wide as comfortable.",
         {**OUT, "flat": 1.0}, "shape"),
    step("Tongue narrow", "Extend and make the tongue narrow or squished, then relax.",
         {**OUT, "squish": 1.0}, "shape"),
    step("Twist tongue left", "Extend and rotate the visible tongue toward your left if possible.",
         {**OUT, "twist": -1.0}, "shape"),
    step("Twist tongue right", "Extend and rotate the visible tongue toward your right if possible.",
         {**OUT, "twist": 1.0}, "shape"),
    step("Touch upper lip", "Extend and touch or lick the upper lip, then retract.", UP,
         "lip_contact"),
    step("Touch lower lip", "Extend and touch or lick the lower lip, then retract.", DOWN,
         "lip_contact"),
    step("Touch left lip corner", "Extend toward your left lip corner, then retract.", LEFT,
         "lip_contact"),
    step("Touch right lip corner", "Extend toward your right lip corner, then retract.", RIGHT,
         "lip_contact"),
    step("Cardinal sequence", "Keep tongue visible and slowly repeat left, right, up, down.", OUT,
         "validation", "free_motion", seconds=12, pattern="hold"),
    step("Natural tongue motion", "Vary extension, direction, jaw opening, lips, and teeth naturally.", OUT,
         "validation", "free_motion", seconds=15, pattern="hold"),
    step("Final neutral", "Fully retract your tongue, close your mouth gently, and relax.", None,
         "negative", "closed_lips", seconds=6, pattern="hold"),
]


TONGUE_TARGET_NAMES = (
    "visibility", "extension", "horizontal", "vertical", "curl_up",
    "bend_down", "roll", "flat", "squish", "twist",
)
