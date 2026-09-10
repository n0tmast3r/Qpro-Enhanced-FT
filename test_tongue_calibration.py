import unittest

from calibration import CalibrationSession, target_activation
import numpy as np

from prepare_tongue_training import apply_training_overrides, target_vector
from tongue_calibration import TONGUE_STEPS, TONGUE_TARGET_NAMES


class TongueCalibrationTests(unittest.TestCase):
    def test_prompt_set_has_hard_negatives_and_edge_contexts(self):
        tags = {tag for step in TONGUE_STEPS for tag in step.tags}
        self.assertIn("negative", tags)
        self.assertIn("closed_lips", tags)
        self.assertIn("teeth_visible", tags)
        self.assertIn("jaw_wide", tags)
        self.assertIn("direction", tags)
        self.assertIn("shape", tags)

    def test_all_targets_are_known_and_bounded(self):
        known = set(TONGUE_TARGET_NAMES)
        for step in TONGUE_STEPS:
            self.assertTrue(set(step.targets).issubset(known))
            for value in step.targets.values():
                self.assertGreaterEqual(value, -1.0)
                self.assertLessEqual(value, 1.0)

    def test_session_uses_specialized_steps(self):
        session = CalibrationSession(
            "unused.json", steps=TONGUE_STEPS, session_type="tongue-stereo-v1"
        )
        self.assertEqual(len(session.steps), len(TONGUE_STEPS))
        self.assertEqual(session.session_type, "tongue-stereo-v1")
        self.assertGreater(session.total_seconds, 300)

    def test_pulse_has_neutral_ramp_hold_and_return(self):
        pulse = next(step for step in TONGUE_STEPS if step.pattern == "pulse")
        self.assertEqual(target_activation(pulse, 0.2), 0.0)
        self.assertGreater(target_activation(pulse, 1.0), 0.0)
        self.assertEqual(target_activation(pulse, 2.0), 1.0)
        self.assertGreater(target_activation(pulse, 3.0), 0.0)
        self.assertEqual(target_activation(pulse, 3.8), 0.0)

    def test_capture_specific_polarity_can_be_inverted(self):
        pulse = TONGUE_STEPS[8]
        normal_start = target_vector(pulse, 0.2)
        inverted_start = target_vector(pulse, 0.2, invert_activation=True)
        normal_peak = target_vector(pulse, 2.0)
        inverted_peak = target_vector(pulse, 2.0, invert_activation=True)
        self.assertEqual(float(normal_start[0]), 0.0)
        self.assertEqual(float(inverted_start[0]), 1.0)
        self.assertEqual(float(normal_peak[0]), 1.0)
        self.assertEqual(float(inverted_peak[0]), 0.0)

    def test_impossible_shape_can_be_skipped_without_fake_labels(self):
        session = CalibrationSession(
            "unused.json", steps=TONGUE_STEPS[:2], session_type="tongue-stereo-v1"
        )
        self.assertEqual(session.handle_key("k", 100), "skipped")
        self.assertEqual(session.current_index, 1)
        self.assertEqual(session.skipped_steps, {0})

    def test_review_edits_trim_and_exclude_without_changing_stereo_rows(self):
        step_ids = np.asarray([0, 0, 0, 1, 1], dtype=np.int16)
        elapsed = np.asarray([0.1, 1.0, 5.9, 0.2, 1.5], dtype=np.float32)
        trainable = np.ones(5, dtype=np.bool_)
        overrides = {
            "excludedStepIds": [1],
            "stepEdits": {
                "0": {
                    "mode": "normal",
                    "trimStartSeconds": 0.5,
                    "trimEndSeconds": 0.5,
                }
            },
        }
        result = apply_training_overrides(
            step_ids, elapsed, trainable, TONGUE_STEPS, overrides
        )
        self.assertEqual(result.tolist(), [False, True, False, False, False])


if __name__ == "__main__":
    unittest.main()
