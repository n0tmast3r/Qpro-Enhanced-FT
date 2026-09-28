using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace QproFaceTracking.Hub;

// `QproFaceTracking.exe --layout-check <folder>` lays the hub and its dialogs out at
// 100-200% Windows scaling and reports anything cut off: controls that stick out of a
// non-scrolling parent, and button/toggle text that doesn't fit. It also saves a
// screenshot per scale and page into <folder> so a person can look. Exit code 0 = no
// problems found, 1 = problems (listed in layout-check.json).
//
// Scaling is simulated the way WinForms applies it for real (Control.Scale for sizes,
// margins, paddings and fixed table rows, plus every font enlarged by the same factor),
// so one PC can check all scaling levels without changing Windows settings.
internal sealed partial class HubForm
{
    internal static bool LayoutCheckMode;
    private static readonly float[] LayoutCheckScales = [1.0f, 1.25f, 1.5f, 1.75f, 2.0f];

    internal static int RunLayoutCheck(string root, string outputFolder)
    {
        LayoutCheckMode = true;
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.ThrowException);
        Directory.CreateDirectory(outputFolder);
        var report = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        foreach (var scale in LayoutCheckScales)
        {
            var key = $"{scale * 100:0}%";
            var issues = new List<string>();
            report[key] = issues;
            using (var form = new HubForm(root) { StartPosition = FormStartPosition.Manual, Location = new Point(0, 0), Opacity = 0 })
            {
                form.Show();
                Application.DoEvents();
                SimulateScale(form, scale);
                form.PerformLayout();
                Application.DoEvents();
                var tabs = new List<Control>();
                CollectControls(form, control => Equals(control.Tag, "workflow-tab"), tabs);
                if (tabs.Count == 0) tabs.Add(form); // still check the page once
                foreach (var tab in tabs)
                {
                    (tab as Button)?.PerformClick();
                    form.PerformLayout();
                    Application.DoEvents();
                    var page = tab == form ? "hub" : tab.Text.Trim();
                    FindLayoutProblems(form, $"{page}", issues);
                    SaveShot(form, Path.Combine(outputFolder, $"hub-{scale * 100:0}-{Safe(page)}.png"));
                    var scroller = FindVisibleScroller(form);
                    if (scroller is not null && scroller.VerticalScroll.Visible)
                    {
                        scroller.AutoScrollPosition = new Point(0, scroller.VerticalScroll.Maximum);
                        Application.DoEvents();
                        SaveShot(form, Path.Combine(outputFolder, $"hub-{scale * 100:0}-{Safe(page)}-bottom.png"));
                        scroller.AutoScrollPosition = new Point(0, 0);
                    }
                }
                form._mode = ConnectionMode.WiFi; // shows the Wi-Fi button without touching the headset
                form.StyleModeButtons();
                form.PerformLayout();
                Application.DoEvents();
                FindLayoutProblems(form, "hub (Wi-Fi mode)", issues);
                form.Close();
            }

            // The dialogs people see most, filled with realistic long text.
            CheckDialog(new EyeModuleGuideDialog(
                "● Not working (QPRO-502). Sergio's module is installed, but it didn't patch this headset's eye model (firmware 51483620027600340).",
                Bad,
                "Sergio's module didn't work on this headset. Next: create a patch from your own headset's eye model. The hub offers to switch Sergio's module off during the install.",
                EyeModuleAction.BuildPatch, "Create my eye patch",
                [(EyeModuleAction.InstallSergio, "Install Sergio's module…"), (EyeModuleAction.CheckNow, "Check now"), (EyeModuleAction.Restart, "Restart headset"), (EyeModuleAction.Revert, "Revert to stock…"), (EyeModuleAction.ReportNotWorking, "My eyes still move together")],
                "Eye convergence needs one independent-eye Magisk module on the headset. No Meta files are shipped or downloaded. Install or change modules before starting Virtual Desktop.",
                EyeModuleAdvancedText()), "eye module guide", scale, outputFolder, issues);
            CheckDialog(new EyeModuleChooserDialog([
                new EyeModuleEntry("qpro_individual_eye_enabler", "Quest Pro Individual Eye Enabler (qpro_individual_eye_enabler) · v1.0.0 · SergioMarquina", true, true),
                new EyeModuleEntry("some_other_module", "A module with a rather long name (some_other_module) · v2.3.4 · Someone", false, false)]),
                "eye module chooser", scale, outputFolder, issues);
            CheckDialog(new TextPromptDialog("Name this dataset", "Give this recording a friendly name. It is shown in the training queue and carried into the trained model.", "Quick refinement 1",
                UiFontName, Background, Panel, Raised, Border, Accent), "name prompt", scale, outputFolder, issues);
        }
        var problems = report.Values.Sum(list => list.Count);
        File.WriteAllText(Path.Combine(outputFolder, "layout-check.json"), JsonSerializer.Serialize(new
        {
            ok = problems == 0,
            problems,
            scales = report,
        }, new JsonSerializerOptions { WriteIndented = true }));
        return problems == 0 ? 0 : 1;
    }

    private static void CheckDialog(Form dialog, string name, float scale, string outputFolder, List<string> issues)
    {
        using (dialog)
        {
            dialog.StartPosition = FormStartPosition.Manual;
            dialog.Location = new Point(0, 0);
            dialog.Opacity = 0;
            dialog.Show();
            Application.DoEvents();
            SimulateScale(dialog, scale);
            dialog.PerformLayout();
            Application.DoEvents();
            FindLayoutProblems(dialog, name, issues);
            SaveShot(dialog, Path.Combine(outputFolder, $"dialog-{scale * 100:0}-{Safe(name)}.png"));
            dialog.Close();
        }
    }

    // What WinForms does at a higher DPI: scale bounds, margins, paddings and fixed-size
    // table rows/columns, and enlarge every font by the same factor. `target` is the
    // Windows scaling to simulate; this PC's own scaling is already applied.
    private static void SimulateScale(Control root, float target)
    {
        var factor = target / (root.DeviceDpi / 96f);
        if (Math.Abs(factor - 1f) < 0.01f) return;
        var explicitFonts = new List<(Control Control, Font Font)>();
        void Collect(Control parent)
        {
            foreach (Control child in parent.Controls)
            {
                if (!ReferenceEquals(child.Font, parent.Font)) explicitFonts.Add((child, child.Font));
                Collect(child);
            }
        }
        Collect(root);
        var rootFont = root.Font;
        root.Scale(new SizeF(factor, factor));
        root.Font = new Font(rootFont.FontFamily, rootFont.Size * factor, rootFont.Style);
        foreach (var (control, font) in explicitFonts)
            control.Font = new Font(font.FontFamily, font.Size * factor, font.Style);
    }

    private static void FindLayoutProblems(Control parent, string where, List<string> issues)
    {
        var parentScrolls = parent is ScrollableControl { AutoScroll: true };
        foreach (Control control in parent.Controls)
        {
            if (!control.Visible) continue;
            var name = DescribeControl(control);
            // Something sticking out of a parent that can't scroll is cut off.
            if (!parentScrolls && parent is not Form && control is not SplitterPanel)
            {
                var client = parent.ClientRectangle;
                if (control.Right > client.Width + 2 || control.Bottom > client.Height + 2)
                    issues.Add($"{where}: {name} is cut off by its container ({control.Right}x{control.Bottom} in {client.Width}x{client.Height}).");
            }
            // Fixed-size buttons and toggles whose text doesn't fit.
            if (control is ButtonBase button && !button.AutoSize && button.Text.Trim().Length > 0 && button.Width > 0)
            {
                var need = TextRenderer.MeasureText(button.Text.Trim(), button.Font);
                if (need.Width > button.ClientSize.Width - 6 || need.Height > button.ClientSize.Height)
                    issues.Add($"{where}: the text of {name} doesn't fit ({need.Width}x{need.Height} needed, {button.ClientSize.Width}x{button.ClientSize.Height} available).");
            }
            FindLayoutProblems(control, where, issues);
        }
    }

    private static string DescribeControl(Control control)
    {
        var text = control.Text.Replace('\n', ' ').Trim();
        if (text.Length > 40) text = text[..40] + "…";
        return control.GetType().Name + (text.Length > 0 ? $" \"{text}\"" : "");
    }

    private static void CollectControls(Control root, Func<Control, bool> predicate, List<Control> found)
    {
        foreach (Control child in root.Controls)
        {
            if (predicate(child)) found.Add(child);
            CollectControls(child, predicate, found);
        }
    }

    private static ScrollableControl? FindVisibleScroller(Control root)
    {
        foreach (Control child in root.Controls)
        {
            if (!child.Visible) continue;
            var nested = FindVisibleScroller(child);
            if (nested is not null) return nested;
            if (child is ScrollableControl { AutoScroll: true } scroller && child is not Form && scroller.VerticalScroll.Visible) return scroller;
        }
        return null;
    }

    private static void SaveShot(Form form, string path)
    {
        try
        {
            using var shot = new Bitmap(Math.Max(1, form.Width), Math.Max(1, form.Height));
            form.DrawToBitmap(shot, new Rectangle(Point.Empty, form.Size));
            shot.Save(path, ImageFormat.Png);
        }
        catch { /* screenshots are a convenience; the checks above are what count */ }
    }

    private static string Safe(string value) => string.Concat(value.Select(ch => char.IsLetterOrDigit(ch) ? char.ToLowerInvariant(ch) : '-')).Trim('-');

    // During the layout check the window may be bigger than this PC's screen (a 4K
    // screen at 200% needs more room than a 1080p one has); let Windows allow that.
    private const int WmGetMinMaxInfo = 0x0024;

    [StructLayout(LayoutKind.Sequential)]
    private struct MinMaxInfo
    {
        public Point Reserved;
        public Point MaxSize;
        public Point MaxPosition;
        public Point MinTrackSize;
        public Point MaxTrackSize;
    }

    protected override void WndProc(ref Message message)
    {
        base.WndProc(ref message);
        if (LayoutCheckMode && message.Msg == WmGetMinMaxInfo)
        {
            var info = Marshal.PtrToStructure<MinMaxInfo>(message.LParam);
            info.MaxTrackSize = new Point(12000, 12000);
            info.MaxSize = new Point(12000, 12000);
            Marshal.StructureToPtr(info, message.LParam, false);
        }
    }
}
