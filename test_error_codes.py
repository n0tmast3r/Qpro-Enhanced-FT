import re
import unittest
from pathlib import Path

SOURCE = Path("qpro-hub/ErrorCodes.cs").read_text(encoding="utf-8")


def _errors():
    return dict(re.findall(r'public static readonly HubError (\w+) = new\("(QPRO-\d{3})"', SOURCE))


def _patterns():
    # (P(@"regex"), Name) entries, in order; .NET and Python agree on these regexes.
    names = _errors()
    return [(re.compile(pattern.replace('""', '"'), re.IGNORECASE), names[name])
            for pattern, name in re.findall(r'\(P\(@"((?:[^"]|"")*)"\), (\w+)\)', SOURCE)]


def diagnose(text):
    for pattern, code in _patterns():
        if pattern.search(text):
            return code
    return "QPRO-900"


class ErrorCodeTests(unittest.TestCase):
    def test_codes_are_unique_and_well_formed(self):
        codes = list(_errors().values())
        self.assertGreater(len(codes), 30)
        self.assertEqual(len(codes), len(set(codes)))

    def test_every_pattern_names_a_known_error(self):
        self.assertEqual(len(_patterns()), SOURCE.count("(P(@"))

    def test_script_failures_map_to_specific_codes(self):
        cases = {
            "The bundled Python installation failed with code 0. See C:\\x\\python-install.log": "QPRO-102",
            "Python 3.12 is already installed at C:\\Py\\python.exe, but it cannot create a private environment (pip/venv is missing).": "QPRO-101",
            "Installing both CUDA and CPU PyTorch builds failed. Check the internet connection and run setup again.": "QPRO-103",
            "The installed runtime failed its final import check. Run setup again": "QPRO-104",
            "Close VRCFaceTracking before installing the combined independent-gaze + Steam Link face bridge.": "QPRO-201",
            "Start VRCFaceTracking first. No VRCFaceTracking process was found.": "QPRO-203",
            "Start SteamVR first. SteamVR's vrserver process was not found.": "QPRO-204",
            "adb: error: more than one device/emulator": "QPRO-303",
            "error: device unauthorized.": "QPRO-304",
            "Magisk root is not granted to Android Shell on USB.": "QPRO-305",
            "No wireless Quest Pro was found on 192.168.1.0/24.": "QPRO-306",
            "No authorized Quest was found over ADB. Connect it by USB or pass -AdbTarget.": "QPRO-302",
            "Pushing the relay failed with exit code 1": "QPRO-401",
            "Injection failed with exit code 3. Send questpro-live-inject.txt.": "QPRO-402",
            "The root relay did not begin listening. Send questpro-live-relay.txt.": "QPRO-403",
            "ADB port forwarding failed with exit code 1": "QPRO-404",
            "The PC tracking runtime exited with code 1.": "QPRO-405",
            "No completed quick-refinement capture was found.": "QPRO-601",
            "RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB": "QPRO-602",
            "Refining tongue direction failed.": "QPRO-603",
        }
        for text, code in cases.items():
            self.assertEqual(diagnose(text), code, text)

    def test_messages_never_just_say_see_activity(self):
        hub = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        self.assertNotIn("See Activity for the exact error and suggested fix", hub)
        self.assertNotIn("did not complete. See Activity for details.", hub)


if __name__ == "__main__":
    unittest.main()
