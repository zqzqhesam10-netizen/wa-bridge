using System.Drawing;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Forms;
using System.Windows.Threading;
using HoverPeek.KafiaNet.Core;
using HoverPeek.KafiaNet.UI;

namespace HoverPeek.KafiaNet;

public partial class App : System.Windows.Application
{
    private GlobalMouseHook? _mouseHook;
    private HoverDetector? _hoverDetector;
    private ExplorerItemResolver? _resolver;
    private FlipOverlayWindow? _overlay;
    private NotifyIcon? _tray;
    private readonly DispatcherTimer _trackTimer = new() { Interval = TimeSpan.FromMilliseconds(120) };
    private bool _trackingBusy;
    private string? _currentPath;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        _resolver = new ExplorerItemResolver();
        _overlay = new FlipOverlayWindow();
        _overlay.Show();
        _overlay.Hide();

        _mouseHook = new GlobalMouseHook();
        _hoverDetector = new HoverDetector(_mouseHook, thresholdMs: 350, jitterPx: 6);
        _hoverDetector.HoverStarted += OnHoverStarted;
        _hoverDetector.HoverEnded += OnHoverEnded;

        _trackTimer.Tick += OnTrackTick;

        _tray = new NotifyIcon
        {
            Icon = SystemIcons.Application,
            Visible = true,
            Text = "HoverPeek-KafiaNet"
        };

        var menu = new ContextMenuStrip();
        var exit = new ToolStripMenuItem("Exit HoverPeek-KafiaNet");
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
            MessageBox.Show(
                "HoverPeek-KafiaNet could not start the mouse hook.\n\n" + ex.Message,
                "HoverPeek-KafiaNet",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
            Shutdown();
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
