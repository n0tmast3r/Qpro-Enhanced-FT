import tempfile
import unittest
from pathlib import Path

from research.inspect_seacliff_archives import inspect
from research.patch_seacliff_independent_axes import patch


ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / "research" / "seacliff_eye_model"


@unittest.skipUnless(
    (MODEL_DIR / "bolt.ptl").is_file() and (MODEL_DIR / "bolt-experimental.ptl").is_file(),
    "Meta eye archives are intentionally excluded from the public source repository",
)
class SeacliffResearchTests(unittest.TestCase):
    def test_experimental_archive_matches_active_archive(self):
        active = inspect(MODEL_DIR / "bolt.ptl")
        experimental = inspect(MODEL_DIR / "bolt-experimental.ptl")
        self.assertEqual(active["sha256"], experimental["sha256"])

    def test_local_branch_patch_preserves_public_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "patched.ptl"
            result = patch(MODEL_DIR / "bolt-experimental.ptl", destination)
            details = inspect(destination, node_ids={18, 52})
        self.assertIn("node 50 -> local per-eye node 18", result["redirect"])
        self.assertEqual(
            [item["shape"] for item in details["output"]],
            [[1, 4], [1, 6], [1, 6], [1, 1], [1, 1]],
        )
        selected = {node["id"]: node for node in details["selected_nodes"]}
        self.assertEqual(selected[18]["output"][0]["shape"], [2, 2])
        self.assertEqual(selected[52]["input"], [[18, 0], [51, 0]])


if __name__ == "__main__":
    unittest.main()
