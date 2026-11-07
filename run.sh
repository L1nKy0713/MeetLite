#!/bin/bash
# 开发模式：直接在浏览器里用（不开原生窗口）
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -u server.py "$@"
