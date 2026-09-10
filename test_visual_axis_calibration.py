import unittest

import numpy as np

from visual_axis_calibration import (
    apply_axis_eye_mapping,
    evaluate_independent_convergence,
    fit_axis_eye_mapping,
)


class VisualAxisCalibrationTests(unittest.TestCase):
    @staticmethod
    def samples() -> list[dict[str, object]]:
        samples: list[dict[str, object]] = []
        for index in range(600):
            phase = "gaze" if index < 360 else "convergence"
            x = -25.0 + 50.0 * ((index % 60) / 59.0)
            y = -18.0 + 36.0 * (((index // 6) % 60) / 59.0)
            depth_delta = 0.0 if phase == "gaze" else 5.0 * np.sin(index * 0.08)
            left_raw = (x + depth_delta * 0.5, y)
            right_raw = (x - depth_delta * 0.5, y)
            left_target = (
                2.0 + 1.10 * left_raw[0] + 0.05 * left_raw[1],
                -1.0 + 0.03 * left_raw[0] + 0.95 * left_raw[1],
            )
            right_target = (
                -3.0 + 0.90 * right_raw[0] - 0.04 * right_raw[1],
                0.5 - 0.02 * right_raw[0] + 1.05 * right_raw[1],
            )
            samples.append(
                {
                    "timestamp_ns": index * 25_000_000,
                    "phase": phase,
                    "distance_m": 2.0 if phase == "gaze" else 0.2 + index / 300.0,
                    "left_raw": list(left_raw),
                    "right_raw": list(right_raw),
                    "left_target_deg": list(left_target),
                    "right_target_deg": list(right_target),
                }
            )
        return samples

    def test_affine_mapping_recovers_each_eye_without_cross_eye_inputs(self) -> None:
        gaze = [sample for sample in self.samples() if sample["phase"] == "gaze"]
        left = fit_axis_eye_mapping(gaze, "left")
        right = fit_axis_eye_mapping(gaze, "right")
        self.assertEqual(left["input_order"], ["1", "visual_yaw_deg", "visual_pitch_deg"])
        self.assertLess(left["held_out"]["angular_mae_deg"], 0.01)
        self.assertLess(right["held_out"]["angular_mae_deg"], 0.01)
        self.assertEqual(len(left["coefficients"]), 3)
        self.assertEqual(len(right["coefficients"]), 3)

    def test_convergence_is_derived_after_independent_mapping(self) -> None:
        samples = self.samples()
        gaze = [sample for sample in samples if sample["phase"] == "gaze"]
        left = fit_axis_eye_mapping(gaze, "left")
        right = fit_axis_eye_mapping(gaze, "right")
        metrics = evaluate_independent_convergence(samples, left, right)
        self.assertLess(metrics["convergence"]["mae_deg"], 0.01)
        mapped = apply_axis_eye_mapping(left, (4.0, -2.0))
        self.assertAlmostEqual(mapped[0], 6.3, places=3)
        self.assertAlmostEqual(mapped[1], -2.78, places=3)


if __name__ == "__main__":
    unittest.main()
