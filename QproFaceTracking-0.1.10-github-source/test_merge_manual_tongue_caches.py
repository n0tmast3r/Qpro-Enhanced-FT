import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import merge_manual_tongue_caches


class MergeManualTongueCacheTests(unittest.TestCase):
    @staticmethod
    def _cache(path: Path, offset: int, trainable: list[bool]) -> None:
        path.mkdir()
        frames = len(trainable)
        metadata = {
            "datasetType": "manual-stereo-stills",
            "targetNames": ["visibility", "horizontal"],
            "factoryExpressionNames": ["TongueOut"],
        }
        (path / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        np.save(path / "images.npy", np.full((frames, 2, 16, 16), offset, np.uint8))
        np.save(path / "targets.npy", np.zeros((frames, 2), np.float32))
        np.save(path / "native_tongue_out.npy", np.zeros(frames, np.float32))
        np.save(path / "native_expressions.npy", np.zeros((frames, 1), np.float32))
        np.save(path / "timestamps.npy", np.arange(frames, dtype=np.int64) + offset)
        np.save(path / "step_ids.npy", np.asarray([0, 0, 1, 1][:frames], np.int16))
        np.save(path / "trainable.npy", np.asarray(trainable, np.bool_))

    def test_merge_filters_exclusions_and_keeps_sources_and_prompt_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, output = root / "first", root / "second", root / "out"
            self._cache(first, 10, [True, False, True, True])
            self._cache(second, 20, [True, True, True, True])
            arguments = [
                "merge_manual_tongue_caches.py",
                str(first), str(second), "--output", str(output), "--size", "128",
            ]
            with mock.patch.object(sys, "argv", arguments):
                self.assertEqual(merge_manual_tongue_caches.main(), 0)
            images = np.load(output / "images.npy")
            source_ids = np.load(output / "source_ids.npy")
            step_ids = np.load(output / "step_ids.npy")
            self.assertEqual(images.shape, (7, 2, 128, 128))
            self.assertEqual(list(source_ids), [0, 0, 0, 1, 1, 1, 1])
            self.assertTrue(set(step_ids[:3]).isdisjoint(set(step_ids[3:])))
            self.assertEqual(int(images[0, 0, 0, 0]), 10)
            self.assertEqual(int(images[-1, 0, 0, 0]), 20)


if __name__ == "__main__":
    unittest.main()
