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

    def test_existing_python_312_is_reused_instead_of_modified(self) -> None:
        # Issue #1: the python.org installer modifies an existing per-user 3.12
        # instead of creating the private copy, so setup must look for one first.
        source = (ROOT / "setup-runtime.ps1").read_text(encoding="utf-8")
        self.assertIn("function Find-ExistingPython312", source)
        self.assertIn("Software\\Python\\PythonCore\\3.12\\InstallPath", source)
        self.assertIn("WindowsApps", source)
        self.assertIn("sys.version_info[:2] == (3, 12)", source)
        detect = source.index("($existingPython = Find-ExistingPython312)")
        install = source.index("Start-Process -FilePath $bundledPythonInstaller")
        self.assertLess(detect, install, "existing Python must be checked before running the installer")
        after_install = source[install:]
        self.assertIn("Find-ExistingPython312", after_install, "installer 'success' without python.exe must fall back")
        self.assertIn("no longer starts", source, "a venv whose base Python was removed must be rebuilt")

    def test_setup_cards_size_to_content(self) -> None:
        # Issue #6: fixed-height setup rows clipped the Install buttons at high DPI.
        hub = (ROOT / "qpro-hub" / "Program.cs").read_text(encoding="utf-8")
        self.assertNotIn("firstRun.RowStyles.Add(new RowStyle(SizeType.Absolute", hub)
        start = hub.index("private static Control SetupStepCard(")
        card = hub[start:hub.index("private static Control WorkflowCard(", start)]
        self.assertIn("AutoSize = true", card)
        self.assertNotIn("SizeType.Absolute", card)
        self.assertIn("var setupActions = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true", hub)

    def test_release_builder_is_self_sufficient_and_keeps_the_v8_model(self) -> None:
        builder = (ROOT / "build-release.ps1").read_text(encoding="utf-8")
        for asset in ("models\\qpro-stereo-tongue-v8-gate.pt", "models\\qpro-stereo-tongue-v8-direction.pt",
                      "platform-tools\\adb.exe", "python-runtime\\python-3.12.10-amd64.exe",
                      "questpro-camera-injector"):
            self.assertIn(f'"{asset}"', builder)
        self.assertIn("release-assets", builder)
        self.assertIn("SHA256SUMS.txt", builder)
        self.assertIn("--self-test", builder)
        self.assertIn("release-manifest.json", builder)
        self.assertIn("release-assets/", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_hub_title_shows_the_manifest_version(self) -> None:
        import json
        manifest = json.loads((ROOT / "release-manifest.json").read_text(encoding="utf-8"))
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+")
        hub = (ROOT / "qpro-hub" / "Program.cs").read_text(encoding="utf-8")
        self.assertNotIn("Proof of Concept", hub)
        self.assertIn('"QproFaceTracking " + AppVersionLabel()', hub)
        self.assertIn("Qpro.SteamLinkBridge.dll", hub[:hub.index("class HubForm")])


if __name__ == "__main__":
    unittest.main()
