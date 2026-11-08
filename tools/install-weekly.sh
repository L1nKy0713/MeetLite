#!/usr/bin/env bash
# 装一个每周一 10:00 的 launchd 任务，拉取 label=feedback 的 issue 生成周报。
#
#   bash tools/install-weekly.sh            # 安装并立刻跑一次（默认周一 10:00）
#   bash tools/install-weekly.sh --hour 9   # 换时间
#   bash tools/install-weekly.sh --no-run   # 只装不跑
#   bash tools/install-weekly.sh --remove   # 卸载
#
# token 放在 ~/.config/meetlite/gh_token（没有就提示你填）。
set -euo pipefail

LABEL="ai.meetlite.feedback"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3)"
OUT="$ROOT/feedback/digest/weekly.log"

HOUR=10
REMOVE=0
NORUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hour) HOUR="${2:?--hour 需要小时数}"; shift 2 ;;
    --remove) REMOVE=1; shift ;;
    --no-run) NORUN=1; shift ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
done

if [[ "$REMOVE" == "1" ]]; then
  launchctl unload -w "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✓ 已移除 $LABEL"
  exit 0
fi

TOKDIR="$HOME/.config/meetlite"
TOKFILE="$TOKDIR/gh_token"
if [[ ! -f "$TOKFILE" ]]; then
  mkdir -p "$TOKDIR" && chmod 700 "$TOKDIR"
  echo "需要一个 GitHub PAT（只需 repo 读权限，用来读 issues）。"
  echo "生成：https://github.com/settings/tokens"
  read -r -s -p "粘贴 token 后回车：" TOK
  echo
  printf '%s\n' "$TOK" > "$TOKFILE"
  chmod 600 "$TOKFILE"
  echo "✓ 已保存到 $TOKFILE"
fi

mkdir -p "$(dirname "$OUT")"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$ROOT/tools/feedback-weekly.py</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>1</integer>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>$OUT</string>
  <key>StandardErrorPath</key><string>$OUT</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLISTEOF

launchctl unload -w "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
echo "✓ 已安装：每周一 $(printf %02d "$HOUR"):00 生成 $ROOT/feedback/digest/<日期>.md"
echo "  日志：$OUT"

if [[ "$NORUN" != "1" ]]; then
  echo "— 立刻跑一次 —"
  launchctl start "$LABEL"
  sleep 3
  tail -3 "$OUT" 2>/dev/null || true
fi