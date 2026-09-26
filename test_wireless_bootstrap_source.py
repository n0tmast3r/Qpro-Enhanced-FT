import hashlib
import re
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

    def test_wireless_relay_keeps_its_magisk_session_alive(self):
        source = Path("build-and-run.ps1").read_text(encoding="utf-8")
        self.assertIn("transport=live-adb-su", source)
        self.assertIn('$relayArguments = "shell su -c', source)
        self.assertNotIn(".ArgumentList.Add", source)


class CablefreeReconnectSourceTests(unittest.TestCase):
    def test_connect_script_scans_and_verifies_a_rooted_quest(self):
        source = Path("connect-quest-wireless.ps1").read_text(encoding="utf-8")
        # It must confirm identity and root before adopting a candidate, and be
        # able to rediscover a moved headset by scanning the local /24.
        self.assertIn("ro.product.device", source)
        self.assertIn("uid=0\\(root\\)", source)
        self.assertIn("BeginConnect", source)
        self.assertIn("config\\wireless-headset.json", source)

    def test_connect_script_emits_a_capturable_ready_line(self):
        source = Path("connect-quest-wireless.ps1").read_text(encoding="utf-8")
        # The machine-readable result must go to stdout (Write-Output), not the
        # host stream, so build-and-run and the hub can parse the target.
        self.assertIn('Write-Output "WIRELESS_ADB_READY', source)
        self.assertNotIn('Write-Host "WIRELESS_ADB_READY', source)

    def test_connect_script_fast_fails_a_stale_saved_address(self):
        source = Path("connect-quest-wireless.ps1").read_text(encoding="utf-8")
        # A dead saved address must be rejected by a short port probe rather than
        # waiting out adb connect's own multi-second timeout before scanning.
        self.assertIn("function Test-Port", source)
        self.assertIn("if (-not (Test-Port $Target)) { return $false }", source)

    def test_build_and_run_exposes_a_wireless_switch(self):
        source = Path("build-and-run.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$Wireless", source)
        self.assertIn("connect-quest-wireless.ps1", source)
        self.assertIn("WIRELESS_ADB_READY", source)

    def test_pairing_scripts_use_bundled_adb(self):
        # enable/disable must find the bundled adb, not only PATH/SDK, so a
        # friend without adb installed does not hit "adb not found".
        for name in ("enable-quest-wireless.ps1", "disable-quest-wireless.ps1"):
            src = Path(name).read_text(encoding="utf-8")
            self.assertIn("platform-tools\\adb.exe", src, name)

    def test_bat_launchers_bypass_policy_and_stay_open(self):
        # Double-clicking a .ps1 runs and closes instantly; the .bat wrappers
        # bypass execution policy and pause so errors are readable.
        for name in ("Enable-Wireless.bat", "Disable-Wireless.bat"):
            bat = Path(name).read_text(encoding="utf-8")
            self.assertIn("-ExecutionPolicy Bypass", bat, name)
            self.assertIn("pause", bat, name)

    def test_enable_uses_wlan0_and_delegates_to_scan(self):
        src = Path("enable-quest-wireless.ps1").read_text(encoding="utf-8")
        # Derive the Wi-Fi IP from wlan0 (not the default-route source, which can
        # be a different interface) and hand off to the scan-capable connector so
        # a wrong/moved address self-corrects instead of failing.
        self.assertIn("ip -4 addr show wlan0", src)
        self.assertIn("connect-quest-wireless.ps1", src)


class HubWirelessSourceTests(unittest.TestCase):
    def test_hub_resolves_wireless_target(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        self.assertIn("EnsureWirelessAdoptedAsync", source)
        self.assertIn("wireless-headset.json", source)
        # Wi-Fi mode keeps/reacquires the paired headset.
        self.assertIn("_mode == ConnectionMode.WiFi", source)

    def test_hub_has_a_connect_wirelessly_action(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        self.assertIn("ConnectWirelesslyAsync", source)
        self.assertIn("Enable / Connect Wi-Fi", source)
        self.assertIn("WIRELESS_ADB_READY", source)

    def test_hub_has_magisk_root_preflight(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        self.assertIn("EnsureHeadsetRootAsync", source)
        self.assertIn("HeadsetRootedAsync", source)
        # The dialog must name the exact Magisk step.
        self.assertIn("Superuser", source)
        self.assertIn("ADB Shell", source)

    def test_hub_passes_wireless_target_to_tracking_scripts(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        # Tongue live tracking and capture must forward -AdbTarget when on Wi-Fi.
        self.assertIn('"-AdbTarget", _wirelessTarget', source)
        self.assertIn("tongueArgs.AddRange(targetArgs)", source)
        self.assertIn(".. captureTargetArgs", source)

    def test_hub_usb_probe_excludes_wireless_serials(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        # A wireless target is also "<serial> device" in adb devices; USB must
        # exclude ip:port and mDNS serials so Wi-Fi is not reported as a cable.
        probe = source.split("private async Task<string?> UsbQuestSerialAsync()", 1)[1]
        probe = probe.split("private async Task<string?> ActiveTargetAsync()", 1)[0]
        self.assertIn("!serial.Contains(':')", probe)

    def test_usb_mode_targets_the_usb_serial(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        # The same headset is often attached over USB and Wi-Fi at once, where an
        # untargeted adb call fails with "more than one device". USB mode must aim
        # at the USB serial: hub probes via -s, child scripts via ANDROID_SERIAL,
        # pairing via -UsbSerial.
        self.assertIn('info.Environment["ANDROID_SERIAL"] = serial', source)
        self.assertNotIn('? ["shell",', source)
        self.assertIn('["-s", target, "shell", shellArg]', source)
        self.assertIn('"-UsbSerial", usbSerial!', source)


class EyeModuleSourceTests(unittest.TestCase):
    SERGIO_FILES = ("module.prop", "customize.sh", "patch_bolt.sh", "service.sh", "uninstall.sh", "README.md")

    def test_sergio_module_is_bundled_unmodified(self):
        # SergioMarquina's module ships byte-for-byte (with his permission): the hub checks every
        # file against this SHA-256 manifest before installing it.
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        manifest = source.split("SergioModuleFiles = new Dictionary<string, string>", 1)[1].split("};", 1)[0]
        for name in self.SERGIO_FILES:
            digest = hashlib.sha256(Path("sergio-eye-module", name).read_bytes()).hexdigest()
            self.assertIn(f'["{name}"] = "{digest}"', manifest, name)
        self.assertEqual(sorted(entry.name for entry in Path("sergio-eye-module").iterdir()), sorted(self.SERGIO_FILES))

    def test_sergio_module_is_script_only_and_keeps_lf(self):
        for name in self.SERGIO_FILES:
            data = Path("sergio-eye-module", name).read_bytes()
            self.assertNotIn(b"\r\n", data, name)
            self.assertNotIn(b"\x00", data, name)
        self.assertIn("sergio-eye-module/** -text", Path(".gitattributes").read_text(encoding="utf-8"))

    def test_no_meta_model_is_bundled(self):
        self.assertFalse(Path("eye-model-module").exists())
        self.assertFalse(Path("install-eye-module.ps1").exists())
        for builder in ("build-release.ps1", "build-github-source.ps1"):
            src = Path(builder).read_text(encoding="utf-8")
            self.assertIn('("sergio-eye-module\\" + $file)', src, builder)
            self.assertNotIn(".ptl\"", src.split("$forbidden", 1)[0], builder)
        notices = Path("THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("no eye-tracking model", notices)
        self.assertIn("SergioMarquina", notices)
        self.assertIn("with his permission", notices)

    def test_user_module_list_is_machine_local(self):
        ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("config/eye-modules.json", ignored)

    def test_hub_offers_sergio_first(self):
        hub = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        self.assertIn('DialogButton("Install Sergio\'s module (recommended)…", true, () => Choose(EyeModuleAction.InstallSergio))', hub)
        self.assertIn("if (action == EyeModuleAction.InstallSergio) await InstallSergioModuleAsync();", hub)
        install = hub.split("private async Task InstallSergioModuleAsync()", 1)[1].split("private static byte[] WithoutCarriageReturns(", 1)[0]
        self.assertIn('Path.Combine(_root, "sergio-eye-module")', install)
        self.assertIn("EyeModelPatcher.Sha256(bytes) != expected", install)
        self.assertIn("InstallEyeModuleZipAsync(", install)
        self.assertIn("Create my eye patch", install)                  # points to the fallback
        self.assertIn('"sergio-eye-module\\\\module.prop"', hub)         # release self-test requires it


def _module_id_pattern():
    source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
    match = re.search(r'MagiskModuleIdPattern = new\(@"([^"]+)"', source)
    assert match, "MagiskModuleIdPattern not found"
    # .NET's \z (absolute end) is Python's \Z.
    return re.compile(match.group(1).replace(r"\z", r"\Z"))


class HubEyeModuleSourceTests(unittest.TestCase):
    def test_hub_recognizes_builtin_and_user_eye_modules(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        self.assertIn('BuiltInEyeModuleIds = [EyeModelPatcher.ModuleId, "questpro_independent_gaze", "qpro_individual_eye_enabler"]', source)
        self.assertIn('Path.Combine(_root, "config", "eye-modules.json")', source)
        scan = source.split("private async Task<EyeModuleScan> ScanEyeModulesAsync(string? target)", 1)[1]
        scan = scan.split("private async Task<bool> HeadsetRootedAsync(", 1)[0]
        # Built-ins + the user's ids; installed, enabled, not flagged for removal; a module
        # staged until reboot is reported separately.
        self.assertIn("RecognizedEyeModuleIds()", scan)
        self.assertIn("test -e $d/disable && continue", scan)
        self.assertIn("test -e $d/remove && continue", scan)
        self.assertIn("echo PENDING:$m", scan)
        # An update over a live install stays active (the old version is mounted until reboot).
        self.assertIn('grep -qvxE \\"module[.]prop|update\\"', scan)
        self.assertIn('["-s", target, "shell", shellArg]', scan)
        self.assertIn("EyeModelReady() => _eyeModuleActive", source)

    def test_module_ids_are_validated_before_any_shell_use(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        pattern = _module_id_pattern()
        for good in ("questpro_independent_gaze", "qpro_individual_eye_enabler", "mod-1.2_x", "ab"):
            self.assertTrue(pattern.match(good), good)
        for bad in ("a", "1abc", "evil;reboot", "x y", "good\nreboot", "good\n", "a'b", "a$(id)",
                    "a/b", "a" * 65, ""):
            self.assertFalse(pattern.match(bad), repr(bad))
        # Every path that turns an id into a root command filters with IsValidModuleId.
        load = source.split("private List<string> LoadUserEyeModuleIds()", 1)[1].split("private void SaveUserEyeModuleIds", 1)[0]
        self.assertIn(".Where(IsValidModuleId)", load)
        save = source.split("private void SaveUserEyeModuleIds(", 1)[1].split("private List<string> RecognizedEyeModuleIds", 1)[0]
        self.assertIn("ids.Where(IsValidModuleId)", save)
        self.assertIn("if (!IsValidModuleId(id))", source.split("private static MagiskModuleInfo? ReadMagiskModule(", 1)[1])
        self.assertIn("moduleIds.Where(IsValidModuleId)", source.split("private async Task<List<string>> DisableEyeModulesAsync(", 1)[1])
        self.assertIn("!IsValidModuleId(parts[1])", source.split("private static async Task<List<HeadsetModule>?> ListHeadsetModulesAsync(", 1)[1])

    def test_hub_installs_only_a_user_supplied_module_zip(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        install = source.split("private async Task InstallUserEyeModuleAsync()", 1)[1]
        install = install.split("private sealed record MagiskModuleInfo", 1)[0]
        self.assertIn('Filter = "Magisk module (*.zip)|*.zip"', install)
        self.assertIn("ReadMagiskModule(zipPath, out var problem)", install)
        self.assertIn("MessageBoxButtons.OKCancel", install)          # user confirms the module.prop details
        self.assertIn("VirtualDesktop.Streamer", install)             # warns (does not block) while VD runs
        self.assertIn("MessageBoxButtons.YesNoCancel", install)       # offers to disable other eye modules
        self.assertIn("EyeModuleFinishSteps()", install)              # reboot/root/recalibrate checklist
        self.assertIn('"Eye module or helper?"', install)             # helpers (e.g. OverlayFS) are not eye modules
        self.assertIn("installed && isEyeModule &&", install)          # only an eye module is remembered
        magisk = source.split("private async Task<bool> InstallMagiskModuleAsync(", 1)[1]
        magisk = magisk.split("private async Task<List<string>> DisableEyeModulesAsync(", 1)[0]
        self.assertIn("magisk --install-module", magisk)
        self.assertIn('"push", zipPath, remote', magisk)
        self.assertIn("rm -f", magisk)
        self.assertIn('"/data/local/tmp/qpro-eye-module-" + Guid.NewGuid()', magisk)  # unique per install
        read = source.split("private static MagiskModuleInfo? ReadMagiskModule(", 1)[1].split("private async Task<bool> InstallMagiskModuleAsync(", 1)[0]
        # The id is read exactly like Magisk's grep_prop (first line starting with id=, untrimmed).
        self.assertIn('line.StartsWith("id=", StringComparison.Ordinal)', read)
        self.assertIn("detectEncodingFromByteOrderMarks: false", read)
        setup = source.split("private async Task ShowEyeModuleSetupAsync()", 1)[1].split("private string EyeModuleSetupText()", 1)[0]
        self.assertIn("if (_eyeModuleBusy) return;", setup)             # one eye-module flow at a time
        self.assertIn("SetSetupButtonsEnabled(false);", setup)

    def test_hub_never_reboots_or_flips_properties_for_convergence(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        # Clearing the eye-tracking filter properties was measured NOT to free the eyes
        # on the stock model, and restarting trackingservice stopped controller tracking
        # on an older firmware build, so the hub must never do either. Rebooting stays the
        # user's call (the hub shows a checklist instead).
        for forbidden in ("resetprop", "social_filtering", "stop trackingservice",
                          "start trackingservice", "trackingfidelityservice",
                          "install-eye-module.ps1", '"reboot"', "svc power reboot"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_docs_describe_bring_your_own_module(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("Manage eye module", readme)
        self.assertIn("Install Sergio's module", readme)
        self.assertIn("Create my eye patch", readme)
        self.assertIn("Install module (.zip)", readme)
        self.assertIn("Choose installed", readme)
        self.assertNotIn("Enable convergence", readme)
        self.assertNotIn("social_filtering", readme)


class OwnEyePatchSourceTests(unittest.TestCase):
    """Our own eye patch: built from the user's own model, applied on the headset."""

    def patcher(self):
        return Path("qpro-hub/EyeModelPatcher.cs").read_text(encoding="utf-8")

    def test_gate_is_found_from_the_graph_not_fixed_offsets(self):
        source = self.patcher()
        analyze = source.split("internal static Plan Analyze(byte[] model)", 1)[1].split("internal static byte[] Apply(", 1)[0]
        # Walk: public [1,4] reshape <- Add(local, Mul(Sigmoid(FC(x, W, b)), ...)).
        for op in ('"OP_Reshape"', '"OP_Add_f"', '"OP_Mul_f"', '"Sigmoid"', '"OP_FC_f"'):
            self.assertIn(op, analyze, op)
        self.assertIn('OutputShape(node).SequenceEqual([1, 4])', analyze)
        # No hard-coded offsets from other modules.
        for offset in ("354347", "358443", "375752", "383828"):
            self.assertNotIn(offset, source, offset)
        # Fails closed on anything unexpected, including an already-patched model.
        self.assertIn("already looks patched", analyze)
        self.assertIn("Crc32(payload) == storedCrc", analyze)
        self.assertIn('\\"id\\": {reshapeId}, \\"name\\": ', analyze)  # anchored on id + name

    def test_edits_keep_the_archive_consistent(self):
        source = self.patcher()
        apply = source.split("internal static byte[] Apply(", 1)[1].split("internal static byte[] BuildModule(", 1)[0]
        self.assertIn("foreach (var position in plan.CrcPositions)", apply)  # every CRC copy is rewritten
        self.assertIn("SameLengthInput", source)                            # rewire never changes the size
        self.assertIn('Need(stock.Length == patched.Length', source)

    def test_generated_module_ships_no_model_and_verifies_everything(self):
        source = self.patcher()
        build = source.split("internal static byte[] BuildModule(", 1)[1].split("private static List<(int Offset, byte[] Bytes)> DiffRuns(", 1)[0]
        files = re.findall(r'\["([^"]+)"\] =', build)
        self.assertEqual(sorted(files), sorted(["module.prop", "qpro-eye.conf", "patch.sh", "customize.sh",
                                                "post-fs-data.sh", "service.sh", "uninstall.sh"]))
        # The patched model is produced on the headset from its own stock copy, checked by SHA-256.
        self.assertIn("STOCK_SHA", build)
        self.assertIn("PATCHED_SHA", build)
        self.assertIn('abort \\"! The patched model did not verify; nothing was changed.\\"', build)
        # Readable by trackingservice under enforcing SELinux.
        self.assertIn("u:object_r:vendor_configs_file:s0", build.split('["customize.sh"]', 1)[1].split('["post-fs-data.sh"]', 1)[0])
        self.assertIn("chcon u:object_r:vendor_configs_file:s0", build)
        # Only trackingservice is restarted (init's restart), only if it already loaded the stock model.
        self.assertIn("setprop ctl.restart trackingservice", build)
        self.assertIn("pidof trackingservice >/dev/null && touch", build)
        # A later overlay (Magisk OverlayFS on /odm/etc) can hide the early mount: service.sh
        # re-checks the live path after boot and mounts again on top.
        service = build.split('["service.sh"]', 1)[1].split('["uninstall.sh"]', 1)[0]
        self.assertIn('mount -o bind', service)
        self.assertIn('sys.boot_completed', service.split('mount -o bind', 1)[0])
        self.assertNotIn("trackingfidelityservice", source)
        self.assertNotIn("reboot", build)
        self.assertIn(f"internal const string ModuleId = \"qpro_eye_patch\";", source)

    def test_module_clears_only_social_filtering(self):
        # Measured: the model patch alone and social_filtering=0 alone each leave the eyes
        # locked; both together free them. The other *_filtering properties are not needed.
        source = self.patcher()
        self.assertIn("internal const bool ClearSocialFilteringByDefault = true;", source)
        self.assertIn("resetprop debug.oculus.eye_tracking.social_filtering 0", source)
        for other in ("foveation_filtering", "interaction_filtering"):
            self.assertNotIn(other, source, other)

    def test_tested_stock_models_are_listed_by_hash(self):
        source = self.patcher()
        self.assertIn('["8868cbc3c19a002be2a2d32a7db6de72911dc24f3b95a527a5d747378d6628bb"] = "51503870021300340"', source)

    def test_hub_builds_and_installs_the_patch(self):
        hub = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        build = hub.split("private async Task BuildOwnEyePatchAsync()", 1)[1].split("private static bool MountCoversModel(", 1)[0]
        self.assertIn("MountCoversModel(line, EyeModelPatcher.ProductionModelPath)", build)   # never read a covered model
        self.assertIn("EyeModelPatcher.Sha256(stock) != hashes[source]", build)                # complete, unmodified read
        self.assertIn("EyeModelPatcher.Analyze(stock)", build)
        self.assertIn("InstallEyeModuleZipAsync(", build)                                      # same confirm/conflict/install path
        self.assertIn("File.Delete(zipPath)", build)
        read = hub.split("private static async Task<byte[]?> ReadHeadsetFileAsync(", 1)[1]
        self.assertIn('"exec-out", "su -c \'cat " + path + "\'"', read)
        self.assertIn("EyeModuleAction.BuildPatch", hub)
        self.assertIn('DialogButton("Create my eye patch…", false', hub)   # the fallback, after Sergio's

    def test_zips_carrying_a_model_are_refused(self):
        hub = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        read = hub.split("private static MagiskModuleInfo? ReadMagiskModule(", 1)[1]
        self.assertIn('entry.Name.EndsWith(".ptl", StringComparison.OrdinalIgnoreCase)', read)
        self.assertIn('entry.Name.EndsWith(".img", StringComparison.OrdinalIgnoreCase)', read)

    def test_source_package_includes_the_patcher(self):
        self.assertIn('"qpro-hub\\EyeModelPatcher.cs"', Path("build-github-source.ps1").read_text(encoding="utf-8"))


class HubConnectionModeSourceTests(unittest.TestCase):
    def test_mode_toggle_exists(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        self.assertIn("enum ConnectionMode { Usb, WiFi }", source)
        self.assertIn("SelectMode(ConnectionMode.Usb)", source)
        self.assertIn("SelectMode(ConnectionMode.WiFi)", source)

    def test_gaze_is_passive_no_pc_runtime(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        start = source.split("private async Task StartTrackingAsync()", 1)[1]
        start = start.split("private async Task StopTrackingAsync()", 1)[0]
        # The developer's PC gaze runtime is no longer launched; convergence is produced
        # on the headset and forwarded through Virtual Desktop on both transports.
        self.assertNotIn("native-eye-local-branch-test.ps1", start)
        self.assertIn("forwarded through Virtual Desktop", start)

    def test_setup_step3_opens_eye_module_setup(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        self.assertIn("_setupGazeButton.Click += async (_, _) => await ShowEyeModuleSetupAsync();", source)
        self.assertIn('SetupButton("Manage eye module")', source)

    def test_wifi_button_only_in_wifi_mode(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        self.assertIn("_wifiConnectButton.Visible = _mode == ConnectionMode.WiFi;", source)
        self.assertIn('SecondaryButton("Enable / Connect Wi-Fi")', source)

    def test_gaze_toggle_replaced_by_skip_eye_gaze(self):
        # Gaze runs on the headset, so the hub only offers to skip the eye-module check.
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        self.assertNotIn('FeatureToggle("Independent eye gaze', source)
        self.assertIn('_skipGaze = FeatureToggle("Skip eye gaze (tongue only)", false);', source)
        start = source.split("private async Task StartTrackingAsync()", 1)[1].split("private async Task StopTrackingAsync()", 1)[0]
        self.assertIn("!_skipGaze.Checked && !EyeModelReady()", start)
        self.assertIn("_skipGaze.Checked && !_tongue.Checked", start)

    def test_eye_module_extras_are_under_advanced(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        dialog = source.split("private sealed class EyeModuleSetupDialog : Form", 1)[1].split("private sealed record EyeModuleEntry", 1)[0]
        advanced = dialog.split("var advanced = new TableLayoutPanel", 1)[1].split("var footer", 1)[0]
        self.assertIn("Visible = false", advanced)
        self.assertIn('"Install module (.zip)…"', advanced)
        self.assertIn('"Choose installed…"', advanced)
        primary = dialog.split("var primary = new FlowLayoutPanel", 1)[1].split("var advanced = new TableLayoutPanel", 1)[0]
        self.assertIn('"Install Sergio\'s module (recommended)…"', primary)
        self.assertIn('"Create my eye patch…"', primary)
        self.assertIn('"Advanced ▸"', dialog)

    def test_mode_drives_transport_only(self):
        source = Path("qpro-hub/Program.cs").read_text(encoding="utf-8")
        # The toggle still selects the ADB transport for tongue/capture/module ops.
        self.assertIn("string[] ModeTargetArgs()", source)
        self.assertIn("_mode == ConnectionMode.WiFi", source)


class TongueRuntimeSourceTests(unittest.TestCase):
    def test_continuous_calibration_was_removed(self):
        for path in ("tongue_model_preview.py", "receiver.py", "build-and-run.ps1", "qpro-hub/Program.cs"):
            source = Path(path).read_text(encoding="utf-8")
            for gone in ("continuous_calibration", "ContinuousCalibration", "continuous-calibration", "_tongueContinuous", "reset_calibration"):
                self.assertNotIn(gone, source, f"{gone} in {path}")


if __name__ == "__main__":
    unittest.main()
