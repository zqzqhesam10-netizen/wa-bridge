using System.IO;
using System.Windows.Automation;
namespace HoverPeek.KafiaNet.Core;

public sealed class ExplorerItemResolver
{
    private int _cachedWindowHandle;
    private string? _cachedFolderPath;
    private DateTime _cachedAt;
    private static readonly TimeSpan CacheTtl = TimeSpan.FromSeconds(2);

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

            var fullPath = Path.Combine(folderPath, name);
            var isDirectory = Directory.Exists(fullPath);
            if (!isDirectory && !File.Exists(fullPath))
                return null;

            var bounds = item.Current.BoundingRectangle;
            if (bounds.IsEmpty || bounds.Width < 2 || bounds.Height < 2)
                return null;

            return new ExplorerItemInfo(
                fullPath,
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
