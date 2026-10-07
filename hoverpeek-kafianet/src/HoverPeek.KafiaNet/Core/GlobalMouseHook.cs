using System.Runtime.InteropServices;

namespace HoverPeek.KafiaNet.Core;

public sealed class GlobalMouseHook : IDisposable
{
    private const int WH_MOUSE_LL = 14;
    private const int WM_MOUSEMOVE = 0x0200;

    private readonly LowLevelMouseProc _proc;
    private IntPtr _hookId;
    private bool _disposed;

    public event Action<int, int>? MouseMoved;

    public GlobalMouseHook()
    {
        _proc = HookCallback;
    }

    public void Install()
    {
        if (_hookId != IntPtr.Zero)
            throw new InvalidOperationException("Mouse hook is already installed.");

        using var process = System.Diagnostics.Process.GetCurrentProcess();
        using var module = process.MainModule;
        var moduleName = module?.ModuleName;

        _hookId = SetWindowsHookEx(
            WH_MOUSE_LL,
            _proc,
            GetModuleHandle(moduleName),
            0);

        if (_hookId == IntPtr.Zero)
            throw new InvalidOperationException(
                $"Failed to install mouse hook. Win32 error: {Marshal.GetLastWin32Error()}");
    }

    private IntPtr HookCallback(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0 && wParam == (IntPtr)WM_MOUSEMOVE)
        {
            var hook = Marshal.PtrToStructure<MSLLHOOKSTRUCT>(lParam);
            MouseMoved?.Invoke(hook.Point.X, hook.Point.Y);
        }

        return CallNextHookEx(_hookId, nCode, wParam, lParam);
    }

    public void Dispose()
    {
        if (_disposed)
            return;

        if (_hookId != IntPtr.Zero)
        {
            UnhookWindowsHookEx(_hookId);
            _hookId = IntPtr.Zero;
        }

        _disposed = true;
    }

    private delegate IntPtr LowLevelMouseProc(int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(
        int idHook,
        LowLevelMouseProc lpfn,
        IntPtr hMod,
        uint dwThreadId);

    [DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern bool UnhookWindowsHookEx(IntPtr hHook);

    [DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern IntPtr CallNextHookEx(
        IntPtr hHook,
        int nCode,
        IntPtr wParam,
        IntPtr lParam);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr GetModuleHandle(string? moduleName);

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int X;
        public int Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MSLLHOOKSTRUCT
    {
        public POINT Point;
        public uint MouseData;
        public uint Flags;
        public uint Time;
        public IntPtr ExtraInfo;
    }
}
