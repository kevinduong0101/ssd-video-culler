#!/bin/zsh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Video Culler"
DEST_DIR="${1:-$HOME/Desktop}"
APP_BUNDLE="$DEST_DIR/$APP_NAME.app"
CONTENTS="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RES_DIR="$CONTENTS/Resources"

rm -rf "$APP_BUNDLE"
mkdir -p "$MACOS_DIR" "$RES_DIR/app"

# 1. Compile native Apple Silicon / macOS Mach-O binary
/usr/bin/swiftc \
  -O \
  -framework Cocoa \
  -framework WebKit \
  "$REPO_DIR/macos/VideoCuller.swift" \
  -o "$MACOS_DIR/VideoCuller"

# 2. Bundle fallback copy of culler.py + index.html inside Resources/app
cp "$REPO_DIR/culler.py" "$RES_DIR/app/culler.py"
cp "$REPO_DIR/index.html" "$RES_DIR/app/index.html"
chmod +x "$RES_DIR/app/culler.py"

# 3. Generate sleek darkroom emerald AppIcon.icns via CoreGraphics Swift snippet + iconutil
ICON_TMP="$(mktemp -d)"
ICONSET="$ICON_TMP/AppIcon.iconset"
mkdir -p "$ICONSET"

/usr/bin/swift - "$ICONSET" <<'SWIFT'
import Cocoa
let iconset = CommandLine.arguments[1]
let sizes = [16, 32, 64, 128, 256, 512, 1024]

func renderIcon(px: Int) -> Data {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: px,
        pixelsHigh: px,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    )!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = CGFloat(px)
    let inset = s * 0.08
    let rect = NSRect(x: inset, y: inset, width: s - inset * 2, height: s - inset * 2)
    let radius = s * 0.225

    // Dark obsidian squircle base
    let bgPath = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
    let grad = NSGradient(starting: NSColor(calibratedRed: 0.11, green: 0.13, blue: 0.16, alpha: 1),
                          ending: NSColor(calibratedRed: 0.05, green: 0.06, blue: 0.08, alpha: 1))!
    grad.draw(in: bgPath, angle: -90)

    // Subtle emerald inner rim
    NSColor(calibratedRed: 0.06, green: 0.72, blue: 0.50, alpha: 0.45).setStroke()
    bgPath.lineWidth = max(1, s * 0.022)
    bgPath.stroke()

    // Crisp emerald play triangle at center
    let tri = NSBezierPath()
    let cx = s * 0.52
    let cy = s * 0.50
    let r = s * 0.22
    tri.move(to: NSPoint(x: cx - r * 0.65, y: cy + r * 0.85))
    tri.line(to: NSPoint(x: cx - r * 0.65, y: cy - r * 0.85))
    tri.line(to: NSPoint(x: cx + r * 0.95, y: cy))
    tri.close()

    NSColor(calibratedRed: 0.063, green: 0.725, blue: 0.506, alpha: 1.0).setFill()
    tri.fill()

    // Small emerald check badge at top-right corner of frame
    let dotR = s * 0.065
    let dot = NSBezierPath(ovalIn: NSRect(x: s * 0.68 - dotR, y: s * 0.68 - dotR, width: dotR * 2, height: dotR * 2))
    NSColor(calibratedRed: 0.13, green: 0.85, blue: 0.58, alpha: 1.0).setFill()
    dot.fill()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

let pairs: [(Int, String)] = [
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]
for (px, name) in pairs {
    let url = URL(fileURLWithPath: iconset).appendingPathComponent(name)
    try renderIcon(px: px).write(to: url)
}
SWIFT

/usr/bin/iconutil -c icns "$ICONSET" -o "$RES_DIR/AppIcon.icns"
rm -rf "$ICON_TMP"

# 4. Write Info.plist with ATS local networking + macOS folder usage descriptions
cat > "$CONTENTS/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>vi</string>
  <key>CFBundleDisplayName</key>
  <string>Video Culler</string>
  <key>CFBundleExecutable</key>
  <string>VideoCuller</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleIdentifier</key>
  <string>app.local.videoculler</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Video Culler</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.1.0</string>
  <key>CFBundleVersion</key>
  <string>2</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key>
    <true/>
  </dict>
  <key>NSDesktopFolderUsageDescription</key>
  <string>Video Culler cần quyền đọc/ghi thư mục Desktop khi bạn chọn lưu video tại đây.</string>
  <key>NSDocumentsFolderUsageDescription</key>
  <string>Video Culler cần quyền đọc thư mục Documents khi bạn quét video tại đây.</string>
  <key>NSDownloadsFolderUsageDescription</key>
  <string>Video Culler cần quyền đọc/ghi thư mục Downloads khi bạn lọc video tại đây.</string>
  <key>NSRemovableVolumesUsageDescription</key>
  <string>Video Culler cần quyền đọc ổ cứng rời SSD để duyệt và copy video.</string>
</dict>
</plist>
EOF

# 5. Clear extended attributes & ad-hoc codesign so Finder opens immediately with zero Gatekeeper prompt
/usr/bin/xattr -cr "$APP_BUNDLE"
/usr/bin/codesign --force --deep --sign - "$APP_BUNDLE"

echo "Built: $APP_BUNDLE"
