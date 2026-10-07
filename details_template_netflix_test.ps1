param(
    [string]$Template = (Join-Path $PSScriptRoot "template.png"),
    [string]$Output = (Join-Path $PSScriptRoot "details_test.png")
)

Add-Type -AssemblyName System.Drawing

if (-not (Test-Path -LiteralPath $Template)) {
    throw "template.png not found: $Template"
}

$src = [System.Drawing.Bitmap]::new($Template)

try {
    # Work from the original icon itself so its folder silhouette,
    # 3D depth, bevels and shadow are preserved.
    $small = [System.Drawing.Bitmap]::new(48, 48, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $shade = [System.Drawing.Bitmap]::new(256, 256, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $out = [System.Drawing.Bitmap]::new(256, 256, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)

    try {
        $gs = [System.Drawing.Graphics]::FromImage($small)
        $gs.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBilinear
        $gs.DrawImage($src, 0, 0, 48, 48)
        $gs.Dispose()

        $gsh = [System.Drawing.Graphics]::FromImage($shade)
        $gsh.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $gsh.DrawImage($small, 0, 0, 256, 256)
        $gsh.Dispose()

        $srcRect = [System.Drawing.Rectangle]::new(0, 0, $src.Width, $src.Height)
        $dstRect = [System.Drawing.Rectangle]::new(0, 0, 256, 256)

        $shadeData = $shade.LockBits(
            $dstRect,
            [System.Drawing.Imaging.ImageLockMode]::ReadOnly,
            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        )
        $srcData = $src.LockBits(
            $srcRect,
            [System.Drawing.Imaging.ImageLockMode]::ReadOnly,
            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        )
        $outData = $out.LockBits(
            $dstRect,
            [System.Drawing.Imaging.ImageLockMode]::WriteOnly,
            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        )

        try {
            $shadeBytes = New-Object byte[] ($shadeData.Stride * $shade.Height)
            [Runtime.InteropServices.Marshal]::Copy(
                $shadeData.Scan0,
                $shadeBytes,
                0,
                $shadeBytes.Length
            )

            $srcBytes = New-Object byte[] ($srcData.Stride * $src.Height)
            [Runtime.InteropServices.Marshal]::Copy(
                $srcData.Scan0,
                $srcBytes,
                0,
                $srcBytes.Length
            )

            $outBytes = New-Object byte[] ($outData.Stride * $out.Height)

            for ($y = 0; $y -lt 256; $y++) {
                $srcY = [Math]::Min(
                    $src.Height - 1,
                    [int](($y / 255.0) * ($src.Height - 1))
                )

                for ($x = 0; $x -lt 256; $x++) {
                    $shadeIndex = $y * $shadeData.Stride + ($x * 4)
                    $srcX = [Math]::Min(
                        $src.Width - 1,
                        [int](($x / 255.0) * ($src.Width - 1))
                    )
                    $srcIndex = $srcY * $srcData.Stride + ($srcX * 4)
                    $outIndex = $y * $outData.Stride + ($x * 4)

                    # Coarse luminance keeps the original 3D lighting and
                    # silhouette, but suppresses the 1883 artwork/details.
                    $b0 = $shadeBytes[$shadeIndex]
                    $g0 = $shadeBytes[$shadeIndex + 1]
                    $r0 = $shadeBytes[$shadeIndex + 2]

                    $lum = (0.2126 * $r0) + (0.7152 * $g0) + (0.0722 * $b0)
                    $v = $lum / 255.0

                    # Netflix-like black/charcoal base with restrained red.
                    $redAccent = (1.0 - ($x / 255.0)) * 0.20 + ($y / 255.0) * 0.08

                    $r = [Math]::Min(255, [int](7 + ($v * 38) + ($redAccent * 95)))
                    $g = [Math]::Min(255, [int](8 + ($v * 30)))
                    $b = [Math]::Min(255, [int](10 + ($v * 34)))

                    # Preserve the exact original folder transparency/shadow.
                    $a = $srcBytes[$srcIndex + 3]

                    $outBytes[$outIndex] = $b
                    $outBytes[$outIndex + 1] = $g
                    $outBytes[$outIndex + 2] = $r
                    $outBytes[$outIndex + 3] = $a
                }
            }

            [Runtime.InteropServices.Marshal]::Copy(
                $outBytes,
                0,
                $outData.Scan0,
                $outBytes.Length
            )
        }
        finally {
            $shade.UnlockBits($shadeData)
            $src.UnlockBits($srcData)
            $out.UnlockBits($outData)
        }

        # Test text only. The real TMDB data comes later.
        $g = [System.Drawing.Graphics]::FromImage($out)
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
        $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit

        $titleFont = [System.Drawing.Font]::new(
            "Arial",
            19,
            [System.Drawing.FontStyle]::Bold
        )
        $infoFont = [System.Drawing.Font]::new(
            "Arial",
            11,
            [System.Drawing.FontStyle]::Regular
        )

        $shadowBrush = [System.Drawing.SolidBrush]::new(
            [System.Drawing.Color]::FromArgb(180, 0, 0, 0)
        )
        $whiteBrush = [System.Drawing.SolidBrush]::new(
            [System.Drawing.Color]::White
        )
        $redBrush = [System.Drawing.SolidBrush]::new(
            [System.Drawing.Color]::FromArgb(255, 229, 9, 20)
        )

        $g.DrawString("TEST MOVIE", $titleFont, $shadowBrush, 34, 112)
        $g.DrawString("TEST MOVIE", $titleFont, $whiteBrush, 32, 110)

        $g.DrawString("2026  •  ACTION / SCI-FI", $infoFont, $whiteBrush, 34, 150)
        $g.DrawString("UNITED STATES", $infoFont, $whiteBrush, 34, 171)

        $g.DrawString("★ 7.5", $infoFont, $redBrush, 34, 198)

        $shadowBrush.Dispose()
        $whiteBrush.Dispose()
        $redBrush.Dispose()
        $titleFont.Dispose()
        $infoFont.Dispose()
        $g.Dispose()

        $out.Save(
            $Output,
            [System.Drawing.Imaging.ImageFormat]::Png
        )

        Write-Host "Created: $Output"
        Write-Host "Size: 256x256"
        Write-Host "Template geometry/shadow preserved; 1883 artwork suppressed."
    }
    finally {
        $small.Dispose()
        $shade.Dispose()
        $out.Dispose()
    }
}
finally {
    $src.Dispose()
}
