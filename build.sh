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
cp README.md THIRD_PARTY.md LICENSE "$APP/Contents/Resources/app/" 2>/dev/null || true

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

# 版本号从 config.py 读，避免 plist 与界面显示漂移
VER=$(grep -m1 'APP_VERSION' config.py | sed -E 's/.*"([0-9.]+)".*/\1/')
echo "› 版本 $VER"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>MeetLite</string>
  <key>CFBundleDisplayName</key><string>MeetLite</string>
  <key>CFBundleIdentifier</key><string>app.meetlite.local</string>
  <key>CFBundleVersion</key><string>${VER}</string>
  <key>CFBundleShortVersionString</key><string>${VER}</string>
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

# 签名：优先用稳定的本机自签身份；没有就用 ad-hoc，但必须显式写死
# designated requirement，否则系统按 cdhash 记账——每次重新构建指纹都变，
# 「设置 → 隐私与安全性 → 录屏与系统录音」里那条授权就变成勾着也无效，
# 用户必须重新授权。把 DR 固定成只认 bundle id，重建后系统才认得出是同一个应用。
IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null | grep -o '"MeetLite[^"]*"' | head -1 | tr -d '"' || true)
if [ -n "$IDENTITY" ]; then
  echo "› 签名（$IDENTITY）"
  codesign --force --deep --sign "$IDENTITY" --identifier app.meetlite.local "$APP" || \
    codesign --force --deep --sign - --identifier app.meetlite.local \
      -r='designated => identifier "app.meetlite.local"' "$APP"
else
  echo "› 签名（ad-hoc，固定 designated requirement）"
  codesign --force --deep --sign - --identifier app.meetlite.local \
    -r='designated => identifier "app.meetlite.local"' "$APP" 2>/dev/null || echo "  跳过签名"
fi

echo "✓ 完成: $(pwd)/$APP"
du -sh "$APP"
