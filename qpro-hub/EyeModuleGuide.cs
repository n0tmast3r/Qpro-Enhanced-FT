using System.Text.Json;
using System.Text.RegularExpressions;

namespace QproFaceTracking.Hub;

// Guided eye-convergence setup (First-time setup step 3).
//
// The hub remembers what was tried on this headset (eye-module-state.json under
// %LOCALAPPDATA%\QproFaceTracking, so it survives moving to a newer release folder),
// offers to restart the headset after a change, checks after the restart whether the
// module really took effect, and points to the next option when it didn't:
//
//   Sergio's module  ->  restart  ->  verified?  yes: done
//                                              no:  Create my eye patch  ->  restart  ->  verified?
//   Revert to stock  ->  restart  ->  stock model restored (optionally continue with a new patch)
//
// The hub never changes eye-tracking properties or restarts tracking services itself.
// The only headset-wide action it takes is a full restart, and only when the user
// confirms it.
internal sealed partial class HubForm
{
    private const string SergioModuleId = "qpro_individual_eye_enabler";
    // From Sergio's patch_bolt.sh: the stock model his patch accepts and its patched result.
    private const string SergioStockMd5 = "e76c2ea88de1e9ff1d7848a2c02ddde8";
    private const string SergioPatchedMd5 = "499f8b1ab40a24e396a6f0c8d1184414";

    private static class EyeStage
    {
        public const string None = "none";                        // nothing installed / stock
        public const string AwaitingRestart = "awaiting-restart";  // installed, restart pending
        public const string Reverting = "reverting";               // removal flagged, restart pending
        public const string Working = "working";                   // verified active
        public const string Unverified = "unverified";             // enabled, but the hub can't check this module
        public const string NotWorking = "not-working";            // verified NOT active (or reported by the user)
    }

    internal sealed class EyeModuleState
    {
        public string? HeadsetSerial { get; set; }
        public string? Method { get; set; }            // sergio | own-patch | other | revert
        public string? ModuleId { get; set; }
        public string Stage { get; set; } = EyeStage.None;
        public string? BootIdAtChange { get; set; }    // headset boot when the change was made
        public string? LastCheckedBootId { get; set; }
        public string? Firmware { get; set; }
        public string? Summary { get; set; }
        public string? ErrorCode { get; set; }
        public string? ContinueWith { get; set; }      // "own-patch": build a patch once the revert is done
        public DateTimeOffset? ChangedUtc { get; set; }
        public DateTimeOffset? CheckedUtc { get; set; }
        public Dictionary<string, string> Tried { get; set; } = new(StringComparer.Ordinal); // method -> working | not-working
    }

    private sealed record EyeVerdict(string Stage, string Summary, HubError? Error, string? Detail, string? ModuleId);

    private sealed record EyeCheck(
        string BootId, string Serial, string Firmware, string Social, string ModelMd5, string ModelSha, bool ModelCovered,
        IReadOnlyDictionary<string, string> Modules, IReadOnlyDictionary<string, string> Status,
        IReadOnlyDictionary<string, string> PatchedSha, IReadOnlyList<string> SergioLog);

    private static readonly JsonSerializerOptions EyeStateJson = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase, WriteIndented = true };
    private EyeModuleState? _eyeState;
    private EyeVerdict? _eyeVerdict;
    private bool _eyeCheckRunning;

    private static string EyeStatePath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "QproFaceTracking", "eye-module-state.json");

    private EyeModuleState EyeState => _eyeState ??= LoadEyeState();

    private static EyeModuleState LoadEyeState()
    {
        try
        {
            if (File.Exists(EyeStatePath))
            {
                var state = JsonSerializer.Deserialize<EyeModuleState>(File.ReadAllText(EyeStatePath), EyeStateJson);
                if (state is not null)
                {
                    state.Tried = new Dictionary<string, string>(state.Tried ?? new Dictionary<string, string>(), StringComparer.Ordinal);
                    return state;
                }
            }
        }
        catch { }
        return new EyeModuleState();
    }

    private void SaveEyeState()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(EyeStatePath)!);
            File.WriteAllText(EyeStatePath, JsonSerializer.Serialize(EyeState, EyeStateJson));
        }
        catch (Exception error) { AppendLog($"Could not save the eye-module progress: {error.Message}"); }
    }

    private static string MethodOf(string moduleId) =>
        moduleId == SergioModuleId ? "sergio" : moduleId == EyeModelPatcher.ModuleId ? "own-patch" : "other";

    private static string ModuleDisplayName(string moduleId) => moduleId switch
    {
        SergioModuleId => "Sergio's module",
        EyeModelPatcher.ModuleId => "Your eye patch",
        "questpro_independent_gaze" => "Quest Pro Independent Eye Gaze",
        _ => moduleId,
    };

    // The headset's current boot (changes on every restart); null when unreachable.
    private static async Task<string?> ReadBootIdAsync(string adb, string target)
    {
        var probe = await RunAdbProbeAsync(adb, ["-s", target, "shell", "cat", "/proc/sys/kernel/random/boot_id"], 6);
        var value = probe.Output.Trim();
        return probe.Completed && probe.ExitCode == 0 && Regex.IsMatch(value, @"^[0-9a-f-]{36}$") ? value : null;
    }

    // Remember a change the hub just made on the headset, so the result can be checked
    // after the next restart.
    private void RecordEyeModuleChange(string method, string? moduleId, string? bootId, string? continueWith = null)
    {
        var state = EyeState;
        state.Method = method;
        state.ModuleId = moduleId;
        state.Stage = method == "revert" ? EyeStage.Reverting : EyeStage.AwaitingRestart;
        state.BootIdAtChange = bootId;
        state.ContinueWith = continueWith;
        state.Summary = null;
        state.ErrorCode = null;
        state.ChangedUtc = DateTimeOffset.UtcNow;
        SaveEyeState();
        _eyeVerdict = new EyeVerdict(state.Stage,
            method == "revert" ? "Restart the headset to finish going back to the stock eye model." : "Installed. Restart the headset to switch it on.",
            null, null, moduleId);
    }

    // One root probe with everything needed to judge the eye module: the model the tracking
    // service actually sees, the filter property, each recognized module's state, our
    // patch's own boot status and the tail of Sergio's log. Read-only.
    private async Task<EyeCheck?> ProbeEyeModulesAsync(string adb, string target)
    {
        var ids = RecognizedEyeModuleIds(); // every id matched MagiskModuleIdPattern
        var model = EyeModelPatcher.ProductionModelPath;
        // One quoted `su -c` argument (see ScanEyeModulesAsync).
        var shellArg = "su -c '" +
            "echo QPRO_BOOT=$(cat /proc/sys/kernel/random/boot_id); " +
            "echo QPRO_SERIAL=$(getprop ro.serialno); " +
            "echo QPRO_FW=$(getprop ro.build.version.incremental); " +
            "echo QPRO_FILTER=$(getprop debug.oculus.eye_tracking.social_filtering); " +
            "echo QPRO_MD5=$(md5sum " + model + " | cut -d\" \" -f1); " +
            "echo QPRO_SHA=$(sha256sum " + model + " | cut -d\" \" -f1); " +
            "grep -F /odm /proc/1/mountinfo | sed \"s/^/QPRO_MNT /\"; " +
            "for m in " + string.Join(" ", ids) + "; do d=/data/adb/modules/$m; s=none; " +
            "test -d /data/adb/modules_update/$m && s=pending; " +
            "if test -d $d; then s=enabled; " +
            "if test -e $d/update && ! ls $d | grep -qvxE \"module[.]prop|update\"; then s=pending; fi; " +
            "test -e $d/disable && s=disabled; test -e $d/remove && s=removing; fi; " +
            "echo QPRO_MOD $m $s; " +
            "test -f $d/qpro_status && echo QPRO_STATUS $m $(head -c 200 $d/qpro_status); " +
            "test -f $d/qpro-eye.conf && echo QPRO_CONF $m $(grep -m1 ^PATCHED_SHA= $d/qpro-eye.conf | cut -d= -f2); " +
            "done; " +
            "test -f /data/local/tmp/model_patcher.log && tail -n 4 /data/local/tmp/model_patcher.log | sed \"s/^/QPRO_SLOG /\"; " +
            "echo QPRO_DONE'";
        var probe = await RunAdbProbeAsync(adb, ["-s", target, "shell", shellArg], 45);
        if (!probe.Completed || !probe.Output.Contains("QPRO_DONE", StringComparison.Ordinal)) return null;

        string Value(string key) => probe.Output.Split('\n').Select(line => line.TrimEnd('\r'))
            .FirstOrDefault(line => line.StartsWith(key + "=", StringComparison.Ordinal))?[(key.Length + 1)..].Trim() ?? "";
        var modules = new Dictionary<string, string>(StringComparer.Ordinal);
        var status = new Dictionary<string, string>(StringComparer.Ordinal);
        var patched = new Dictionary<string, string>(StringComparer.Ordinal);
        var sergioLog = new List<string>();
        var covered = false;
        foreach (var raw in probe.Output.Split('\n'))
        {
            var line = raw.TrimEnd('\r');
            if (line.StartsWith("QPRO_MNT ", StringComparison.Ordinal)) covered |= MountCoversModel(line[9..], model);
            else if (line.StartsWith("QPRO_SLOG ", StringComparison.Ordinal)) sergioLog.Add(line[10..].Trim());
            else
            {
                var match = Regex.Match(line, @"^QPRO_(MOD|STATUS|CONF) (\S+) ?(.*)$");
                if (!match.Success || !ids.Contains(match.Groups[2].Value)) continue;
                var target_ = match.Groups[1].Value switch { "MOD" => modules, "STATUS" => status, _ => patched };
                target_[match.Groups[2].Value] = match.Groups[3].Value.Trim();
            }
        }
        return new EyeCheck(Value("QPRO_BOOT"), Value("QPRO_SERIAL"), Value("QPRO_FW"), Value("QPRO_FILTER"),
            Value("QPRO_MD5"), Value("QPRO_SHA"), covered, modules, status, patched, sergioLog);
    }

    private static EyeVerdict EvaluateEyeCheck(EyeCheck check, EyeModuleState state)
    {
        var restarted = state.BootIdAtChange is null || state.BootIdAtChange != check.BootId;
        string StateOf(string id) => check.Modules.TryGetValue(id, out var value) ? value : "none";

        if (state.Method == "revert")
        {
            var left = check.Modules.Where(pair => pair.Value is "enabled" or "pending" or "removing").Select(pair => ModuleDisplayName(pair.Key)).ToList();
            if (!restarted)
                return new(EyeStage.Reverting, "Removal is scheduled. Restart the headset to finish going back to the stock eye model.", null, null, null);
            if (left.Count > 0 || check.ModelCovered)
                return new(EyeStage.NotWorking, "The stock eye model isn't fully restored yet.", ErrorCodes.RevertIncomplete,
                    left.Count > 0 ? "Still installed: " + string.Join(", ", left) + "." : "Something is still mounted over the eye model.", null);
            return new(EyeStage.None, "The stock eye model is restored. No eye module is installed.", null, null, null);
        }

        // Judge the module the hub installed; otherwise any recognized one that is enabled.
        var id = state.ModuleId is not null && StateOf(state.ModuleId) != "none" ? state.ModuleId
            : check.Modules.FirstOrDefault(pair => pair.Value == "enabled").Key
              ?? check.Modules.FirstOrDefault(pair => pair.Value == "pending").Key;
        if (id is null) return new(EyeStage.None, "No eye convergence module is installed.", null, null, null);
        var name = ModuleDisplayName(id);
        var moduleState = StateOf(id);
        var ours = state.ModuleId == id && state.BootIdAtChange is not null;

        if (moduleState == "pending")
            return ours && restarted
                ? new(EyeStage.NotWorking, $"{name} is still waiting to be installed after the restart.", ErrorCodes.StillPendingAfterRestart, null, id)
                : new(EyeStage.AwaitingRestart, $"{name} is installed. Restart the headset to switch it on.", null, null, id);
        if (moduleState is "disabled" or "removing")
            return new(EyeStage.None, $"{name} is {(moduleState == "disabled" ? "disabled in Magisk" : "being removed")}, so no eye module is active.", null, null, id);
        if (ours && !restarted)
            return new(EyeStage.AwaitingRestart, $"{name} is installed. Restart the headset to finish applying it.", null, null, id);

        var filterOff = check.Social == "0";
        switch (MethodOf(id))
        {
            case "sergio":
                if (check.ModelMd5 == SergioPatchedMd5)
                    return filterOff
                        ? new(EyeStage.Working, $"{name} is working: the patched eye model is loaded and the eye-coupling filter is off.", null, null, id)
                        : new(EyeStage.NotWorking, $"{name} patched the eye model, but the eye-coupling filter is still on.", ErrorCodes.FilterStillOn, null, id);
                var log = check.SergioLog.LastOrDefault(line => line.Contains("[-]", StringComparison.Ordinal)) ?? check.SergioLog.LastOrDefault();
                var modelNote = check.ModelMd5 == SergioStockMd5 ? "" : " This firmware's eye model is different from the one Sergio's patch supports.";
                return new(EyeStage.NotWorking,
                    $"{name} is installed, but it didn't patch this headset's eye model (firmware {Fallback(check.Firmware)}).{modelNote}",
                    check.ModelCovered && check.ModelMd5 != SergioStockMd5 ? ErrorCodes.EyeModelCovered : ErrorCodes.SergioNotApplied,
                    log is null ? null : "Module log: " + log, id);
            case "own-patch":
                var status = check.Status.GetValueOrDefault(id, "");
                var patchedSha = check.PatchedSha.GetValueOrDefault(id, "");
                if (status == "mounted" && patchedSha.Length == 64 && check.ModelSha == patchedSha)
                    return filterOff
                        ? new(EyeStage.Working, $"{name} is working: your patched eye model is loaded and the eye-coupling filter is off.", null, null, id)
                        : new(EyeStage.NotWorking, $"{name} is loaded, but the eye-coupling filter is still on.", ErrorCodes.FilterStillOn, null, id);
                return new(EyeStage.NotWorking, $"{name} is installed but not active.", ErrorCodes.OwnPatchInactive,
                    status.Length > 0 && status != "mounted" ? "Patch status on the headset: " + status : "The patched eye model isn't the one being used.", id);
            default:
                return new(EyeStage.Unverified,
                    $"{name} is enabled. The hub can't check this module automatically; look at your avatar in a mirror to confirm the eyes can converge.", null, null, id);
        }
    }

    private static string Fallback(string value) => string.IsNullOrWhiteSpace(value) ? "unknown" : value;

    // Store the verdict. Returns the stage it replaced, so callers can notice changes.
    private string ApplyEyeVerdict(EyeCheck check, EyeVerdict verdict)
    {
        var state = EyeState;
        if (!string.IsNullOrEmpty(state.HeadsetSerial) && check.Serial.Length > 0 && state.HeadsetSerial != check.Serial)
        {
            AppendLog("A different headset is connected; starting its eye-module progress fresh.");
            state = _eyeState = new EyeModuleState();
        }
        var previous = state.Stage;
        if (check.Serial.Length > 0) state.HeadsetSerial = check.Serial;
        if (check.Firmware.Length > 0) state.Firmware = check.Firmware;
        state.LastCheckedBootId = check.BootId;
        state.CheckedUtc = DateTimeOffset.UtcNow;
        if (state.Method == "revert" && verdict.Stage == EyeStage.None) { state.Method = null; state.ModuleId = null; }
        if (state.Method != "revert" && verdict.ModuleId is not null)
        {
            state.ModuleId = verdict.ModuleId;
            state.Method = MethodOf(verdict.ModuleId);
        }
        state.Stage = verdict.Stage;
        state.Summary = verdict.Summary;
        state.ErrorCode = verdict.Error?.Code;
        if (state.Method is "sergio" or "own-patch" or "other" && verdict.Stage is EyeStage.Working or EyeStage.NotWorking)
            state.Tried[state.Method] = verdict.Stage;
        SaveEyeState();
        _eyeVerdict = verdict;
        return previous;
    }

    // Full check now (after a restart, or when the user presses Check now).
    private async Task<EyeVerdict?> CheckEyeModuleNowAsync(string target, bool showResult)
    {
        var adb = FindAdb();
        if (adb is null) { if (showResult) ShowError(ErrorCodes.AdbMissing); return null; }
        _eyeCheckRunning = true;
        try
        {
            AppendLog("Checking the eye convergence module on the headset…");
            if (!await HeadsetRootedAsync(target))
            {
                if (showResult)
                    ShowError(ErrorCodes.RootMissing,
                        "The headset is connected, but root isn't active, so the eye module can't be checked yet. If the headset just restarted, re-apply root first; the hub then checks automatically.",
                        "Eye module check");
                return null;
            }
            var check = await ProbeEyeModulesAsync(adb, target);
            if (check is null)
            {
                if (showResult) ShowError(ErrorCodes.EyeModelUnreadable, null, "Eye module check");
                return null;
            }
            var verdict = EvaluateEyeCheck(check, EyeState);
            ApplyEyeVerdict(check, verdict);
            AppendLog($"[Eye module] {verdict.Summary}" + (verdict.Error is null ? "" : $" ({verdict.Error.Code})") + (verdict.Detail is null ? "" : $" {verdict.Detail}"));
            if (showResult) ShowEyeVerdict(verdict);
            return verdict;
        }
        finally { _eyeCheckRunning = false; }
    }

    private void ShowEyeVerdict(EyeVerdict verdict)
    {
        if (verdict.Error is not null)
        {
            ShowError(verdict.Error, verdict.Summary + (verdict.Detail is null ? "" : "\n" + verdict.Detail), "Eye convergence");
            return;
        }
        var working = verdict.Stage == EyeStage.Working;
        if (working || verdict.Stage == EyeStage.None) PlaySfx("succeed.wav");
        MessageBox.Show(
            this,
            (working ? "✓ " : "") + verdict.Summary +
            (working ? "\n\nYou're all set. You don't need to redo eye-tracking calibration. Start Virtual Desktop and look at your avatar in a mirror; if the eyes still move together, open Manage eye module and choose \"My eyes still move together\"." : ""),
            working ? "Eye convergence is on" : "Eye convergence",
            MessageBoxButtons.OK,
            MessageBoxIcon.Information);
    }

    // Called from the regular status refresh: check once per headset boot while there is
    // something to check, and tell the user when a result changes (for example after they
    // restarted the headset themselves).
    private async Task MaybeAutoCheckEyeModuleAsync(string target, EyeModuleScan scan)
    {
        if (_eyeModuleBusy || _eyeCheckRunning || string.IsNullOrEmpty(scan.BootId)) return;
        var state = EyeState;
        var interesting = scan.Active.Count + scan.Pending.Count + scan.Inactive.Count > 0 || state.Stage != EyeStage.None;
        if (!interesting || state.LastCheckedBootId == scan.BootId) return;
        var before = _eyeVerdict?.Stage ?? state.Stage;
        var verdict = await CheckEyeModuleNowAsync(target, showResult: false);
        if (verdict is null || verdict.Stage == before) return;
        var announce = before is EyeStage.AwaitingRestart or EyeStage.Reverting or EyeStage.Working
            && verdict.Stage is EyeStage.Working or EyeStage.NotWorking or EyeStage.None or EyeStage.Unverified;
        if (!announce) return;
        if (ActiveForm == this && !_eyeModuleBusy)
            BeginInvoke(async () =>
            {
                ShowEyeVerdict(verdict);
                if (verdict.Stage != EyeStage.None || EyeState.ContinueWith != "own-patch" || _eyeModuleBusy) return;
                _eyeModuleBusy = true;
                SetSetupButtonsEnabled(false);
                try { await ContinueAfterRevertAsync(); }
                catch (Exception error) { ShowError(ErrorCodes.Unknown, error.Message, "Create my eye patch"); }
                finally { _eyeModuleBusy = false; SetSetupButtonsEnabled(true); await RefreshStatusAsync(); }
            });
    }

    // Ask, then restart the headset and wait for it to come back. True when it is back.
    private async Task<bool> RestartHeadsetAsync(string target, string why, bool ask = true)
    {
        if (_trackingProcesses.Any(process => !process.HasExited))
        {
            MessageBox.Show(this, "Stop live tracking before restarting the headset.", "Restart headset", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return false;
        }
        if (ask && MessageBox.Show(
                this,
                why + "\n\nRestart the headset now? Virtual Desktop and anything else running on the headset will close. " +
                "Keep the cable connected (on Wi-Fi, keep the headset on this network). The hub waits for it and then checks the eye module.",
                "Restart headset",
                MessageBoxButtons.YesNo,
                MessageBoxIcon.Question) != DialogResult.Yes)
            return false;
        var adb = FindAdb();
        if (adb is null) { ShowError(ErrorCodes.AdbMissing); return false; }
        var before = await ReadBootIdAsync(adb, target);
        BeginSetupProgress("Restart headset");
        _setupProgressStatus.Text = "Restarting the headset…";
        AppendLog("Restarting the headset (confirmed by you)…");
        // A plain adb reboot (the same as holding the power button > Restart).
        await RunAdbProbeAsync(adb, ["-s", target, "reboot"], 20);
        var wifi = target.Contains(':');
        string? after = null;
        var sawOffline = false; // when the old boot id is unknown, wait until the headset has gone away once
        var deadline = DateTime.UtcNow.AddMinutes(4);
        await Task.Delay(8000);
        while (DateTime.UtcNow < deadline)
        {
            _setupProgressStatus.Text = "Waiting for the headset to start again… If it goes to sleep, put it on for a moment.";
            if (wifi) await RunAdbProbeAsync(adb, ["connect", target], 6);
            var boot = await ReadBootIdAsync(adb, target);
            if (boot is null) sawOffline = true;
            else if (before is not null ? boot != before : sawOffline)
            {
                var completed = await RunAdbProbeAsync(adb, ["-s", target, "shell", "getprop", "sys.boot_completed"], 6);
                if (completed.Output.Trim() == "1") { after = boot; break; }
            }
            await Task.Delay(3000);
        }
        if (after is null)
        {
            _lastError = ErrorCodes.RestartTimeout;
            FinishSetupProgress(false, "Restart headset");
            if (_eyeModuleBusy) SetSetupButtonsEnabled(false);
            ShowError(ErrorCodes.RestartTimeout, null, "Restart headset");
            return false;
        }
        AppendLog("The headset is back. Giving the eye module a moment to start…");
        _setupProgressStatus.Text = "The headset is back. Giving the eye module a moment to start…";
        // Sergio's service script and ours both run shortly after boot completes.
        await Task.Delay(20000);
        FinishSetupProgress(true, "Restart headset");
        if (_eyeModuleBusy) SetSetupButtonsEnabled(false); // the eye-module flow re-enables them when it ends
        _setupProgressContainer.Visible = false;
        return true;
    }

    // After a change: offer the restart right away (recommended), then check the result.
    private async Task<bool> OfferRestartAndCheckAsync(string target, string why)
    {
        if (!await RestartHeadsetAsync(target, why))
        {
            AppendLog("Restart the headset when you're ready; the hub checks the eye module automatically afterwards.");
            return false;
        }
        if (!await HeadsetRootedAsync(target))
        {
            PlaySfx("warning.wav");
            MessageBox.Show(
                this,
                "The headset restarted. Re-apply root with your root method now (if it needs that after a restart).\n\n" +
                "The hub checks the eye module automatically as soon as root is back, or press Check now in Manage eye module.",
                "Re-apply root",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
            return true;
        }
        var verdict = await CheckEyeModuleNowAsync(target, showResult: true);
        await RefreshStatusAsync();
        if (verdict?.Stage == EyeStage.None && EyeState.ContinueWith == "own-patch") await ContinueAfterRevertAsync();
        return true;
    }

    private async Task ContinueAfterRevertAsync()
    {
        EyeState.ContinueWith = null;
        SaveEyeState();
        if (MessageBox.Show(this, "The stock eye model is back. Continue with Create my eye patch now?", "Create my eye patch",
                MessageBoxButtons.YesNo, MessageBoxIcon.Question) == DialogResult.Yes)
            await BuildOwnEyePatchAsync();
    }

    // Remove every recognized eye module and go back to the stock eye model. Magisk removes
    // flagged modules during the next restart and runs their uninstall scripts.
    private async Task RevertToStockAsync(bool thenBuildPatch = false)
    {
        if (_trackingProcesses.Any(process => !process.HasExited)) { MessageBox.Show(this, "Stop live tracking before changing the eye module."); return; }
        var target = await ActiveTargetAsync();
        if (!await EnsureHeadsetRootAsync(target)) return;
        var adb = FindAdb()!;
        var modules = await ListHeadsetModulesAsync(adb, target!);
        if (modules is null) { ShowError(ErrorCodes.EyeModelUnreadable, "Could not read the Magisk modules on the headset.", "Revert to stock"); return; }
        var recognized = RecognizedEyeModuleIds();
        var present = modules.Where(module => recognized.Contains(module.Id) && module.State != "removing").ToList();
        if (present.Count == 0)
        {
            var pendingRemoval = modules.Any(module => recognized.Contains(module.Id));
            MessageBox.Show(this,
                pendingRemoval
                    ? "The eye module is already set to be removed. Restart the headset to finish going back to stock."
                    : "No eye convergence module is installed. The headset is using its stock eye model.",
                "Revert to stock", MessageBoxButtons.OK, MessageBoxIcon.Information);
            if (!pendingRemoval) return;
        }
        else if (MessageBox.Show(
                     this,
                     "Remove the eye convergence module and go back to the stock eye model?\n\n• " +
                     string.Join("\n• ", present.Select(DescribeModule)) +
                     "\n\nMagisk removes it during the next restart, and the headset uses its original eye model again. Nothing of Meta's is changed on disk.",
                     "Revert to stock",
                     MessageBoxButtons.OKCancel,
                     MessageBoxIcon.Question) != DialogResult.OK)
            return;
        var ids = present.Select(module => module.Id).Where(IsValidModuleId).ToList();
        if (ids.Count > 0)
        {
            // Every id matched MagiskModuleIdPattern; one quoted `su -c` argument.
            var shellArg = "su -c 'for m in " + string.Join(" ", ids) + "; do " +
                "if test -d /data/adb/modules/$m; then touch /data/adb/modules/$m/remove && echo QPRO_REMOVE:$m; fi; " +
                "if test -d /data/adb/modules_update/$m; then rm -rf /data/adb/modules_update/$m && echo QPRO_REMOVE:$m; fi; done'";
            var probe = await RunAdbProbeAsync(adb, ["-s", target!, "shell", shellArg], 15);
            var removed = ids.Where(id => Regex.IsMatch(probe.Output, "^QPRO_REMOVE:" + Regex.Escape(id) + @"\r?$", RegexOptions.Multiline)).ToList();
            if (removed.Count < ids.Count)
            {
                ShowError(ErrorCodes.RevertIncomplete, "Could not flag for removal: " + string.Join(", ", ids.Except(removed)) + ".", "Revert to stock");
                return;
            }
            AppendLog("Flagged for removal in Magisk (applies at the next restart): " + string.Join(", ", removed));
        }
        RecordEyeModuleChange("revert", null, await ReadBootIdAsync(adb, target!), thenBuildPatch ? "own-patch" : null);
        await RefreshStatusAsync();
        await OfferRestartAndCheckAsync(target!, "The eye module is set to be removed. A restart finishes going back to the stock eye model.");
    }

    // The user says the eyes still move together although the checks passed.
    private void ReportEyesStillCoupled()
    {
        var state = EyeState;
        state.Stage = EyeStage.NotWorking;
        state.Summary = "You reported that the eyes still move together.";
        state.ErrorCode = null;
        if (state.Method is "sergio" or "own-patch" or "other") state.Tried[state.Method] = EyeStage.NotWorking;
        SaveEyeState();
        _eyeVerdict = new EyeVerdict(EyeStage.NotWorking, state.Summary, null, null, state.ModuleId);
        AppendLog("[Eye module] Marked as not working (reported by you).");
    }

    private string CurrentEyeStage() => _eyeVerdict?.Stage ?? EyeState.Stage;

    // What the guide recommends next, from the current result and what was already tried.
    private (string Text, EyeModuleAction Action, string Label) EyeNextStep()
    {
        var state = EyeState;
        var stage = CurrentEyeStage();
        var error = _eyeVerdict?.Error?.Code ?? state.ErrorCode;
        var sergioFailed = state.Tried.GetValueOrDefault("sergio") == EyeStage.NotWorking;
        var ownFailed = state.Tried.GetValueOrDefault("own-patch") == EyeStage.NotWorking;
        switch (stage)
        {
            case EyeStage.AwaitingRestart:
                return ("Restart the headset to switch the module on. The hub checks it automatically afterwards.", EyeModuleAction.Restart, "Restart headset now");
            case EyeStage.Reverting:
                return ("Restart the headset to finish going back to the stock eye model.", EyeModuleAction.Restart, "Restart headset now");
            case EyeStage.Working:
                return ("Nothing else to do: convergence is on and was verified on the headset. You don't need to redo eye-tracking calibration.", EyeModuleAction.None, "");
            case EyeStage.Unverified:
                return ("Check your avatar in a mirror. If the eyes still move together, choose \"My eyes still move together\" below.", EyeModuleAction.None, "");
            case EyeStage.NotWorking:
                if (error is "QPRO-510" or "QPRO-504" or "QPRO-511")
                    return ("Restart the headset once more (re-apply root first if your root method needs it).", EyeModuleAction.Restart, "Restart headset now");
                if (state.Method == "sergio" || (sergioFailed && !ownFailed && state.Method != "own-patch"))
                    return ("Sergio's module didn't work on this headset. Next: create a patch from your own headset's eye model. The hub offers to switch Sergio's module off during the install.",
                        EyeModuleAction.BuildPatch, "Create my eye patch");
                if (state.Method == "own-patch")
                    return error == "QPRO-503"
                        ? ("Rebuild your eye patch for this headset's current firmware, then restart.", EyeModuleAction.BuildPatch, "Rebuild my eye patch")
                        : ("Try creating the patch again and pick the other patch type (exact rewire), or go back to stock and report your firmware number.", EyeModuleAction.BuildPatch, "Create my eye patch again");
                return ("Go back to the stock eye model, then try Sergio's module or your own patch.", EyeModuleAction.Revert, "Revert to stock");
            default:
                if (sergioFailed && ownFailed)
                    return ("Neither module has worked on this headset yet. You can stay on stock and report your firmware number, or try the exact rewire patch type.", EyeModuleAction.BuildPatch, "Create my eye patch");
                if (sergioFailed)
                    return ("Sergio's module didn't work on this headset before. Next: create a patch from your own headset's eye model.", EyeModuleAction.BuildPatch, "Create my eye patch");
                return ("Start with Sergio's module (recommended). If it doesn't work on your headset, the hub guides you to the next option.", EyeModuleAction.InstallSergio, "Install Sergio's module");
        }
    }

    private (string Text, Color Color) EyeStatusLine()
    {
        var stage = CurrentEyeStage();
        var summary = _eyeVerdict?.Summary ?? EyeState.Summary;
        var code = _eyeVerdict?.Error?.Code ?? EyeState.ErrorCode;
        return stage switch
        {
            EyeStage.Working => ("● Convergence on (verified). " + summary, Good),
            EyeStage.Unverified => ("● Module enabled (can't be verified automatically). " + summary, Good),
            EyeStage.AwaitingRestart => ("● Restart needed. " + summary, Warning),
            EyeStage.Reverting => ("● Restart needed to finish going back to stock.", Warning),
            EyeStage.NotWorking => ("● Not working" + (code is null ? ". " : $" ({code}). ") + summary, Bad),
            _ => _eyeModuleActive ? ("● An eye module is enabled; press Check now to verify it.", Warning)
                : _eyeModulePending ? ("● Restart needed: your eye module goes live when the headset restarts.", Warning)
                : _eyeModuleInactive ? ("● Your eye patch is inactive: the headset's eye model changed; create it again.", Warning)
                : ("○ No eye convergence module detected (or the headset isn't connected).", Muted),
        };
    }

    private string GazeStatusText() => CurrentEyeStage() switch
    {
        EyeStage.Working => "Convergence on",
        EyeStage.Unverified => "Module on",
        EyeStage.AwaitingRestart => "Restart headset",
        EyeStage.Reverting => "Restart headset",
        EyeStage.NotWorking => "Not working · step 3",
        _ => _eyeModuleActive ? "Module on" : _eyeModulePending ? "Restart headset" : _eyeModuleInactive ? "Patch inactive" : "Module needed",
    };

    // Step 3's dialog: where you are, the recommended next step, and everything else.
    private sealed class EyeModuleGuideDialog : Form
    {
        public EyeModuleAction Action { get; private set; } = EyeModuleAction.None;

        public EyeModuleGuideDialog(
            string status, Color statusColor, string nextText, EyeModuleAction nextAction, string nextLabel,
            IReadOnlyList<(EyeModuleAction Action, string Label)> others, string about, string advancedGuide)
        {
            StyleDialog(this, "Eye convergence module");
            var layout = DialogLayout();
            layout.Controls.Add(new Label { Text = status, AutoSize = true, MaximumSize = new Size(620, 0), ForeColor = statusColor, Font = new Font(UiFontName, 10F, FontStyle.Bold), Margin = new Padding(0, 0, 0, 12) });

            var next = new TableLayoutPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 1, BackColor = Raised, Padding = new Padding(14), Margin = new Padding(0, 0, 0, 14) };
            next.Controls.Add(new Label { Text = "NEXT STEP", AutoSize = true, ForeColor = Warning, Font = new Font(UiFontName, 8.5F, FontStyle.Bold), Margin = new Padding(0, 0, 0, 4) });
            next.Controls.Add(new Label { Text = nextText, AutoSize = true, MaximumSize = new Size(590, 0), ForeColor = Color.White, Margin = new Padding(0, 0, 0, nextAction == EyeModuleAction.None ? 0 : 10) });
            if (nextAction != EyeModuleAction.None)
                next.Controls.Add(DialogButton(nextLabel + (nextAction is EyeModuleAction.Restart or EyeModuleAction.CheckNow ? "" : "…"), true, () => Choose(nextAction)));
            layout.Controls.Add(next);

            if (others.Count > 0)
            {
                layout.Controls.Add(new Label { Text = "Other options", AutoSize = true, ForeColor = Muted, Font = new Font(UiFontName, 9F, FontStyle.Bold), Margin = new Padding(0, 0, 0, 4) });
                var row = new FlowLayoutPanel { AutoSize = true, WrapContents = true, MaximumSize = new Size(640, 0), Margin = new Padding(0, 0, 0, 12) };
                foreach (var (action, label) in others)
                {
                    var button = DialogButton(label, false, () => Choose(action));
                    button.Margin = new Padding(0, 0, 8, 8);
                    row.Controls.Add(button);
                }
                layout.Controls.Add(row);
            }

            layout.Controls.Add(new Label { Text = about, AutoSize = true, MaximumSize = new Size(620, 0), ForeColor = Muted, Margin = new Padding(0, 0, 0, 14) });

            var advanced = new TableLayoutPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 1, Visible = false, BackColor = Panel, Margin = new Padding(0, 8, 0, 0) };
            advanced.Controls.Add(new Label { Text = advancedGuide, AutoSize = true, MaximumSize = new Size(620, 0), ForeColor = Muted, Margin = new Padding(0, 0, 0, 8) });
            var advancedActions = new FlowLayoutPanel { AutoSize = true, WrapContents = true, MaximumSize = new Size(640, 0), Margin = new Padding(0) };
            advancedActions.Controls.Add(DialogButton("Install module (.zip)…", false, () => Choose(EyeModuleAction.InstallZip)));
            advancedActions.Controls.Add(DialogButton("Choose installed…", false, () => Choose(EyeModuleAction.ChooseInstalled)));
            advanced.Controls.Add(advancedActions);

            var footer = new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = new Padding(0) };
            DarkButton toggle = null!;
            toggle = DialogButton("Advanced ▸", false, () =>
            {
                advanced.Visible = !advanced.Visible;
                toggle.Text = advanced.Visible ? "Advanced ▾" : "Advanced ▸";
            });
            var close = DialogButton("Close", false, () => Choose(EyeModuleAction.None));
            footer.Controls.Add(toggle);
            footer.Controls.Add(close);
            layout.Controls.Add(footer);
            layout.Controls.Add(advanced);
            Controls.Add(layout);
            CancelButton = close;
        }

        private void Choose(EyeModuleAction action)
        {
            Action = action;
            DialogResult = action == EyeModuleAction.None ? DialogResult.Cancel : DialogResult.OK;
        }
    }
}
