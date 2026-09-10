import unittest

import numpy as np

from pupil_gaze_calibration import (
    PUPIL_MEAN,
    PUPIL_SCALE,
    apply_direction_aware_convergence_mapping,
    apply_pupil_eye_mapping,
    apply_pupil_stereo_mapping,
    build_meta_teacher_samples,
    filter_recorded_pupil_samples,
    fit_pupil_eye_mapping,
    fit_pupil_calibration,
    fit_safe_pupil_eye_mapping,
    fit_pupil_stereo_mapping,
    fit_direction_aware_convergence_mapping,
)


class PupilGazeCalibrationTests(unittest.TestCase):
    def test_meta_teacher_supplies_common_direction_not_fake_convergence(self) -> None:
        samples = []
        for index in range(240):
            disparity = 2.0 + np.sin(index * 0.07)
            common_yaw = 10.0 * np.sin(index * 0.03)
            left_raw = PUPIL_MEAN["left"] + np.asarray([
                common_yaw / 10.0, np.cos(index * 0.04), 0.0
            ]) * PUPIL_SCALE["left"]
            right_raw = PUPIL_MEAN["right"] + np.asarray([
                common_yaw / 10.0, np.cos(index * 0.04), 0.0
            ]) * PUPIL_SCALE["right"]
            samples.append({
                "timestamp_ns": index * 14_000_000,
                "phase": "gaze" if index < 120 else "convergence",
                "left_raw": left_raw.tolist(),
                "right_raw": right_raw.tolist(),
                "left_target_deg": [common_yaw - disparity * 0.5, 0.0],
                "right_target_deg": [common_yaw + disparity * 0.5, 0.0],
                "factory": {
                    "left_valid": True,
                    "right_valid": True,
                    "left_orientation": [0.0, 0.0, 0.0, 1.0],
                    "right_orientation": [0.0, 0.0, 0.0, 1.0],
                    "alignment_ms": 1.0,
                },
            })
        teacher, diagnostics = build_meta_teacher_samples(samples)
        self.assertGreaterEqual(len(teacher), 216)
        self.assertFalse(diagnostics["runtime_meta_required"])
        teacher_by_time = {row["timestamp_ns"]: row for row in teacher}
        for index in (20, 133, 210):
            row = teacher_by_time[samples[index]["timestamp_ns"]]
            original_disparity = (
                samples[index]["right_target_deg"][0]
                - samples[index]["left_target_deg"][0]
            )
            teacher_disparity = (
                row["right_target_deg"][0]
                - row["left_target_deg"][0]
            )
            teacher_common = (
                row["right_target_deg"][0]
                + row["left_target_deg"][0]
            ) * 0.5
            self.assertAlmostEqual(teacher_disparity, original_disparity)
            self.assertAlmostEqual(teacher_common, 0.0)
        result = fit_pupil_calibration(samples)
        self.assertEqual(
            result["format"], "qpro-independent-neural-pupil-calibration-v7"
        )
        self.assertIsNotNone(result["meta_teacher"])

    def test_direction_aware_convergence_recovers_depth_without_modifying_rays(self) -> None:
        samples = []
        left_prediction = []
        right_prediction = []
        for index in range(800):
            common_yaw = 24.0 * np.sin(index * 0.031)
            common_pitch = 16.0 * np.cos(index * 0.027)
            raw_disparity = 4.0 + 2.0 * np.sin(index * 0.019)
            expected = (
                3.0
                - 0.35 * raw_disparity
                + 0.002 * common_yaw * common_yaw
                + 0.001 * common_yaw * common_pitch
            )
            left = [common_yaw - raw_disparity * 0.5, common_pitch]
            right = [common_yaw + raw_disparity * 0.5, common_pitch]
            left_prediction.append(left)
            right_prediction.append(right)
            samples.append({
                "timestamp_ns": index * 13_000_000,
                "phase": "gaze" if index < 400 else "convergence",
                "left_target_deg": [-expected * 0.5, common_pitch],
                "right_target_deg": [expected * 0.5, common_pitch],
            })
        mapping = fit_direction_aware_convergence_mapping(
            samples, np.asarray(left_prediction), np.asarray(right_prediction)
        )
        self.assertLess(mapping["held_out"]["all"]["mae_deg"], 0.05)
        for index in (31, 411, 733):
            before_left = tuple(left_prediction[index])
            before_right = tuple(right_prediction[index])
            actual, in_range = apply_direction_aware_convergence_mapping(
                mapping, before_left, before_right
            )
            expected = (
                samples[index]["right_target_deg"][0]
                - samples[index]["left_target_deg"][0]
            )
            self.assertAlmostEqual(actual, expected, delta=0.05)
            self.assertEqual(tuple(left_prediction[index]), before_left)
            self.assertEqual(tuple(right_prediction[index]), before_right)
            self.assertTrue(in_range)

    def test_saved_sample_filter_preserves_unfiltered_audit_values(self) -> None:
        samples = []
        for index in range(5):
            left = PUPIL_MEAN["left"] + np.asarray([index, 0.0, 0.0]) * PUPIL_SCALE["left"]
            right = PUPIL_MEAN["right"]
            samples.append({
                "timestamp_ns": index * 14_000_000,
                "headset_kernel_time_s": index * 0.014,
                "phase": "gaze",
                "left_raw": left.tolist(),
                "right_raw": right.tolist(),
            })
        filtered = filter_recorded_pupil_samples(samples)
        self.assertEqual(filtered[-1]["left_raw_unfiltered"], samples[-1]["left_raw"])
        self.assertLess(filtered[-1]["left_raw"][0], samples[-1]["left_raw"][0])
        self.assertTrue(np.allclose(filtered[-1]["right_raw"], PUPIL_MEAN["right"]))

    def test_safe_mapping_has_no_cross_axis_interaction(self) -> None:
        samples = []
        for index in range(500):
            x = np.sin(index * 0.071)
            y = np.cos(index * 0.053)
            sample = {"timestamp_ns": index * 13_000_000}
            for eye in ("left", "right"):
                normalized = np.asarray([x, y, np.sin(index * 0.031)])
                sample[f"{eye}_raw"] = (
                    normalized * PUPIL_SCALE[eye] + PUPIL_MEAN[eye]
                ).tolist()
                sample[f"{eye}_target_deg"] = [15.0 * x, -12.0 * y]
            samples.append(sample)
        mapping, _ = fit_safe_pupil_eye_mapping(samples, "left")
        rest = PUPIL_MEAN["left"].copy()
        baseline = apply_pupil_eye_mapping(mapping, rest)
        z_changed = rest.copy()
        z_changed[2] += PUPIL_SCALE["left"][2]
        self.assertEqual(baseline, apply_pupil_eye_mapping(mapping, z_changed))

    def test_degree_two_mapping_recovers_each_eye(self) -> None:
        samples = []
        for index in range(500):
            x = np.sin(index * 0.071)
            y = np.cos(index * 0.053)
            z = np.sin(index * 0.037) * 0.5
            timestamp = index * 13_000_000
            sample = {"timestamp_ns": timestamp}
            for eye, sign in (("left", 1.0), ("right", -1.0)):
                normalized = np.asarray([x, y, z * sign])
                sample[f"{eye}_raw"] = (
                    normalized * PUPIL_SCALE[eye] + PUPIL_MEAN[eye]
                ).tolist()
                sample[f"{eye}_target_deg"] = [
                    16.0 * x + 2.0 * x * y + sign,
                    -12.0 * y + 1.5 * z * z,
                ]
            samples.append(sample)

        left, _prediction = fit_pupil_eye_mapping(samples, "left")
        right, _prediction = fit_pupil_eye_mapping(samples, "right")
        for index in (11, 123, 377):
            for eye, mapping in (("left", left), ("right", right)):
                expected = samples[index][f"{eye}_target_deg"]
                actual = apply_pupil_eye_mapping(mapping, samples[index][f"{eye}_raw"])
                self.assertAlmostEqual(actual[0], expected[0], delta=0.2)
                self.assertAlmostEqual(actual[1], expected[1], delta=0.2)

    def test_stereo_mapping_recovers_disparity(self) -> None:
        samples = []
        for index in range(700):
            left = np.asarray([
                np.sin(index * 0.031), np.cos(index * 0.019), np.sin(index * 0.013)
            ])
            right = np.asarray([
                np.cos(index * 0.029), np.sin(index * 0.023), np.cos(index * 0.017)
            ])
            disparity = 2.0 + 3.2 * (right[0] - left[0]) + 0.8 * left[1] * right[1]
            samples.append({
                "timestamp_ns": index * 13_000_000,
                "phase": "convergence",
                "left_raw": (left * PUPIL_SCALE["left"] + PUPIL_MEAN["left"]).tolist(),
                "right_raw": (right * PUPIL_SCALE["right"] + PUPIL_MEAN["right"]).tolist(),
                "left_target_deg": [-disparity * 0.5, 0.0],
                "right_target_deg": [disparity * 0.5, 0.0],
            })
        mapping = fit_pupil_stereo_mapping(samples, lag_frames=0)
        self.assertLess(mapping["held_out"]["mae_deg"], 0.1)
        for index in (17, 245, 611):
            actual = apply_pupil_stereo_mapping(
                mapping, samples[index]["left_raw"], samples[index]["right_raw"]
            )
            expected = (
                samples[index]["right_target_deg"][0]
                - samples[index]["left_target_deg"][0]
            )
            self.assertAlmostEqual(actual, expected, delta=0.1)


if __name__ == "__main__":
    unittest.main()
    apply_direction_aware_convergence_mapping,
