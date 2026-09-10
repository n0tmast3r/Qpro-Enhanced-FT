import json
import struct
import tempfile
import unittest
from pathlib import Path

from capture_format import CaptureWriter, TRANSPORT_HEADER, inspect_capture
from tongue_still_capture import (
    TONGUE_ARC_PROMPTS,
    TONGUE_CORRECTION_PROMPTS,
    TONGUE_REFINEMENT_PROMPTS,
    TONGUE_STILL_PROMPTS,
    TongueStillCaptureSession,
)


class TongueStillCaptureTests(unittest.TestCase):
    def test_space_writes_one_exact_frame_and_x_excludes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = CaptureWriter(root / "test.qpcap")
            session = TongueStillCaptureSession(root / "test.qpsession.json")
            payload = bytes(1200 * 400)
            header = TRANSPORT_HEADER.pack(
                b"QPLIVE3\0", 3, TRANSPORT_HEADER.size, 1, 2,
                1200, 400, 1200, 1, len(payload), 0x1C, 0,
            )
            self.assertEqual(session.handle_key(" "), "started")
            self.assertTrue(session.consume_frame(writer, header, payload, 10, 20))
            self.assertFalse(session.consume_frame(writer, header, payload, 11, 21))
            self.assertEqual(writer.frame_count, 1)
            self.assertEqual(session.handle_key("x"), "back")
            session.finish(False)
            writer.close(completed=False)
            payload_json = json.loads((root / "test.qpsession.json").read_text())
            self.assertTrue(payload_json["samples"][0]["excluded"])
            self.assertEqual(inspect_capture(root / "test.qpcap")["scanned_frames"], 1)

    def test_enter_requires_minimum_manual_repetitions(self):
        with tempfile.TemporaryDirectory() as directory:
            session = TongueStillCaptureSession(Path(directory) / "test.qpsession.json")
            self.assertEqual(session.handle_key("\r"), "not_ready")
            self.assertEqual(session.current_index, 0)

    def test_correction_session_records_distinct_type_and_exact_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "correction.qpsession.json"
            session = TongueStillCaptureSession(
                path,
                prompts=TONGUE_CORRECTION_PROMPTS,
                session_type="tongue-stereo-corrections-v1",
                title="corrections",
            )
            data = json.loads(path.read_text())
            self.assertEqual(data["sessionType"], "tongue-stereo-corrections-v1")
            self.assertGreaterEqual(len(data["prompts"]), 20)
            hidden = [value for value in data["prompts"] if not value["targets"]]
            visible = [value for value in data["prompts"] if value["targets"].get("visibility")]
            self.assertGreaterEqual(len(hidden), 10)
            self.assertGreaterEqual(len(visible), 10)
            self.assertTrue(any("shirt" in value["name"].lower() for value in hidden))
            # Every direction correction is a fixed numeric target, never an
            # unlabeled sweep whose frames would carry ambiguous intensities.
            for value in visible:
                for target in value["targets"].values():
                    self.assertIsInstance(target, (int, float))

    def test_refinement_set_is_compact_and_covers_visible_directions(self):
        names = [value.name.lower() for value in TONGUE_REFINEMENT_PROMPTS]
        self.assertTrue(any("straight tongue" in name for name in names))
        self.assertFalse(any("shirt" in name for name in names))
        self.assertFalse(any("jaw wide" in name for name in names))
        self.assertTrue(all(value.recommended_captures <= 6 for value in TONGUE_REFINEMENT_PROMPTS))
        directions = {
            (float(value.targets.get("horizontal", 0.0)), float(value.targets.get("vertical", 0.0)))
            for value in TONGUE_REFINEMENT_PROMPTS if value.targets.get("visibility")
        }
        for coordinate in ((-0.85, 0), (0.85, 0), (0, 0.85), (0, -0.85),
                           (-0.75, 0.75), (0.75, 0.75), (-0.75, -0.75), (0.75, -0.75)):
            self.assertIn(coordinate, directions)

    def test_full_set_omits_shapes_the_lower_cameras_cannot_supervise(self):
        names = " ".join(value.name.lower() for value in TONGUE_STILL_PROMPTS)
        for unsupported in ("inside left cheek", "inside right cheek", "roll", "squish", "flat / thin", "twist"):
            self.assertNotIn(unsupported, names)

    def test_arc_set_covers_full_vertical_at_five_horizontal_positions(self):
        coordinates = {
            (float(value.targets["horizontal"]), float(value.targets["vertical"]))
            for value in TONGUE_ARC_PROMPTS
        }
        expected = {
            (horizontal, vertical)
            for horizontal in (-1.0, -0.5, 0.0, 0.5, 1.0)
            for vertical in (-1.0, 1.0)
        }
        self.assertEqual(coordinates, expected)
        self.assertEqual(len(TONGUE_ARC_PROMPTS), 10)


if __name__ == "__main__":
    unittest.main()
