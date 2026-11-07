#!/usr/bin/env bash
# 交叉编译 Windows 采集器（在 macOS 上用 mingw-w64）
set -e
cd "$(dirname "$0")"
CXX=${CXX:-x86_64-w64-mingw32-g++}
OUT=${OUT:-../../dist/loopcap.exe}
mkdir -p "$(dirname "$OUT")"
$CXX -std=c++17 -O2 -static -static-libgcc -static-libstdc++ \
     -o "$OUT" loopcap.cpp -lole32 -loleaut32 -luuid
echo "✓ $OUT ($(du -h "$OUT" | cut -f1))"
