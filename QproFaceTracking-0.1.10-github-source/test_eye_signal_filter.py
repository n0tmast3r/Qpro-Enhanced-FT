import unittest

import numpy as np

from eye_signal_filter import IndependentEyeFilter


class EyeSignalFilterTests(unittest.TestCase):
    def test_reduces_stationary_jitter(self) -> None:
        eye_filter = IndependentEyeFilter(2)
        random = np.random.default_rng(7)
        raw = random.normal(0.0, 0.2, (300, 2))
        filtered = []
        for index, value in enumerate(raw):
            left, _right = eye_filter.update(value, value, index / 77.0)
            filtered.append(left)
        self.assertLess(
            float(np.std(np.asarray(filtered)[50:])),
            float(np.std(raw[50:])) * 0.55,
        )

    def test_unilateral_motion_does_not_cross_between_filters(self) -> None:
        eye_filter = IndependentEyeFilter(2)
        left_outputs = []
        right_outputs = []
        for index in range(80):
            left = np.array([0.0, 0.0])
            right = np.array([1.0, 0.0]) if index >= 20 else np.array([0.0, 0.0])
            filtered_left, filtered_right = eye_filter.update(
                left, right, index / 77.0
            )
            left_outputs.append(filtered_left.copy())
            right_outputs.append(filtered_right.copy())
        self.assertLess(float(np.max(np.abs(left_outputs))), 1e-12)
        self.assertGreater(float(right_outputs[-1][0]), 0.95)


if __name__ == "__main__":
    unittest.main()
