import json
import tempfile
import unittest
from pathlib import Path

from calibration import CalibrationSession, STEPS, prompt_image


class CalibrationTests(unittest.TestCase):
    def test_user_paced_progress_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.qpsession.json"
            session = CalibrationSession(path)
            started = 1_000_000_000
            index, step, phase, elapsed, remaining = session.status(started)
            self.assertEqual(index, 0)
            self.assertEqual(step, STEPS[0])
            self.assertEqual(phase, "instruction")
            self.assertEqual(elapsed, 0.0)
            self.assertAlmostEqual(remaining, STEPS[0].seconds)

            active = started + 500_000_000
            self.assertEqual(session.handle_key(" ", active), "started")
            self.assertEqual(session.status(active)[2], "active")
            self.assertEqual(
                session.handle_key(" ", active + 100_000_000), "not_ready"
            )
            self.assertEqual(session.status(active + 100_000_000)[2], "active")

            ready = active + int((STEPS[0].seconds + 0.1) * 1e9)
            self.assertTrue(session.update(ready))
            self.assertFalse(session.update(ready))
            self.assertEqual(session.status(ready)[2], "ready")

            advanced = ready + 100_000_000
            self.assertEqual(session.handle_key("\r", advanced), "advanced")
            self.assertEqual(session.status(advanced)[0], 1)
            self.assertEqual(session.status(advanced)[2], "instruction")

            session.finish(advanced, completed=False)
            metadata = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(metadata["completed"])
            self.assertTrue(metadata["userPaced"])
            self.assertEqual(
                metadata["minimumRecommendedSeconds"], session.total_seconds
            )
            self.assertEqual(len(metadata["steps"]), len(STEPS))
            self.assertGreaterEqual(len(metadata["events"]), 4)

    def test_prompt_is_visible_size(self) -> None:
        image = prompt_image(
            0, STEPS[0], "instruction", 0.0, STEPS[0].seconds
        )
        self.assertEqual(image.shape, (650, 1100, 3))
        self.assertGreater(int(image.max()), 0)

    def test_gaze_target_moves_during_active_step(self) -> None:
        gaze_step = next(step for step in STEPS if step.name == "Look left")
        centered = prompt_image(1, gaze_step, "active", 0.0, gaze_step.seconds)
        moved = prompt_image(1, gaze_step, "active", 1.0, gaze_step.seconds - 1)
        self.assertFalse((centered == moved).all())


if __name__ == "__main__":
    unittest.main()
