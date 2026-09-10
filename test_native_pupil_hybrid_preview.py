import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from native_pupil_hybrid_preview import NativePupilHybridModel
from pupil_gaze_calibration import PUPIL_MEAN, PUPIL_SCALE, fit_pupil_stereo_mapping


class NativePupilHybridPreviewTests(unittest.TestCase):
    def test_midpoint_plus_disparity_preserves_unilateral_motion(self) -> None:
        samples = []
        for index in range(700):
            disparity = -2.0 - index / 200.0
            left = np.asarray([0.0, 0.0, 0.0])
            right = np.asarray([disparity / 8.0, 0.0, 0.0])
            samples.append({
                "phase": "convergence",
                "left_raw": (left * PUPIL_SCALE["left"] + PUPIL_MEAN["left"]).tolist(),
                "right_raw": (right * PUPIL_SCALE["right"] + PUPIL_MEAN["right"]).tolist(),
                "left_target_deg": [-disparity * 0.5, 0.0],
                "right_target_deg": [disparity * 0.5, 0.0],
            })
        stereo = fit_pupil_stereo_mapping(samples, lag_frames=0)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pupil_path = root / "pupil.json"
            factory_path = root / "factory.json"
            pupil_path.write_text(json.dumps({
                "format": "qpro-independent-neural-pupil-calibration-v2", "stereo": stereo
            }))
            identity = {"coefficients": [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]}
            factory_path.write_text(json.dumps({
                "factory_baseline": {"left_gaze": identity, "right_gaze": identity}
            }))
            model = NativePupilHybridModel(pupil_path, factory_path)
            factory_sample = {
                "leftEyeIsValid": True, "rightEyeIsValid": True,
                "leftEyeConfidence": 1.0, "rightEyeConfidence": 1.0,
                "leftEyeOrientation": [0.0, 0.0, 0.0, 1.0],
                "rightEyeOrientation": [0.0, 0.0, 0.0, 1.0],
                "leftEyePosition": [-0.0325, 0.0, 0.0],
                "rightEyePosition": [0.0325, 0.0, 0.0],
            }
            prediction = model.predict(
                np.asarray(samples[400]["left_raw"]),
                np.asarray(samples[400]["right_raw"]),
                factory_sample,
            )
            self.assertAlmostEqual(
                prediction.right_gaze_deg[0] - prediction.left_gaze_deg[0],
                -4.0,
                delta=0.1,
            )


if __name__ == "__main__":
    unittest.main()
