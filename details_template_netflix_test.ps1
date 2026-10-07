param(
    [string]$Template = (Join-Path $PSScriptRoot "template.png"),
    [string]$Output = (Join-Path $PSScriptRoot "details_test.png")
)

Add-Type -AssemblyName System.Drawing

if (-not (Test-Path -LiteralPath $Template)) {
    throw "template.png not found: $Template"
}

$src = [System.Drawing.Bitmap]::new($Template)
$dst = [System.Drawing.Bitmap]::new(256, 256, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)

try {
    $g = [System.Drawing.Graphics]::FromImage($dst)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)

    # Use the original 1883 PNG ONLY as the folder silhouette/mask.
    # Its artwork is never copied into the new front face.
    $mask = [System.Drawing.Bitmap]::new(256, 256, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    try {
        $mg = [System.Drawing.Graphics]::FromImage($mask)
        $mg.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $mg.DrawImage($src, 0, 0, 256, 256)
        $mg.Dispose()

        # Build a modern black -> charcoal -> restrained Netflix-red atmosphere.
        $rect = [System.Drawing.Rectangle]::new(0, 0, 256, 256)
        $grad = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
            $rect,
            [System.Drawing.Color]::FromArgb(255, 8, 8, 10),
            [System.Drawing.Color]::FromArgb(255, 55, 6, 12),
            35
        )

        # Derive transparency from the reference silhouette while painting a new surface.
        $pixels = $mask.LockBits(
            $rect,
            [System.Drawing.Imaging.ImageLockMode]::ReadOnly,
            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        )

        $bytes = New-Object byte[] ($pixels.Stride * $pixels.Height)
        [Runtime.InteropServices.Marshal]::Copy($pixels.Scan0, $bytes, 0, $bytes.Length)
        $mask.UnlockBits($pixels)

        $layer = [System.Drawing.Bitmap]::new(256, 256, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        try {
            $lg = [System.Drawing.Graphics]::FromImage($layer)
            $lg.FillRectangle($grad, $rect)
            $lg.Dispose()

            $lp = $layer.LockBits(
                $rect,
                [System.Drawing.Imaging.ImageLockMode]::ReadWrite,
                [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
            )

            $stride = $lp.Stride
            $outBytes = New-Object byte[] ($stride * 256)
            [Runtime.InteropServices.Marshal]::Copy($lp.Scan0, $outBytes, 0, $outBytes.Length)

            # Replace layer alpha with reference silhouette alpha.
            for ($y = 0; $y -lt 256; $y++) {
                for ($x = 0; $x -lt 256; $x++) {
                    $i = $y * $stride + ($x * 4)
                    $mi = $y * $mask.Width * 4 + ($x * 4)
                    $outBytes[$i + 3] = $bytes[$mi + 3]
                }
            }

            [Runtime.InteropServices.Marshal]::Copy($outBytes, 0, $lp.Scan0, $outBytes.Length)
            $layer.UnlockBits($lp)

            # Add subtle red cinematic light to the layer before drawing it.
            # Because the layer already carries the template alpha mask,
            # the light cannot leak outside the folder silhouette.
            $lg2 = [System.Drawing.Graphics]::FromImage($layer)
            $glow = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
                [System.Drawing.Rectangle]::new(20, 15, 215, 120),
                [System.Drawing.Color]::FromArgb(80, 229, 9, 20),
                [System.Drawing.Color]::FromArgb(0, 229, 9, 20),
                90
            )
            $lg2.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceOver
            $lg2.FillRectangle($glow, 20, 15, 215, 120)
            $glow.Dispose()
            $lg2.Dispose()

            $g.DrawImageUnscaled($layer, 0, 0)
        }
        finally {
            $layer.Dispose()
        }

        $grad.Dispose()

        # Test typography only; real TMDB fields come later.
        $titleFont = [System.Drawing.Font]::new("Arial", 19, [System.Drawing.FontStyle]::Bold)
        $infoFont  = [System.Drawing.Font]::new("Arial", 11, [System.Drawing.FontStyle]::Regular)
        $smallFont = [System.Drawing.Font]::new("Arial", 9, [System.Drawing.FontStyle]::Regular)

        $white = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::White)
        $gray  = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(215, 205, 205, 205))
        $red   = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(255, 229, 9, 20))

        $g.DrawString("TEST MOVIE", $titleFont, $white, 36, 112)
        $g.DrawString("2026", $infoFont, $gray, 36, 148)
        $g.DrawString("ACTION • SCI-FI", $infoFont, $gray, 36, 169)
        $g.DrawString("UNITED STATES", $infoFont, $gray, 36, 190)
        $g.DrawString("★ 7.5", $infoFont, $red, 36, 214)
        $g.DrawString("TMDB", $smallFont, $gray, 202, 225)

        $white.Dispose()
        $gray.Dispose()
        $red.Dispose()
        $titleFont.Dispose()
        $infoFont.Dispose()
        $smallFont.Dispose()

        $g.Dispose()

        $dst.Save($Output, [System.Drawing.Imaging.ImageFormat]::Png)
        Write-Host "Created: $Output"
        Write-Host "Size: 256x256"
        Write-Host "Reference: template.png used only as silhouette/mask."
    }
    finally {
        $mask.Dispose()
    }
}
finally {
    $src.Dispose()
    $dst.Dispose()
}
