using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Automation;

namespace HoverPeek.KafiaNet.Core;

public sealed class ExplorerItemResolver
{
    private int _cachedWindowHandle;
    private string? _cachedFolderPath;
    private DateTime _cachedAt;

    // Explorer may navigate within the same HWND. Keep this cache short to avoid stale parent paths.
    private static readonly TimeSpan CacheTtl = TimeSpan.FromMilliseconds(300);

    public ExplorerItemInfo? ResolveAtPoint(int screenX, int screenY)
    {
        try
        {
            var point = new System.Windows.Point(screenX, screenY);
            var element = AutomationElement.FromPoint(point);
            if (element == null)
                return null;

            var item = FindExplorerItem(element);
            if (item == null)
                return null;

            var name = item.Current.Name;
            if (string.IsNullOrWhiteSpace(name))
                return null;

            var folderPath = GetCurrentExplorerFolderCached(item);
            if (string.IsNullOrWhiteSpace(folderPath))
                return null;

            // With "file name extensions" hidden, UI Automation can return "Folder"
            // even though the real filesystem entry is "Folder.lnk".
            var displayedPath = FindDisplayedPath(folderPath, name);
            if (displayedPath == null)
                return null;

            var resolvedPath = displayedPath;
            var isDirectory = Directory.Exists(displayedPath);

            // Resolve filesystem folder shortcuts to their target for the details.png lookup.
            // Bounds remain those of the visible .lnk item in Explorer.
            if (!isDirectory &&
                string.Equals(Path.GetExtension(displayedPath), ".lnk", StringComparison.OrdinalIgnoreCase))
            {
                var target = TryResolveDirectoryShortcut(displayedPath);
                if (!string.IsNullOrWhiteSpace(target))
                {
                    resolvedPath = target;
                    isDirectory = true;
                }
            }

            var bounds = item.Current.BoundingRectangle;
            if (bounds.IsEmpty || bounds.Width < 2 || bounds.Height < 2)
                return null;

            return new ExplorerItemInfo(
                resolvedPath,
                new System.Windows.Rect(bounds.X, bounds.Y, bounds.Width, bounds.Height),
                isDirectory);
        }
        catch (ElementNotAvailableException)
        {
            return null;
        }
        catch
        {
            return null;
        }
    }

    private static string? FindDisplayedPath(string parentPath, string displayName)
    {
        try
        {
            var directPath = Path.Combine(parentPath, displayName);
            if (Directory.Exists(directPath) || File.Exists(directPath))
                return directPath;

            if (!displayName.EndsWith(".lnk", StringComparison.OrdinalIgnoreCase))
            {
                var shortcutPath = Path.Combine(parentPath, displayName + ".lnk");
                if (File.Exists(shortcutPath))
                    return shortcutPath;
            }
        }
        catch
        {
        }

        return null;
    }

    private static string? TryResolveDirectoryShortcut(string shortcutPath)
    {
        object? shell = null;
        object? shortcut = null;

        try
        {
            var shellType = Type.GetTypeFromProgID("WScript.Shell");
            if (shellType == null)
                return null;

            shell = Activator.CreateInstance(shellType);
            if (shell == null)
                return null;

            dynamic shellObject = shell;
            shortcut = shellObject.CreateShortcut(shortcutPath);
            if (shortcut == null)
                return null;

            dynamic shortcutObject = shortcut;
            object? rawTarget = shortcutObject.TargetPath;
            var targetPath = rawTarget as string;
            if (string.IsNullOrWhiteSpace(targetPath))
                return null;

            targetPath = Environment.ExpandEnvironmentVariables(targetPath.Trim().Trim('"'));

            if (!Path.IsPathRooted(targetPath))
            {
                var shortcutDirectory = Path.GetDirectoryName(shortcutPath);
                if (string.IsNullOrWhiteSpace(shortcutDirectory))
                    return null;

                targetPath = Path.GetFullPath(targetPath, shortcutDirectory);
            }
            else
            {
                targetPath = Path.GetFullPath(targetPath);
            }

            return Directory.Exists(targetPath) ? targetPath : null;
        }
        catch
        {
            return null;
        }
        finally
        {
            if (shortcut != null && Marshal.IsComObject(shortcut))
                Marshal.FinalReleaseComObject(shortcut);

            if (shell != null && Marshal.IsComObject(shell))
                Marshal.FinalReleaseComObject(shell);
        }
    }

    private string? GetCurrentExplorerFolderCached(AutomationElement item)
    {
        var walker = TreeWalker.ControlViewWalker;
        var current = item;

        while (current != null &&
               current.Current.ClassName != "CabinetWClass" &&
               current.Current.ClassName != "ExploreWClass")
        {
            current = walker.GetParent(current);
        }

        if (current == null)
            return null;

        var hwnd = current.Current.NativeWindowHandle;
        if (hwnd == _cachedWindowHandle &&
            _cachedFolderPath != null &&
            DateTime.UtcNow - _cachedAt < CacheTtl)
        {
            return _cachedFolderPath;
        }

        var path = GetFolderPathViaCom(current);
        if (string.IsNullOrWhiteSpace(path))
            path = GetFolderPathFromAddressBar(current);

        if (!string.IsNullOrWhiteSpace(path))
        {
            _cachedWindowHandle = hwnd;
            _cachedFolderPath = path;
            _cachedAt = DateTime.UtcNow;
        }

        return path;
    }

    private static AutomationElement? FindExplorerItem(AutomationElement element)
    {
        if (IsExplorerItem(element))
            return element;

        var walker = TreeWalker.ControlViewWalker;
        var current = element;

        for (var i = 0; i < 6 && current != null; i++)
        {
            current = walker.GetParent(current);
            if (current != null && IsExplorerItem(current))
                return current;
        }

        return null;
    }

    private static bool IsExplorerItem(AutomationElement element)
    {
        var type = element.Current.ControlType;
        if (type != ControlType.ListItem &&
            type != ControlType.DataItem &&
            type != ControlType.TreeItem &&
            type != ControlType.Text &&
            type != ControlType.Image &&
            type != ControlType.Group &&
            type != ControlType.Custom)
        {
            return false;
        }

        var walker = TreeWalker.ControlViewWalker;
        var current = element;

        for (var depth = 0; current != null && depth < 10; depth++)
        {
            var className = current.Current.ClassName;

            if (className == "CabinetWClass" || className == "ExploreWClass")
                return true;

            if (className == "UIItemsView" ||
                className == "DirectUIHWND" ||
                className == "ShellView" ||
                className == "SHELLDLL_DefView")
            {
                return true;
            }

            current = walker.GetParent(current);
        }

        return false;
    }

    private static string? GetFolderPathFromAddressBar(AutomationElement explorerWindow)
    {
        try
        {
            var addressBar = explorerWindow.FindFirst(
                TreeScope.Descendants,
                new PropertyCondition(
                    AutomationElement.AutomationIdProperty,
                    "1001"));

            if (addressBar == null)
                return null;

            if (addressBar.TryGetCurrentPattern(ValuePattern.Pattern, out var pattern))
            {
                var value = ((ValuePattern)pattern).Current.Value;
                if (!string.IsNullOrWhiteSpace(value))
                    return value;
            }
        }
        catch
        {
        }

        return null;
    }

    private static string? GetFolderPathViaCom(AutomationElement explorerWindow)
    {
        try
        {
            var shellType = Type.GetTypeFromProgID("Shell.Application");
            if (shellType == null)
                return null;

            dynamic? shell = Activator.CreateInstance(shellType);
            if (shell == null)
                return null;

            var windows = shell.Windows();
            var hwnd = explorerWindow.Current.NativeWindowHandle;

            for (var i = 0; i < windows.Count; i++)
            {
                dynamic? window = windows.Item(i);
                if (window == null)
                    continue;

                try
                {
                    if ((int)window.HWND != hwnd)
                        continue;

                    var locationUrl = window.LocationURL as string;
                    if (!string.IsNullOrWhiteSpace(locationUrl) &&
                        locationUrl.StartsWith("file:///", StringComparison.OrdinalIgnoreCase))
                    {
                        return new Uri(locationUrl).LocalPath;
                    }

                    var path = window.Document?.Folder?.Self?.Path as string;
                    if (!string.IsNullOrWhiteSpace(path))
                        return path;
                }
                catch
                {
                }
            }
        }
        catch
        {
        }

        return null;
    }
}

public sealed record ExplorerItemInfo(
    string FullPath,
    System.Windows.Rect Bounds,
    bool IsDirectory);
