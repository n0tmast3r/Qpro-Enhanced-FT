import unittest

import numpy as np
import torch

from model_preview import LiveModelPreview, Prediction
from prepare_training import nearest_label_indices
from train_model import QuestProTrackingModel, blocked_split


class TrainingPipelineTests(unittest.TestCase):
    def test_nearest_label_alignment(self) -> None:
        indices, errors = nearest_label_indices([11, 29, 51], [10, 30, 50])
        np.testing.assert_array_equal(indices, [0, 1, 2])
        np.testing.assert_allclose(errors, [0.000001, 0.000001, 0.000001])

    def test_blocked_split_covers_every_step(self) -> None:
        step_ids = np.repeat(np.arange(3, dtype=np.int16), 270)
        training, validation = blocked_split(step_ids, block_size=45)
        self.assertFalse(np.intersect1d(training, validation).size)
        self.assertEqual(len(training) + len(validation), len(step_ids))
        self.assertEqual(set(step_ids[validation]), {0, 1, 2})

    def test_model_shapes_and_ranges(self) -> None:
        model = QuestProTrackingModel()
        expressions, orientations = model(torch.rand(2, 5, 96, 96))
        self.assertEqual(tuple(expressions.shape), (2, 70))
        self.assertEqual(tuple(orientations.shape), (2, 8))
        self.assertTrue(torch.all(expressions >= 0))
        self.assertTrue(torch.all(expressions <= 1))
        torch.testing.assert_close(
            torch.linalg.vector_norm(orientations[:, :4], dim=1), torch.ones(2)
        )
        torch.testing.assert_close(
            torch.linalg.vector_norm(orientations[:, 4:], dim=1), torch.ones(2)
        )

    def test_live_comparison_render(self) -> None:
        preview = LiveModelPreview.__new__(LiveModelPreview)
        preview.expression_names = [f"Expression{index}" for index in range(70)]
        prediction = Prediction(
            expressions=np.linspace(0, 1, 70, dtype=np.float32),
            eye_orientations=np.asarray([0, 0, 0, 1, 0, 0, 0, 1], dtype=np.float32),
            inference_ms=2.0,
        )
        sample = {
            "arrivalMonotonicNs": 1_000_000_000,
            "values": np.linspace(1, 0, 70, dtype=np.float32).tolist(),
            "leftEyeOrientation": [0, 0, 0, 1],
            "rightEyeOrientation": [0, 0, 0, 1],
        }
        image = preview.render(prediction, sample, 1_005_000_000, labels_live=True)
        self.assertEqual(image.shape, (760, 1100, 3))
        self.assertGreater(int(image.max()), 0)


if __name__ == "__main__":
    unittest.main()
