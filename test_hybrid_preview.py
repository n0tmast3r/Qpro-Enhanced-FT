import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from hybrid_preview import (
    HybridModelPreview,
    apply_factory_mapping,
    convergence_distance_m,
    gaze_marker_position,
)
from open_source_preview import OpenSourcePrediction


class HybridPreviewTests(unittest.TestCase):
    @staticmethod
    def calibration() -> dict:
        identity_factory = {
            "coefficients": [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
        }
        return {
            "factory_baseline": {
                "left_gaze": identity_factory,
                "right_gaze": identity_factory,
            },
            "stereo": {
                "coefficients": [-4.0] + [0.0] * 14,
            },
        }

    def test_factory_identity_mapping(self) -> None:
        mapping = self.calibration()["factory_baseline"]["left_gaze"]
        yaw, pitch = apply_factory_mapping(mapping, [0.0, 0.0, 0.0, 1.0])
        self.assertAlmostEqual(yaw, 0.0)
        self.assertAlmostEqual(pitch, 0.0)

    def test_distance_from_vergence(self) -> None:
        self.assertIsNone(convergence_distance_m(0.0))
        distance = convergence_distance_m(-6.5, 0.065)
        self.assertIsNotNone(distance)
        self.assertAlmostEqual(distance, 0.573, places=2)

    def test_headset_angle_convention_draws_right_and_up(self) -> None:
        center = gaze_marker_position(0.0, 0.0, 0, 0, 320, 210)
        upper_right = gaze_marker_position(-20.0, 15.0, 0, 0, 320, 210)
        self.assertGreater(upper_right[0], center[0])
        self.assertLess(upper_right[1], center[1])

    def test_fusion_keeps_meta_direction_and_camera_disparity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hybrid.json"
            path.write_text(json.dumps(self.calibration()), encoding="utf-8")
            preview = HybridModelPreview(path)
            camera = OpenSourcePrediction(
                left_eye=np.zeros(5, dtype=np.float32),
                right_eye=np.zeros(5, dtype=np.float32),
                left_face=np.zeros(46, dtype=np.float32),
                right_face=np.zeros(46, dtype=np.float32),
                fused_face=np.zeros(46, dtype=np.float32),
                inference_ms=1.0,
            )
            sample = {
                "values": [0.75],
                "leftEyeIsValid": True,
                "rightEyeIsValid": True,
                "leftEyeConfidence": 0.99,
                "rightEyeConfidence": 0.98,
                "leftEyeOrientation": [0.0, 0.0, 0.0, 1.0],
                "rightEyeOrientation": [0.0, 0.0, 0.0, 1.0],
                "leftEyePosition": [-0.0325, 0.0, 0.0],
                "rightEyePosition": [0.0325, 0.0, 0.0],
            }
            result = preview.predict(camera, sample, ["InnerBrowRaiserL"])
            self.assertEqual(result.left_gaze_deg, (2.0, 0.0))
            self.assertEqual(result.right_gaze_deg, (-2.0, 0.0))
            self.assertEqual(result.factory_expressions["InnerBrowRaiserL"], 0.75)
            image = preview.render(result)
            self.assertEqual(image.shape, (880, 1240, 3))


if __name__ == "__main__":
    unittest.main()
