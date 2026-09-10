import hashlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EXPECTED_PYTHON_SHA256 = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"


class ReleaseBootstrapTests(unittest.TestCase):
    def test_bundled_python_installer_matches_pinned_hash(self) -> None:
        installer = ROOT / "python-runtime" / "python-3.12.10-amd64.exe"
        if not installer.is_file():
            self.skipTest("The official Python installer is a release asset, not a Git-tracked source file")
        self.assertEqual(hashlib.sha256(installer.read_bytes()).hexdigest(), EXPECTED_PYTHON_SHA256)

    def test_runtime_setup_is_private_and_does_not_require_path_python(self) -> None:
        source = (ROOT / "setup-runtime.ps1").read_text(encoding="utf-8")
        for setting in (
            'InstallAllUsers=0',
            'Include_launcher=0',
            'AssociateFiles=0',
            'PrependPath=0',
            'AppendPath=0',
        ):
            self.assertIn(setting, source)
        self.assertIn('python-runtime\\python-3.12.10-amd64.exe', source)
        self.assertIn(EXPECTED_PYTHON_SHA256, source)
        self.assertIn('runtime-ready.json', source)
        self.assertIn('Test-PythonCommand', source)

    def test_release_builder_and_self_test_require_the_bootstrap(self) -> None:
        builder = (ROOT / "build-release.ps1").read_text(encoding="utf-8")
        hub = (ROOT / "qpro-hub" / "Program.cs").read_text(encoding="utf-8")
        self.assertIn('python-3.12.10-amd64.exe', builder)
        self.assertIn('python-runtime\\\\python-3.12.10-amd64.exe', hub)
        self.assertIn('runtime-ready.json', hub)

    def test_hub_has_setup_progress_and_actionable_gaze_preflight(self) -> None:
        hub = (ROOT / "qpro-hub" / "Program.cs").read_text(encoding="utf-8")
        for expected in (
            "First-time setup progress",
            "IsIndeterminate",
            "This can take several minutes",
            "Quest Pro not found over ADB",
            "Quest Pro root access is unavailable",
            "grant Superuser access to Shell / ADB Shell",
        ):
            self.assertIn(expected, hub)


if __name__ == "__main__":
    unittest.main()
