import unittest
from pathlib import Path


class WirelessBootstrapSourceTests(unittest.TestCase):
    def test_single_adb_device_pipeline_is_forced_to_an_array(self):
        source = Path("enable-quest-wireless.ps1").read_text(encoding="utf-8")
        self.assertIn("$devices = @(\n            @(& $adb devices)", source)
        self.assertIn("$UsbSerial = [string]$devices[0]", source)

    def test_wireless_bootstrap_keeps_an_explicit_recovery_path(self):
        enable = Path("enable-quest-wireless.ps1").read_text(encoding="utf-8")
        disable = Path("disable-quest-wireless.ps1").read_text(encoding="utf-8")
        self.assertIn("WIRELESS_ADB_READY", enable)
        self.assertIn("-s $AdbTarget usb", disable)

    def test_root_permission_is_verified_before_wireless_switch(self):
        source = Path("enable-quest-wireless.ps1").read_text(encoding="utf-8")
        self.assertLess(source.index("shell su -c id"), source.index("tcpip $Port"))
        self.assertIn("Magisk > Superuser", source)

    def test_explicit_preview_version_is_not_overwritten_by_loop(self):
        source = Path("preview-latest-tongue.ps1").read_text(encoding="utf-8")
        self.assertIn("$candidateVersion = [int]$Matches.version", source)
        self.assertNotIn("$version = [int]$Matches.version", source)

    def test_independent_eye_runner_can_reuse_saved_wireless_target(self):
        source = Path("native-eye-local-branch-test.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$Wireless", source)
        self.assertIn("config\\wireless-headset.json", source)
        self.assertIn("$env:ANDROID_SERIAL = $AdbTarget.Trim()", source)

    def test_wireless_relay_keeps_its_magisk_session_alive(self):
        source = Path("build-and-run.ps1").read_text(encoding="utf-8")
        self.assertIn("transport=live-adb-su", source)
        self.assertIn('$relayArguments = "shell su -c', source)
        self.assertNotIn(".ArgumentList.Add", source)


if __name__ == "__main__":
    unittest.main()
