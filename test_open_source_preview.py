import unittest

import numpy as np

from open_source_preview import (
    FACE_OUTPUT_NAMES,
    OpenSourceModelPreview,
    OpenSourcePrediction,
    eye_tensor,
    face_tensor,
    fuse_face_outputs,
)


class OpenSourcePreviewTests(unittest.TestCase):
    def test_eye_preprocessing_matches_next_shape(self) -> None:
        tensor = eye_tensor(np.full((400, 400), 128, dtype=np.uint8))
        self.assertEqual(tensor.shape, (1, 3, 224, 224))
        self.assertEqual(tensor.dtype, np.float32)
        self.assertTrue(np.isfinite(tensor).all())

    def test_face_preprocessing_matches_babble_shape(self) -> None:
        tensor = face_tensor(np.full((400, 400), 255, dtype=np.uint8))
        self.assertEqual(tensor.shape, (1, 1, 224, 224))
        self.assertEqual(tensor.dtype, np.float32)
        self.assertAlmostEqual(float(tensor.min()), 1.0)
        self.assertAlmostEqual(float(tensor.max()), 1.0)

    def test_face_fusion_preserves_strongest_view(self) -> None:
        left = np.array([0.1, 0.8, 0.3], dtype=np.float32)
        right = np.array([0.7, 0.2, 0.4], dtype=np.float32)
        np.testing.assert_allclose(
            fuse_face_outputs(left, right), [0.7, 0.8, 0.4]
        )

    def test_all_camera_layout_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "400x2000"):
            OpenSourceModelPreview._panel(
                np.zeros((400, 1200), dtype=np.uint8), 0
            )

    def test_diagnostic_renderer(self) -> None:
        left_face = np.linspace(0.0, 0.8, len(FACE_OUTPUT_NAMES), dtype=np.float32)
        right_face = left_face[::-1].copy()
        prediction = OpenSourcePrediction(
            left_eye=np.array([0.7, 0.6, 0.2, -0.4, 0.3], dtype=np.float32),
            right_eye=np.array([0.4, 0.5, 0.3, 0.2, -0.1], dtype=np.float32),
            left_face=left_face,
            right_face=right_face,
            fused_face=fuse_face_outputs(left_face, right_face),
            inference_ms=8.5,
        )
        image = OpenSourceModelPreview.render(prediction)
        self.assertEqual(image.shape, (880, 1240, 3))
        self.assertEqual(image.dtype, np.uint8)
        self.assertGreater(int(np.count_nonzero(image)), 10_000)


if __name__ == "__main__":
    unittest.main()
