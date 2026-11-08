#!/bin/bash
# 打包成可双击运行的 MeetLite.app
set -euo pipefail
cd "$(dirname "$0")"
APP="dist/MeetLite.app"
rm -rf "$APP"  # 只清 app 包，保留 dist/ 里的 loopcap.exe、zip、文档
mkdir -p dist "$APP/Contents/MacOS" "$APP/Contents/Resources/app"

echo "› 编译启动器"
swiftc -O -framework AppKit -framework WebKit -o "$APP/Contents/MacOS/MeetLite" launcher/main.swift

echo "› 复制运行时"
cp -R server.py config.py store.py capture.py whisper_engine.py llm.py summarize.py \
      platforms.py model_manager.py models.json updater.py audiodec.py feedback.py trash.py \
      static templates bin "$APP/Contents/Resources/app/"
find "$APP/Contents/Resources/app" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

echo "› 图标"
ICONSET=$(mktemp -d)/MeetLite.iconset
mkdir -p "$ICONSET"
swiftc -O -o /tmp/icon_gen tools/make_icon.swift
/tmp/icon_gen /tmp/icon_1024.png
for s in 16 32 64 128 256 512; do
  sips -z $s $s /tmp/icon_1024.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s*2)); sips -z $d $d /tmp/icon_1024.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/MeetLite.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>MeetLite</string>
  <key>CFBundleDisplayName</key><string>MeetLite</string>
  <key>CFBundleIdentifier</key><string>app.meetlite.local</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleExecutable</key><string>MeetLite</string>
  <key>CFBundleIconFile</key><string>MeetLite</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>MeetLite 需要麦克风来录制会议中你的发言。</string>
  <key>NSScreenCaptureUsageDescription</key><string>MeetLite 需要录制系统声音，以便同时记录会议里其他人的声音。</string>
  <key>NSHumanReadableCopyright</key><string>Local-only meeting notes</string>
</dict></plist>
PLIST

echo "› 签名（ad-hoc）"
codesign --force --deep --sign - "$APP" 2>/dev/null || echo "  跳过签名"

echo "✓ 完成: $(pwd)/$APP"
du -sh "$APP"
