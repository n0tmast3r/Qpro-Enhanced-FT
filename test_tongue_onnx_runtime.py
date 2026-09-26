import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent


def _available(*modules: str) -> bool:
    return all(importlib.util.find_spec(module) is not None for module in modules)


def _directml_available() -> bool:
    if not _available("onnxruntime", "onnx", "torch"):
        return False
    import onnxruntime

    return "DmlExecutionProvider" in onnxruntime.get_available_providers()


def _write_checkpoint(path: Path, image_size: int, seed: int) -> None:
    import torch

    from tongue_calibration import TONGUE_TARGET_NAMES
    from train_tongue_model import create_model

    torch.manual_seed(seed)
    names = list(TONGUE_TARGET_NAMES)
    model = create_model("spatial-stereo-resnet-v2", names)
    torch.save(
        {
            "targetNames": names,
            "imageSize": image_size,
            "architecture": "spatial-stereo-resnet-v2",
            "modelState": model.state_dict(),
            "visibilityGate": {"cameraWeight": 0.5, "threshold": 0.44},
        },
        path,
    )


def _strips(count: int) -> list[np.ndarray]:
    rng = np.random.default_rng(7)
    return [rng.integers(0, 255, (400, 800), dtype=np.uint8) for _ in range(count)]


class LowMemoryRuntimeTests(unittest.TestCase):
    def test_preview_module_does_not_import_torch(self):
        # The RAM saving depends on the live receiver never importing PyTorch.
        probe = "import sys, tongue_model_preview; sys.exit(1 if 'torch' in sys.modules else 0)"
        result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT)
        self.assertEqual(result.returncode, 0)

    def test_receiver_limits_openblas_threads_before_numpy(self):
        source = (ROOT / "receiver.py").read_text(encoding="utf-8")
        limit = source.index('os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")')
        self.assertLess(limit, source.index("import numpy as np"))
        self.assertLess(limit, source.index("import cv2"))


@unittest.skipUnless(_available("torch", "onnx", "onnxruntime"), "needs torch, onnx and onnxruntime")
class OnnxTwinTests(unittest.TestCase):
    def test_twin_matches_checkpoint_and_records_its_digest(self):
        import onnxruntime
        import torch

        from export_tongue_onnx import ONNX_FORMAT, checkpoint_digest, export
        from train_tongue_model import create_model

        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "qpro-stereo-tongue-v99-gate.pt"
            _write_checkpoint(checkpoint, 64, seed=1)
            twin = export(checkpoint)
            self.assertEqual(twin, checkpoint.with_suffix(".onnx"))
            session = onnxruntime.InferenceSession(str(twin), providers=["CPUExecutionProvider"])
            metadata = session.get_modelmeta().custom_metadata_map
            self.assertEqual(metadata["sourceSha256"], checkpoint_digest(checkpoint))
            self.assertEqual(metadata["qproTongueOnnxFormat"], ONNX_FORMAT)
            self.assertEqual(metadata["imageSize"], "64")

            state = torch.load(checkpoint, map_location="cpu", weights_only=False)
            model = create_model(state["architecture"], state["targetNames"])
            model.load_state_dict(state["modelState"])
            model.eval()
            cameras = np.random.default_rng(3).random((1, 2, 64, 64), dtype=np.float32)
            with torch.inference_mode():
                expected = model(torch.from_numpy(cameras)).numpy()
            actual = session.run(None, {"cameras": cameras})[0]
            np.testing.assert_allclose(actual, expected, atol=1e-4)


@unittest.skipUnless(_directml_available(), "needs DirectML-enabled ONNX Runtime and torch")
class DirectMLPreviewTests(unittest.TestCase):
    def test_directml_predictions_match_pytorch_and_stale_twins_reconvert(self):
        from export_tongue_onnx import checkpoint_digest, onnx_path
        from tongue_model_preview import LiveTongueModelPreview

        with tempfile.TemporaryDirectory() as folder:
            gate = Path(folder) / "qpro-stereo-tongue-v99-gate.pt"
            direction = Path(folder) / "qpro-stereo-tongue-v99-direction.pt"
            _write_checkpoint(gate, 64, seed=1)
            _write_checkpoint(direction, 48, seed=2)

            directml = LiveTongueModelPreview(gate, "dml", direction_checkpoint_path=direction)
            reference = LiveTongueModelPreview(gate, "cpu", direction_checkpoint_path=direction)
            self.assertEqual(directml.device, "DirectML (ONNX Runtime)")
            self.assertEqual(directml.direction_image_size, 48)
            for strip in _strips(4):
                got = directml.predict(strip, None, [])
                expected = reference.predict(strip, None, [])
                np.testing.assert_allclose(got.values, expected.values, atol=1e-3)
                self.assertEqual(got.visible, expected.visible)

            # A retrained checkpoint must never keep running its old twin.
            _write_checkpoint(gate, 64, seed=5)
            LiveTongueModelPreview(gate, "dml", direction_checkpoint_path=direction)
            import onnxruntime

            session = onnxruntime.InferenceSession(
                str(onnx_path(gate)), providers=["CPUExecutionProvider"]
            )
            self.assertEqual(
                session.get_modelmeta().custom_metadata_map["sourceSha256"],
                checkpoint_digest(gate),
            )


class LowMemoryRuntimeSourceTests(unittest.TestCase):
    def test_runtime_and_launcher_provide_directml(self):
        requirements = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")
        self.assertIn("onnxruntime-directml", requirements)
        self.assertIn("onnx>=", requirements)
        launcher = (ROOT / "build-and-run.ps1").read_text(encoding="utf-8")
        self.assertIn("pip install --disable-pip-version-check onnxruntime-directml onnx", launcher)
        setup = (ROOT / "setup-runtime.ps1").read_text(encoding="utf-8")
        # Existing runtimes gain DirectML without a PyTorch reinstall.
        self.assertIn("DmlExecutionProvider", setup)

    def test_converter_ships_and_twins_stay_untracked(self):
        for builder in ("build-release.ps1", "build-github-source.ps1"):
            self.assertIn('"export_tongue_onnx.py"', (ROOT / builder).read_text(encoding="utf-8"))
        self.assertIn("models/*.onnx", (ROOT / ".gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
