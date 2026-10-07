namespace HoverPeek.KafiaNet.Core;

public sealed class HoverDetector : IDisposable
{
    private readonly GlobalMouseHook _mouseHook;
    private CancellationTokenSource? _cts;
    private readonly object _gate = new();

    private readonly int _thresholdMs;
    private readonly int _jitterPx;
    private int _anchorX;
    private int _anchorY;
    private bool _haveAnchor;
    private bool _hoverFired;
    private bool _disposed;

    public event Action<int, int>? HoverStarted;
    public event Action? HoverEnded;

    public HoverDetector(GlobalMouseHook mouseHook, int thresholdMs = 350, int jitterPx = 6)
    {
        _mouseHook = mouseHook ?? throw new ArgumentNullException(nameof(mouseHook));
        _thresholdMs = thresholdMs;
        _jitterPx = jitterPx;
        _mouseHook.MouseMoved += OnMouseMoved;
    }

    private void OnMouseMoved(int x, int y)
    {
        lock (_gate)
        {
            if (!_haveAnchor)
            {
                _anchorX = x;
                _anchorY = y;
                _haveAnchor = true;
                _cts = new CancellationTokenSource();
                _ = FireAfterDelay(x, y, _cts.Token);
                return;
            }

            var dx = Math.Abs(x - _anchorX);
            var dy = Math.Abs(y - _anchorY);

            if (dx <= _jitterPx && dy <= _jitterPx)
                return;

            _anchorX = x;
            _anchorY = y;

            var wasHovering = _hoverFired;
            _hoverFired = false;

            _cts?.Cancel();
            _cts?.Dispose();
            _cts = new CancellationTokenSource();
            var token = _cts.Token;

            _ = FireAfterDelay(x, y, token);

            if (wasHovering)
                HoverEnded?.Invoke();
        }
    }

    private async Task FireAfterDelay(int x, int y, CancellationToken token)
    {
        try
        {
            await Task.Delay(_thresholdMs, token).ConfigureAwait(false);
            if (token.IsCancellationRequested)
                return;

            lock (_gate)
            {
                if (token.IsCancellationRequested)
                    return;
                _hoverFired = true;
            }

            HoverStarted?.Invoke(x, y);
        }
        catch (OperationCanceledException)
        {
        }
    }

    public void Dispose()
    {
        if (_disposed)
            return;

        _mouseHook.MouseMoved -= OnMouseMoved;
        lock (_gate)
        {
            _cts?.Cancel();
            _cts?.Dispose();
            _cts = null;
        }

        _disposed = true;
    }
}
