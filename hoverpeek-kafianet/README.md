# HoverPeek-KafiaNet

Lightweight Windows Explorer hover overlay for KafiaNet.

Current behavior:
- Offline/local only. No TMDB, no network lookup, no telemetry.
- Only reacts to directories in Windows File Explorer.
- The back face is loaded only from a file named details.png located directly inside the hovered folder.
- No fixed card size: the overlay reads the hovered Explorer item's UI Automation bounding rectangle on each detection and while tracking.
- Native SetWindowPos uses screen pixels, with per-monitor DPI handling.
- The overlay is mouse-transparent and does not steal Explorer focus.
- The flip uses WPF Viewport3D and rotates the card 180 degrees around the Y axis.
- No video engine, WebView2, archive engine, image-processing package, or external NuGet dependency is included.

Source / attribution:
- Hover detection and Explorer UI Automation approach are derived from:
  https://github.com/kyosora/HoverPeek
- Original project license: MIT. The original copyright and permission notice is preserved in this directory.

KafiaNet modifications:
- Reduced to folder-only detection.
- Added exact Explorer-item bounding rectangle tracking.
- Added details.png sidecar image loading.
- Added a mouse-transparent 3D flip overlay.
- Removed heavyweight preview providers and third-party package dependencies.
