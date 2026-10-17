using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32;
using System.Windows;
using System.Windows.Threading;
using HoverPeek.KafiaNet.Core;
using HoverPeek.KafiaNet.UI;
using WinForms = System.Windows.Forms;
using WpfMessageBox = System.Windows.MessageBox;

namespace HoverPeek.KafiaNet;

public partial class App : System.Windows.Application
{
    private const string AutoStartRunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string AutoStartPreferenceKey = @"Software\KafiaNet\HoverPeek";
    private const string AutoStartValueName = "HoverPeek-KafiaNet";
    private const string AutoStartPreferenceName = "StartWithWindows";

    private static string? GetExecutablePath()
    {
        try
        {
            using var process = System.Diagnostics.Process.GetCurrentProcess();
            return process.MainModule?.FileName;
        }
        catch
        {
            return null;
        }
    }

    private GlobalMouseHook? _mouseHook;
    private HoverDetector? _hoverDetector;
    private ExplorerItemResolver? _resolver;
    private FlipOverlayWindow? _overlay;
    private WinForms.NotifyIcon? _tray;
    private readonly DispatcherTimer _trackTimer = new() { Interval = TimeSpan.FromMilliseconds(120) };
    private bool _trackingBusy;
    private string? _currentPath;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // Register on first launch by default; a saved opt-out is respected.
        var startWithWindows = InitializeAutoStart();

        _resolver = new ExplorerItemResolver();
        _overlay = new FlipOverlayWindow();
        _overlay.Show();
        _overlay.Hide();

        _mouseHook = new GlobalMouseHook();
        _hoverDetector = new HoverDetector(_mouseHook, thresholdMs: 180, jitterPx: 6);
        _hoverDetector.HoverStarted += OnHoverStarted;
        _hoverDetector.HoverEnded += OnHoverEnded;

        _trackTimer.Tick += OnTrackTick;

        _tray = new WinForms.NotifyIcon
        {
            Icon = SystemIcons.Application,
            Visible = true,
            Text = "HoverPeek-KafiaNet"
        };

        var menu = new WinForms.ContextMenuStrip();
        var startupItem = new WinForms.ToolStripMenuItem("Run at Windows sign-in")
        {
            CheckOnClick = true,
            Checked = startWithWindows
        };

        var changingStartupCheck = false;
        startupItem.CheckedChanged += (_, _) =>
        {
            if (changingStartupCheck)
                return;

            var requestedState = startupItem.Checked;
            try
            {
                SetAutoStart(requestedState);
            }
            catch (Exception ex)
            {
                changingStartupCheck = true;
                startupItem.Checked = !requestedState;
                changingStartupCheck = false;

                WpfMessageBox.Show(
                    "Unable to update the Windows sign-in setting.\n\n" + ex.Message,
                    "HoverPeek-KafiaNet",
                    MessageBoxButton.OK,
                    MessageBoxImage.Warning);
            }
        };

        menu.Items.Add(startupItem);
        menu.Items.Add(new WinForms.ToolStripSeparator());

        var exit = new WinForms.ToolStripMenuItem("Exit HoverPeek-KafiaNet");
        exit.Click += (_, _) => Shutdown();
        menu.Items.Add(exit);
        _tray.ContextMenuStrip = menu;

        try
        {
            _mouseHook.Install();
            _trackTimer.Start();
        }
        catch (Exception ex)
        {
            WpfMessageBox.Show(
                "HoverPeek-KafiaNet could not start the mouse hook.\n\n" + ex.Message,
                "HoverPeek-KafiaNet",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
            Shutdown();
        }
    }

    private static bool InitializeAutoStart()
    {
        try
        {
            var executablePath = GetExecutablePath();
            if (string.IsNullOrWhiteSpace(executablePath) || !File.Exists(executablePath))
                return false;

            using var preferences = Registry.CurrentUser.CreateSubKey(AutoStartPreferenceKey, writable: true);
            if (preferences == null)
                return false;

            var savedPreference = preferences.GetValue(AutoStartPreferenceName);
            var enabled = savedPreference == null || Convert.ToInt32(savedPreference) != 0;

            if (savedPreference == null)
                preferences.SetValue(AutoStartPreferenceName, 1, RegistryValueKind.DWord);

            ApplyAutoStartRegistration(executablePath, enabled);
            return enabled;
        }
        catch
        {
            // Keep the preview usable even on systems where registry writes are blocked.
            return false;
        }
    }

    private static void SetAutoStart(bool enabled)
    {
        var executablePath = GetExecutablePath();
        if (string.IsNullOrWhiteSpace(executablePath) || !File.Exists(executablePath))
            throw new InvalidOperationException("The running executable path could not be determined.");

        using var preferences = Registry.CurrentUser.CreateSubKey(AutoStartPreferenceKey, writable: true)
            ?? throw new InvalidOperationException("Unable to open the current user's preference key.");

        var oldValue = preferences.GetValue(AutoStartPreferenceName);
        var oldEnabled = oldValue != null && Convert.ToInt32(oldValue) != 0;

        try
        {
            ApplyAutoStartRegistration(executablePath, enabled);
            preferences.SetValue(AutoStartPreferenceName, enabled ? 1 : 0, RegistryValueKind.DWord);
        }
        catch
        {
            try
            {
                ApplyAutoStartRegistration(executablePath, oldEnabled);
            }
            catch
            {
            }

            throw;
        }
    }

    private static void ApplyAutoStartRegistration(string executablePath, bool enabled)
    {
        using var runKey = Registry.CurrentUser.CreateSubKey(AutoStartRunKey, writable: true)
            ?? throw new InvalidOperationException("Unable to open the Windows sign-in registry key.");

        if (enabled)
        {
            var fullPath = Path.GetFullPath(executablePath);
            var command = string.Concat((char)34, fullPath, (char)34);
            runKey.SetValue(AutoStartValueName, command, RegistryValueKind.String);
        }
        else
        {
            runKey.DeleteValue(AutoStartValueName, throwOnMissingValue: false);
        }
    }

    private async void OnHoverStarted(int x, int y)
    {
        var resolver = _resolver;
        var overlay = _overlay;
        if (resolver == null || overlay == null)
            return;

        var item = await Task.Run(() => resolver.ResolveAtPoint(x, y));
        if (item is not { IsDirectory: true })
            return;

        // Cursor movement inside the same target folder should not restart the flip.
        if (overlay.IsVisible &&
            string.Equals(item.FullPath, _currentPath, StringComparison.OrdinalIgnoreCase))
            return;

        var details = await FlipOverlayWindow.LoadDetailsAsync(item.FullPath);
        if (details == null)
            return;

        await Dispatcher.InvokeAsync(() =>
        {
            _currentPath = item.FullPath;
            overlay.ShowCard(item, details);
        });
    }

    private void OnHoverEnded()
    {
        Dispatcher.BeginInvoke(new Action(() =>
        {
            _currentPath = null;
            _overlay?.HideCard();
        }));
    }

    private async void OnTrackTick(object? sender, EventArgs e)
    {
        if (_overlay is not { IsVisible: true } || _resolver == null || _trackingBusy)
            return;

        _trackingBusy = true;
        try
        {
            if (!GetCursorPos(out var point))
                return;

            var item = await Task.Run(() => _resolver.ResolveAtPoint(point.X, point.Y));

            if (item is not { IsDirectory: true } ||
                !string.Equals(item.FullPath, _currentPath, StringComparison.OrdinalIgnoreCase))
            {
                _overlay.HideCard();
                _currentPath = null;
                return;
            }

            _overlay.UpdateBounds(item.Bounds);
        }
        finally
        {
            _trackingBusy = false;
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _trackTimer.Stop();
        _trackTimer.Tick -= OnTrackTick;

        if (_hoverDetector != null)
        {
            _hoverDetector.HoverStarted -= OnHoverStarted;
            _hoverDetector.HoverEnded -= OnHoverEnded;
            _hoverDetector.Dispose();
        }

        _mouseHook?.Dispose();

        if (_tray != null)
        {
            _tray.Visible = false;
            _tray.Dispose();
        }

        _overlay?.Close();
        base.OnExit(e);
    }

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT point);

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int X;
        public int Y;
    }
}
