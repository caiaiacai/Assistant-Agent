#!/bin/bash
# 构建 SATH.app
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$DIR")"
APP="$ROOT/SATH.app"

echo "构建 SATH.app ..."

# 清理
rm -rf "$APP"

# 创建 .app 目录结构
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# 编译 Swift
echo "  编译 Swift ..."
swiftc -o "$APP/Contents/MacOS/SATH" \
    "$DIR/main.swift" \
    -framework Cocoa \
    -framework WebKit \
    -O

# 复制资源
echo "  复制资源 ..."
cp "$ROOT/index.html" "$APP/Contents/Resources/"
cp "$ROOT/bridge.py" "$APP/Contents/Resources/"
cp "$ROOT/sath.db" "$APP/Contents/Resources/"

# 复制 sath-source Python 包（v2：蒸馏/编排/Pipeline）
if [ -d "$ROOT/sath-source" ]; then
  cp -r "$ROOT/sath-source" "$APP/Contents/Resources/sath_source"
  echo "  sath_source: brain/distillation + orchestrator + pipeline"
fi

# 复制 agents 角色库
if [ -d "$ROOT/agents" ]; then
  cp -r "$ROOT/agents" "$APP/Contents/Resources/agents"
  AGENT_COUNT=$(find "$APP/Contents/Resources/agents" -name "SOUL.md" | wc -l | tr -d ' ')
  echo "  Agent 角色库: ${AGENT_COUNT} 个"
fi

# Info.plist
cat > "$APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>SATH</string>
    <key>CFBundleDisplayName</key>
    <string>SATH</string>
    <key>CFBundleIdentifier</key>
    <string>com.sath.todo</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleExecutable</key>
    <string>SATH</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>NSLocationWhenInUseUsageDescription</key>
    <string>SATH 需要您的位置信息来提供智能场景感知</string>
    <key>NSLocationUsageDescription</key>
    <string>SATH 需要您的位置信息来提供智能场景感知</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>SATH 需要获取窗口信息来提供环境感知</string>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key>
        <true/>
    </dict>
</dict>
</plist>
PLIST

echo "✓ 构建完成: $APP"
echo "  运行: open \"$APP\""
