import json
import collections
import threading
import unittest

import numpy as np

from stereo_eye_calibration import (
    _JsonObjectStream,
    StereoEyeCalibrationController,
    TargetState,
    apply_eye_mapping,
    fit_convergence_mapping,
    fit_eye_mapping,
    polynomial_features,
    quaternion_yaw_pitch,
)


class StereoEyeCalibrationTests(unittest.TestCase):
    def test_target_at_interpolates_capture_time_from_history(self) -> None:
        controller = StereoEyeCalibrationController.__new__(
            StereoEyeCalibrationController
        )
        controller._lock = threading.RLock()
        controller.target_history = collections.deque(
            [
                TargetState(1_000_000_000, 0.0, -10.0, 2.0, -8.0, 0.5),
                TargetState(1_020_000_000, 4.0, 10.0, 6.0, 12.0, 1.5),
            ],
            maxlen=16,
        )
        target = controller.target_at(1_005_000_000)
        self.assertIsNotNone(target)
        assert target is not None
        self.assertAlmostEqual(target.left_yaw, -5.0)
        self.assertAlmostEqual(target.right_pitch, 3.0)
        self.assertAlmostEqual(target.distance, 0.75)

    def test_target_at_rejects_stale_history(self) -> None:
        controller = StereoEyeCalibrationController.__new__(
            StereoEyeCalibrationController
        )
        controller._lock = threading.RLock()
        controller.target_history = collections.deque(
            [TargetState(1_000_000_000, 0.0, 0.0, 0.0, 0.0, 1.0)], maxlen=16
        )
        self.assertIsNone(controller.target_at(1_250_000_000))

    def test_identity_factory_quaternion_looks_forward(self) -> None:
        yaw, pitch = quaternion_yaw_pitch([0.0, 0.0, 0.0, 1.0])
        self.assertAlmostEqual(yaw, 0.0)
        self.assertAlmostEqual(pitch, 0.0)

    def test_json_stream_handles_concatenated_and_split_packets(self) -> None:
        decoder = _JsonObjectStream()
        first = {"PacketName": "One", "PacketData": {"text": "a}b"}}
        second = {"PacketName": "Two", "PacketData": {"value": 2}}
        payload = json.dumps(first) + json.dumps(second)
        self.assertEqual(decoder.feed(payload[:17]), [])
        self.assertEqual(decoder.feed(payload[17:]), [first, second])

    def test_polynomial_mapping_recovers_each_eye(self) -> None:
        rng = np.random.default_rng(42)
        samples = []
        left_coefficients = np.array(
            [[1.0, -2.0], [20.0, 1.5], [2.0, 16.0], [1.2, 0.4],
             [-0.7, 0.9], [0.3, -1.0]],
            dtype=np.float64,
        )
        right_coefficients = np.array(
            [[-0.5, 1.0], [18.0, -0.8], [-1.5, 17.0], [0.6, -0.3],
             [0.8, 0.5], [-0.4, 0.7]],
            dtype=np.float64,
        )
        for index in range(600):
            left_raw = rng.uniform(-0.9, 0.9, size=2)
            right_raw = rng.uniform(-0.9, 0.9, size=2)
            left_target = polynomial_features(*left_raw) @ left_coefficients
            right_target = polynomial_features(*right_raw) @ right_coefficients
            samples.append(
                {
                    "timestamp_ns": index * 100_000_000,
                    "phase": "gaze" if index < 450 else "convergence",
                    "distance_m": 0.5 + (index % 150) / 100.0,
                    "left_raw": left_raw.tolist(),
                    "right_raw": right_raw.tolist(),
                    "left_target_deg": left_target.tolist(),
                    "right_target_deg": right_target.tolist(),
                }
            )
        left, _, _ = fit_eye_mapping(samples, "left")
        right, _, _ = fit_eye_mapping(samples, "right")
        self.assertLess(left["held_out"]["angular_mae_deg"], 0.02)
        self.assertLess(right["held_out"]["angular_mae_deg"], 0.02)
        left_value = apply_eye_mapping(left, 0.25, -0.4)
        expected = polynomial_features(0.25, -0.4) @ left_coefficients
        np.testing.assert_allclose(left_value, expected, atol=0.02)
        convergence = fit_convergence_mapping(samples, smoothing=1.0)
        self.assertLess(convergence["held_out"]["mae_deg"], 0.03)
        self.assertLess(convergence["held_out"]["p95_deg"], 0.05)


if __name__ == "__main__":
    unittest.main()
