using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Media.Media3D;
using System.Windows.Interop;

namespace HoverPeek.KafiaNet.UI;

public partial class FlipOverlayWindow : Window
{
    private const int SWP_NOACTIVATE = 0x0010;
    private const int SWP_NOOWNERZORDER = 0x0200;
    private const int SWP_SHOWWINDOW = 0x0040;
    private static readonly IntPtr HWND_TOPMOST = new(-1);

    private readonly AxisAngleRotation3D _rotation = new(new Vector3D(0, 1, 0), 0);
    private readonly RotateTransform3D _rotateTransform;
    private BitmapImage? _detailsImage;

    public FlipOverlayWindow()
    {
        InitializeComponent();
        _rotateTransform = new RotateTransform3D(_rotation);
        Loaded += (_, _) => ApplyTransparentNoActivateStyles();
        SizeChanged += (_, _) => UpdateCamera();
    }

    public void ShowCard(ExplorerItemInfo item, BitmapImage detailsImage)
    {
        _detailsImage = detailsImage;
        BuildCard(detailsImage, GetAspect(item.Bounds));

        Show();
        UpdateNativeBounds(item.Bounds);

        _rotation.BeginAnimation(
            AxisAngleRotation3D.AngleProperty,
            new DoubleAnimation
            {
                From = 0,
                To = 180,
                Duration = TimeSpan.FromMilliseconds(480),
                EasingFunction = new CubicEase { EasingMode = EasingMode.EaseInOut },
                FillBehavior = FillBehavior.HoldEnd
            },
            HandoffBehavior.SnapshotAndReplace);
    }

    public void UpdateBounds(Rect bounds)
    {
        if (!IsVisible)
            return;

        UpdateNativeBounds(bounds);

        if (_detailsImage != null)
            RebuildGeometry(GetAspect(bounds));
    }

    public void HideCard()
    {
        _rotation.BeginAnimation(AxisAngleRotation3D.AngleProperty, null);
        _rotation.Angle = 0;
        Hide();
    }

    private void BuildCard(BitmapImage detailsImage, double aspect)
    {
        _rotation.BeginAnimation(AxisAngleRotation3D.AngleProperty, null);
        _rotation.Angle = 0;

        var halfHeight = 1.0;
        var halfWidth = halfHeight * aspect;
        var mesh = CreateMesh(halfWidth, halfHeight);

        var frontBrush = new SolidColorBrush(Color.FromArgb(245, 16, 20, 24));
        frontBrush.Freeze();

        var imageBrush = new ImageBrush(detailsImage)
        {
            Stretch = Stretch.UniformToFill,
            AlignmentX = AlignmentX.Center,
            AlignmentY = AlignmentY.Center
        };
        imageBrush.Freeze();

        var model = new GeometryModel3D
        {
            Geometry = mesh,
            Material = new DiffuseMaterial(frontBrush),
            BackMaterial = new DiffuseMaterial(imageBrush),
            Transform = _rotateTransform
        };

        CardViewport.Children.Clear();
        CardViewport.Children.Add(new ModelVisual3D { Content = model });
        CardViewport.Children.Add(new ModelVisual3D
        {
            Content = new AmbientLight(Colors.White)
        });

        UpdateCamera();
    }

    private void RebuildGeometry(double aspect)
    {
        if (CardViewport.Children.Count == 0 ||
            CardViewport.Children[0] is not ModelVisual3D visual ||
            visual.Content is not GeometryModel3D model)
        {
            if (_detailsImage != null)
                BuildCard(_detailsImage, aspect);
            return;
        }

        model.Geometry = CreateMesh(aspect, 1.0);
        UpdateCamera();
    }

    private static MeshGeometry3D CreateMesh(double halfWidth, double halfHeight)
    {
        return new MeshGeometry3D
        {
            Positions = new Point3DCollection
            {
                new(-halfWidth,  halfHeight, 0),
                new( halfWidth,  halfHeight, 0),
                new( halfWidth, -halfHeight, 0),
                new(-halfWidth, -halfHeight, 0)
            },
            TextureCoordinates = new PointCollection
            {
                new(0, 0), new(1, 0), new(1, 1), new(0, 1)
            },
            TriangleIndices = new Int32Collection { 0, 1, 2, 0, 2, 3 }
        };
    }

    private void UpdateCamera()
    {
        if (!IsLoaded || ActualWidth <= 1 || ActualHeight <= 1)
            return;

        var aspect = Math.Max(0.2, ActualWidth / ActualHeight);
        CardViewport.Camera = new OrthographicCamera
        {
            Position = new Point3D(0, 0, 10),
            LookDirection = new Vector3D(0, 0, -10),
            UpDirection = new Vector3D(0, 1, 0),
            Width = aspect * 2.0
        };
    }

    private void UpdateNativeBounds(Rect bounds)
    {
        if (!IsLoaded)
            return;

        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd == IntPtr.Zero)
            return;

        var x = (int)Math.Round(bounds.X);
        var y = (int)Math.Round(bounds.Y);
        var width = Math.Max(2, (int)Math.Round(bounds.Width));
        var height = Math.Max(2, (int)Math.Round(bounds.Height));

        SetWindowPos(hwnd, HWND_TOPMOST, x, y, width, height,
            SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_SHOWWINDOW);

        var dpi = GetDpiForWindow(hwnd);
        if (dpi > 0)
        {
            Width = width * 96.0 / dpi;
            Height = height * 96.0 / dpi;
        }

        SetWindowPos(hwnd, HWND_TOPMOST, x, y, width, height,
            SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_SHOWWINDOW);
    }

    private void ApplyTransparentNoActivateStyles()
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd == IntPtr.Zero)
            return;

        var current = GetWindowLongPtr(hwnd, GWL_EXSTYLE).ToInt64();
        var updated = current |
                      WS_EX_NOACTIVATE |
                      WS_EX_TOOLWINDOW |
                      WS_EX_TRANSPARENT |
                      WS_EX_LAYERED;
        SetWindowLongPtr(hwnd, GWL_EXSTYLE, new IntPtr(updated));
    }

    private static double GetAspect(Rect bounds)
    {
        return Math.Max(0.2, bounds.Width / Math.Max(1.0, bounds.Height));
    }

    public static Task<BitmapImage?> LoadDetailsAsync(string folderPath)
    {
        var path = Path.Combine(folderPath, "details.png");
        if (!File.Exists(path))
            return Task.FromResult<BitmapImage?>(null);

        return Task.Run(() =>
        {
            try
            {
                using var stream = new FileStream(
                    path,
                    FileMode.Open,
                    FileAccess.Read,
                    FileShare.ReadWrite | FileShare.Delete);

                var bitmap = new BitmapImage();
                bitmap.BeginInit();
                bitmap.CacheOption = BitmapCacheOption.OnLoad;
                bitmap.StreamSource = stream;
                bitmap.EndInit();
                bitmap.Freeze();
                return bitmap;
            }
            catch
            {
                return null;
            }
        });
    }

    private const int GWL_EXSTYLE = -20;
    private const int WS_EX_TRANSPARENT = 0x20;
    private const int WS_EX_NOACTIVATE = 0x08000000;
    private const int WS_EX_TOOLWINDOW = 0x00000080;
    private const int WS_EX_LAYERED = 0x00080000;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetWindowPos(
        IntPtr hWnd, IntPtr hWndInsertAfter,
        int X, int Y, int cx, int cy, uint uFlags);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetDpiForWindow(IntPtr hWnd);

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW", SetLastError = true)]
    private static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW", SetLastError = true)]
    private static extern IntPtr SetWindowLongPtr(IntPtr hWnd, int nIndex, IntPtr dwNewLong);
}
