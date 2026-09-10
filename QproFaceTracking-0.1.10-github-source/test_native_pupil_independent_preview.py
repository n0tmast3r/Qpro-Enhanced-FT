import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from native_pupil_independent_preview import IndependentPupilGazeModel
from pupil_gaze_calibration import PUPIL_MEAN, PUPIL_SCALE


class IndependentPupilPreviewTests(unittest.TestCase):
    def test_depth_rejects_divergent_rays_without_a_fake_distance_cutoff(self) -> None:
        distance, status = IndependentPupilGazeModel.depth_from_disparity(8.0, 0.065)
        self.assertIsNone(distance)
        self.assertIn("diverging", status)
        distance, status = IndependentPupilGazeModel.depth_from_disparity(-1.0, 0.065)
        self.assertAlmostEqual(distance, 3.72, delta=0.1)
        self.assertIn("geometric", status)
        distance, status = IndependentPupilGazeModel.depth_from_disparity(-7.4, 0.065)
        self.assertAlmostEqual(distance, 0.5, delta=0.05)
        self.assertIn("geometric", status)

    def test_unilateral_pupil_motion_cannot_move_other_eye_ray(self) -> None:
        def mapping(eye: str) -> dict[str, object]:
            return {
                "eye": eye,
                "model": "normalized-pupil-separable-degree2-ridge",
                "yaw_coefficients": [0.0, 10.0, 0.0],
                "pitch_coefficients": [0.0, 10.0, 0.0],
                "normalized_bounds": {
                    "low": [-2.0, -2.0, -2.0],
                    "high": [2.0, 2.0, 2.0],
                },
            }

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "calibration.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "qpro-independent-neural-pupil-calibration-v5",
                        "left": mapping("left"),
                        "right": mapping("right"),
                        "quality_gate": {
                            "status": "partial_pass",
                            "gaze_pass": True,
                            "convergence_pass": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            model = IndependentPupilGazeModel(path)
            left = PUPIL_MEAN["left"].copy()
            right_rest = PUPIL_MEAN["right"].copy()
            first = model.predict(left, right_rest)
            right_moved = right_rest + np.asarray([0.75, 0.0, 0.0]) * PUPIL_SCALE["right"]
            second = model.predict(left, right_moved)
            self.assertEqual(first.left_gaze_deg, second.left_gaze_deg)
            self.assertGreater(second.right_gaze_deg[0], first.right_gaze_deg[0] + 7.0)
            self.assertIsNone(second.estimated_distance_m)
            self.assertIn("disabled", second.fixation_status)


if __name__ == "__main__":
    unittest.main()
