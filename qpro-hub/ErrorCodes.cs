using System.Text.RegularExpressions;

namespace QproFaceTracking.Hub;

// Short, searchable error codes with a plain-language fix, so a failed step tells the
// user what to do instead of only "see Activity". The full output still goes to
// Activity; the code is what users quote when they ask for help.
//
//   1xx PC runtime       2xx VRCFaceTracking / bridges   3xx headset connection
//   4xx live tracking    5xx eye convergence module      6xx tongue training
internal sealed record HubError(string Code, string Title, string Fix)
{
    public string Heading => $"{Code} · {Title}";

    public string Message(string? detail = null) =>
        Title + "\n\n" +
        (string.IsNullOrWhiteSpace(detail) ? "" : detail.Trim() + "\n\n") +
        "What to do:\n" + Fix + "\n\n" +
        $"Error code {Code}. The Activity panel has the full details if you ask for help.";
}

internal static class ErrorCodes
{
    // ---- PC runtime -------------------------------------------------------------------
    public static readonly HubError PythonExistingBroken = new("QPRO-101", "Your existing Python 3.12 can't be used",
        "Open Windows Settings > Apps, choose Python 3.12 > Modify and make sure pip is included (or uninstall it), then press Install runtime again. Your Python installation was not changed.");
    public static readonly HubError PythonInstallerFailed = new("QPRO-102", "The bundled Python installer failed",
        "Re-extract the complete release zip (don't run it from inside the zip), then press Install runtime again. If Python 3.12 is installed on this PC, try Settings > Apps > Python 3.12 > Repair first.");
    public static readonly HubError RuntimeDownloadFailed = new("QPRO-103", "Downloading the tracking libraries failed",
        "Check the internet connection (a VPN, proxy or school/work network can block the download), make sure the disk has at least 6 GB free, then press Install runtime again. It continues where it stopped.");
    public static readonly HubError RuntimeBroken = new("QPRO-104", "The PC runtime is installed but doesn't start",
        "Press Install runtime again to repair it. If it repeats, delete %LOCALAPPDATA%\\QproFaceTracking\\runtime and install again.");
    public static readonly HubError RuntimeMissing = new("QPRO-105", "The PC runtime isn't set up yet",
        "Open First-time setup and press Install runtime (step 1).");
    public static readonly HubError ReleaseIncomplete = new("QPRO-106", "Files from the release are missing",
        "Extract the whole release zip again into a normal folder (for example Documents), and run QproFaceTracking.exe from there. Some antivirus tools quarantine adb.exe; restore it if so.");

    // ---- VRCFaceTracking / bridges ------------------------------------------------------
    public static readonly HubError VrcftRunningDuringInstall = new("QPRO-201", "VRCFaceTracking is open",
        "Close VRCFaceTracking completely (also from the system tray), press the install button again, then start VRCFaceTracking.");
    public static readonly HubError BridgeInstallFailed = new("QPRO-202", "The VRCFaceTracking bridge could not be installed",
        "Re-extract the complete release, close VRCFaceTracking, and try again. If an older backup folder is mentioned in Activity, move it out of the release's research folder first.");
    public static readonly HubError VrcftNotRunning = new("QPRO-203", "VRCFaceTracking isn't running",
        "Start VRCFaceTracking, check that normal face tracking works, then try again.");
    public static readonly HubError SteamVrNotRunning = new("QPRO-204", "SteamVR isn't running",
        "Start SteamVR (and Virtual Desktop or Steam Link), then try again.");
    public static readonly HubError LabelBridgeFailed = new("QPRO-205", "The face-tracking reference stream didn't start",
        "Make sure Virtual Desktop (or Steam Link with the Steam Link bridge) is streaming face tracking into VRCFaceTracking, then try again. Antivirus software can also block Qpro.VirtualDesktopLabelBridge.exe.");

    // ---- Headset connection -------------------------------------------------------------
    public static readonly HubError AdbMissing = new("QPRO-301", "The bundled Android tools (adb) are missing",
        "Re-extract the complete release. If your antivirus removed platform-tools\\adb.exe, restore it and allow it.");
    public static readonly HubError HeadsetNotFound = new("QPRO-302", "The Quest Pro wasn't found",
        "Plug the headset in with a USB data cable, wake it up, and accept \"Allow USB debugging\" inside the headset. On Wi-Fi, press Enable / Connect Wi-Fi.");
    public static readonly HubError MultipleDevices = new("QPRO-303", "More than one Android device is connected",
        "Unplug other phones/headsets (or close Android emulators) so only the Quest Pro is connected, then try again.");
    public static readonly HubError Unauthorized = new("QPRO-304", "USB debugging isn't allowed yet",
        "Put the headset on and accept \"Allow USB debugging\" (tick \"Always allow from this computer\"). If no prompt appears, unplug and replug the cable.");
    public static readonly HubError RootMissing = new("QPRO-305", "Root access isn't granted",
        "On the headset: open the Magisk app > Superuser and enable Shell (ADB Shell). If the headset just restarted, re-apply root with your root method first. Then try again.");
    public static readonly HubError WifiUnreachable = new("QPRO-306", "The headset couldn't be reached over Wi-Fi",
        "Make sure the headset is awake and on the same network as this PC. If it restarted or changed network, connect USB once, press Enable / Connect Wi-Fi, then unplug.");
    public static readonly HubError WifiEnableFailed = new("QPRO-307", "Wi-Fi mode couldn't be enabled",
        "Connect only the Quest Pro by USB, make sure USB debugging is accepted and root is granted to Shell in Magisk, then press Enable / Connect Wi-Fi again.");

    // ---- Live tracking ------------------------------------------------------------------
    public static readonly HubError HeadsetFilesFailed = new("QPRO-401", "Copying the tracking files to the headset failed",
        "Check that the headset is awake and has free storage, then press Apply again. If it repeats, restart the headset.");
    public static readonly HubError InjectionFailed = new("QPRO-402", "The headset camera hook didn't start",
        "Restart the headset, re-apply root, then press Apply again. This can also mean the firmware isn't supported (v2.7 or lower only).");
    public static readonly HubError RelayFailed = new("QPRO-403", "The headset camera relay didn't start",
        "Press Stop, then Apply again. If it repeats, restart the headset and re-apply root.");
    public static readonly HubError ForwardFailed = new("QPRO-404", "The connection to the headset dropped while starting",
        "Check the USB cable (or Wi-Fi), then press Apply again.");
    public static readonly HubError TrackingExited = new("QPRO-405", "Tongue tracking stopped unexpectedly",
        "Press Stop, then Apply again. If it keeps happening, lower the Camera FPS cap and check that the GPU driver is up to date.");
    public static readonly HubError ModelMissing = new("QPRO-406", "The selected tongue model is missing",
        "Press Refresh and pick another model. The bundled v8 demo model lives in the release's models folder; re-extract the release if it's gone.");

    // ---- Eye convergence module ---------------------------------------------------------
    public static readonly HubError SergioFilesChanged = new("QPRO-501", "Sergio's module files are missing or changed",
        "Re-extract the complete release so the sergio-eye-module folder is intact.");
    public static readonly HubError SergioNotApplied = new("QPRO-502", "Sergio's module is installed but didn't patch the eye model",
        "It supports one specific stock eye model, and this headset's firmware has a different one. Use Create my eye patch instead: the hub removes Sergio's module and builds a patch from your own headset's model.");
    public static readonly HubError OwnPatchInactive = new("QPRO-503", "Your eye patch is installed but not active",
        "Open Manage eye module and choose Create my eye patch again (this rebuilds it for the current firmware), then restart the headset.");
    public static readonly HubError FilterStillOn = new("QPRO-504", "The eye module loaded, but the eye-coupling filter is still on",
        "Restart the headset (the module turns the filter off at boot). If root is applied after boot on your headset, re-apply root and press Check after restart.");
    public static readonly HubError MagiskInstallFailed = new("QPRO-505", "Magisk couldn't install the module",
        "Check that Magisk is working (open the Magisk app on the headset) and root is granted to Shell. Some modules need a helper module first (for example Magisk OverlayFS).");
    public static readonly HubError EyeModelUnreadable = new("QPRO-506", "The headset's eye model couldn't be read",
        "Wake the headset, make sure root is granted to Shell in Magisk, and try again.");
    public static readonly HubError EyeModelCovered = new("QPRO-507", "Another eye module is already patching the eye model",
        "Use Revert to stock in Manage eye module (or remove the other module in the Magisk app), restart the headset, then create your patch.");
    public static readonly HubError EyeModelUnknown = new("QPRO-508", "This firmware's eye model has an unknown layout",
        "Nothing was changed. This firmware isn't supported by the patch builder yet; please report it with your firmware number.");
    public static readonly HubError RestartTimeout = new("QPRO-509", "The headset didn't reconnect after restarting",
        "Put the headset on so it wakes up and finishes starting. On USB, check the cable; on Wi-Fi, connect USB once and press Enable / Connect Wi-Fi. Then press Check in Manage eye module.");
    public static readonly HubError StillPendingAfterRestart = new("QPRO-510", "Magisk didn't apply the module during the restart",
        "Re-apply root on the headset, then restart it once more from Manage eye module. Magisk only finishes installing modules at boot while root is active.");
    public static readonly HubError RevertIncomplete = new("QPRO-511", "The stock eye model isn't fully restored yet",
        "Restart the headset once more. If an eye module is still listed, remove it in the Magisk app and restart again.");

    // ---- Tongue training ----------------------------------------------------------------
    public static readonly HubError NoDataset = new("QPRO-601", "There is no recorded dataset to train",
        "Record a dataset first (button 1 on the card), give it a name, then select it and train.");
    public static readonly HubError TrainingOutOfMemory = new("QPRO-602", "Training ran out of memory",
        "Close games and other GPU-heavy apps, then train again. On a smaller GPU, Quick refinement uses less memory than Full dataset.");
    public static readonly HubError TrainingFailed = new("QPRO-603", "Training stopped with an error",
        "Press Install runtime once to repair the PC runtime, then train again. If it repeats, record the dataset again.");

    public static readonly HubError Unknown = new("QPRO-900", "Something went wrong",
        "Try again. If it repeats, send the Activity text (click in Activity, press Ctrl+A then Ctrl+C) with this code.");

    // Script/adb output text -> code. First match wins, so specific patterns come first.
    private static readonly (Regex Pattern, HubError Error)[] Patterns =
    [
        (P(@"Python 3\.12 is already installed at .* cannot create a private environment"), PythonExistingBroken),
        (P(@"bundled Python install(er|ation) failed|installer failed its integrity check|bundled Python installer is missing"), PythonInstallerFailed),
        (P(@"Updating pip failed|Installing the tracking runtime failed|PyTorch build(s)? failed|ONNX Runtime could not be installed|Installing the PC preview dependencies failed"), RuntimeDownloadFailed),
        (P(@"failed its final import check|Creating the (local )?Python environment failed|Could not determine the PyTorch training device"), RuntimeBroken),
        (P(@"Set up the PC runtime first|Python environment missing"), RuntimeMissing),
        (P(@"requirements-runtime\.txt is missing|Reinstall the release package|Re-extract the (whole|complete) release|Required release file is missing"), ReleaseIncomplete),
        (P(@"Close VRCFaceTracking before installing"), VrcftRunningDuringInstall),
        (P(@"bridge (DLL was not produced|is missing)|Installed bridge failed its hash check|Backup path already exists|Building the (VRCFT eye|Steam Link VRCFT) bridge failed"), BridgeInstallFailed),
        (P(@"Start VRCFaceTracking first"), VrcftNotRunning),
        (P(@"Start SteamVR first"), SteamVrNotRunning),
        (P(@"label bridge (exited|is missing|executable was not produced)"), LabelBridgeFailed),
        (P(@"adb\.exe (was not found|is missing)|Android Platform Tools were not found"), AdbMissing),
        (P(@"more than one (device|emulator)|Connect exactly one Quest"), MultipleDevices),
        (P(@"\bunauthorized\b"), Unauthorized),
        (P(@"root is not granted|su: (inaccessible or )?not found|Permission denied \(su\)|root access was not granted"), RootMissing),
        (P(@"wireless ADB connection|not reachable at its saved address|No wireless Quest Pro was found|could not be reached on the network|No saved wireless headset"), WifiUnreachable),
        (P(@"Enabling ADB-over-Wi-Fi failed"), WifiEnableFailed),
        (P(@"No authorized Quest was found|ADB target is unavailable|no devices/emulators found|device '.*' not found|device offline"), HeadsetNotFound),
        (P(@"Pushing the (streamer|relay|injector) failed|Marking the headset executables|Setting the streamer library permissions|Preparing the headset logs"), HeadsetFilesFailed),
        (P(@"Injection failed"), InjectionFailed),
        (P(@"root relay (exited|did not begin)|Starting the root relay failed"), RelayFailed),
        (P(@"ADB port forwarding failed"), ForwardFailed),
        (P(@"(Tongue( direction)? model|Model checkpoint) not found|No paired (gate/direction )?(base )?tongue (model|checkpoint)"), ModelMissing),
        (P(@"No completed (quick-refinement|manual tongue-still) capture|not a completed (quick-refinement|full tongue-still) capture|Matching capture is missing"), NoDataset),
        (P(@"CUDA out of memory|out of memory|MemoryError|DefaultCPUAllocator: not enough memory"), TrainingOutOfMemory),
        (P(@"Refining tongue (visibility|direction) failed|Training the (visibility|direction) checkpoint failed|Preparing the (refinement frames|manual still dataset) failed"), TrainingFailed),
        (P(@"PC tracking runtime exited with code|tongue preview exited with code"), TrackingExited),
    ];

    private static Regex P(string pattern) => new(pattern, RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    // The most specific known error in a script's output, or `fallback` when nothing matches.
    public static HubError Diagnose(string? output, HubError? fallback = null)
    {
        if (!string.IsNullOrEmpty(output))
            foreach (var (pattern, error) in Patterns)
                if (pattern.IsMatch(output)) return error;
        return fallback ?? Unknown;
    }

    // The last line that looks like the script's own error message (PowerShell `throw`
    // text), shown under the title so the user sees the concrete reason.
    public static string? LastErrorLine(string? output)
    {
        if (string.IsNullOrEmpty(output)) return null;
        foreach (var raw in output.Split('\n').Reverse())
        {
            var line = raw.Trim();
            if (line.Length is < 12 or > 400) continue;
            if (line.StartsWith("At ", StringComparison.Ordinal) || line.StartsWith("+ ", StringComparison.Ordinal) ||
                line.StartsWith("~", StringComparison.Ordinal) || line.Contains("CategoryInfo") || line.Contains("FullyQualifiedErrorId") ||
                line.Contains("finished with code") || line.Contains("exited with code")) continue;
            if (Regex.IsMatch(line, @"(fail|error|not found|missing|refus|unable|cannot|could not|couldn't|denied|unavailable|required|first\.)", RegexOptions.IgnoreCase))
                return line.Length > 260 ? line[..260] + "…" : line;
        }
        return null;
    }
}
