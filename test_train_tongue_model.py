import unittest

import numpy as np
import torch

from train_tongue_model import (
    SpatialStereoTongueModel,
    StereoTongueModel,
    balanced_step_weights,
    blocked_train_validation_split,
    classification_at_threshold,
    target_loss,
)


class TongueTrainingTests(unittest.TestCase):
    def test_split_excludes_untrainable_frames(self):
        steps = np.repeat(np.arange(3), 100)
        trainable = np.ones(len(steps), dtype=bool)
        trainable[200:] = False
        training, validation = blocked_train_validation_split(steps, trainable, 20)
        self.assertTrue(np.all(training < 200))
        self.assertTrue(np.all(validation < 200))
        self.assertFalse(set(training) & set(validation))

    def test_balancing_equalizes_prompt_mass(self):
        steps = np.asarray([0] * 10 + [1] * 100)
        indices = np.arange(len(steps))
        weights = balanced_step_weights(steps, indices)
        self.assertAlmostEqual(float(np.sum(weights[:10])), float(np.sum(weights[10:])))

    def test_model_bounds_signed_and_unsigned_outputs(self):
        names = [
            "visibility", "extension", "horizontal", "vertical", "curl_up",
            "bend_down", "roll", "flat", "squish", "twist",
        ]
        model = StereoTongueModel(names).eval()
        output = model(torch.zeros(2, 2, 160, 160))
        self.assertEqual(tuple(output.shape), (2, 10))
        self.assertTrue(torch.all(output[:, :2] >= 0))
        self.assertTrue(torch.all(output[:, :2] <= 1))
        self.assertTrue(torch.all(output[:, 2:4] >= -1))
        self.assertTrue(torch.all(output[:, 2:4] <= 1))

    def test_spatial_stereo_model_preserves_output_contract(self):
        names = [
            "visibility", "extension", "horizontal", "vertical", "curl_up",
            "bend_down", "roll", "flat", "squish", "twist",
        ]
        model = SpatialStereoTongueModel(names).eval()
        output = model(torch.zeros(1, 2, 224, 224))
        self.assertEqual(tuple(output.shape), (1, 10))
        self.assertTrue(torch.all(output[:, [0, 1, 4, 5, 6, 7, 8]] >= 0))
        self.assertTrue(torch.all(output[:, [0, 1, 4, 5, 6, 7, 8]] <= 1))

    def test_manual_still_split_holds_out_repetitions_per_card(self):
        steps = np.repeat(np.arange(3), 6)
        trainable = np.ones(len(steps), dtype=bool)
        training, validation = blocked_train_validation_split(
            steps, trainable, dataset_type="manual-stereo-stills"
        )
        self.assertEqual(len(training), 15)
        self.assertEqual(len(validation), 3)
        self.assertFalse(set(training) & set(validation))

    def test_visibility_loss_stays_finite_at_float16_probability_limits(self):
        names = [
            "visibility", "extension", "horizontal", "vertical", "curl_up",
            "bend_down", "roll", "flat", "squish", "twist",
        ]
        prediction = torch.zeros(2, 10, dtype=torch.float16)
        prediction[0, 0] = 1.0
        prediction[1, 0] = 0.0
        target = torch.zeros(2, 10, dtype=torch.float16)
        target[0, 0] = 1.0
        self.assertTrue(torch.isfinite(target_loss(prediction, target, names)))

    def test_classification_metrics_expose_false_positive_rate(self):
        values = np.asarray([0.9, 0.8, 0.7, 0.1])
        target = np.asarray([1.0, 0.0, 1.0, 0.0])
        metrics = classification_at_threshold(values, target, 0.5)
        self.assertAlmostEqual(metrics["precision"], 2 / 3)
        self.assertEqual(metrics["recall"], 1.0)
        self.assertEqual(metrics["falsePositiveRate"], 0.5)
        self.assertEqual(metrics["falseNegativeRate"], 0.0)


if __name__ == "__main__":
    unittest.main()
