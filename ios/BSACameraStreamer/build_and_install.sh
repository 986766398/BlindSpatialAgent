#!/usr/bin/env bash
#
# 一键构建并安装 "BSA 之眼" 到已连接的 iPhone
# -----------------------------------------------------------------------------
# 前提：Xcode 已登录 Apple ID（Xcode ▸ Settings ▸ Accounts），且团队 ID 正确。
# 说明：本 App 使用独立 Bundle ID（com.bsa.spatialeye），
#       不会覆盖已有的 com.example.BlindCorridor* 系列 App。
#
# 本脚本会依次自检三个最容易卡住的点，并给出可执行的修复指引：
#   ① Xcode 是否登录了 Apple ID（没有账号则无法签名，报错是 "No Accounts"）
#   ② 设备标识是否用对（详见下）
#   ③ 开发者磁盘映像（DDI）能否挂载 —— 纯 Wi-Fi 连接常挂载失败，需 USB
#
# ⚠️ 设备标识有两个，很容易搞混（本项目踩过，导致真机构建静默失败）：
#     - `xcodebuild -destination` 需要的是 **UDID**
#       （形如 00008150-000829A611F0401C，xcodebuild -showdestinations 里能看到）
#     - `devicectl --device` 两种都收，但它自己报的 Identifier 是另一种
#       （形如 8B7EEE49-70DD-534B-B3B0-1AF6C67231A5）
#     拿后者当 -destination 会「找不到目标」，build/ 里不会产出 .app。
#     所以本脚本默认**自动探测 UDID**；需要时可用 DEVICE_ID=... 覆盖。
#
# 用法：
#   bash build_and_install.sh            # 设备已解锁、账号已登录时直接构建安装
#   bash build_and_install.sh --wait     # 先等设备解锁（最多 10 分钟）再构建
#
set -euo pipefail

WAIT_SECONDS=0
for arg in "$@"; do
  case "$arg" in
    --wait) WAIT_SECONDS="${WAIT_UNLOCK_S:-600}" ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg（可用: --wait）" >&2; exit 2 ;;
  esac
done

TEAM_ID="${TEAM_ID:-Q6LSLN3RJ5}"
BUNDLE_ID="${BUNDLE_ID:-com.bsa.spatialeye}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DERIVED="${DERIVED:-${PROJECT_DIR}/build}"
XCODEPROJ="${PROJECT_DIR}/BSACameraStreamer.xcodeproj"

show_destinations() {
  xcodebuild -project "${XCODEPROJ}" -scheme BSACameraStreamer -showdestinations 2>/dev/null
}

# 自动探测第一台**真机 iPhone** 的 UDID。
# 必须排除模拟器（模拟器没有摄像头，跑不了本 App 的真实用途）。
detect_udid() {
  show_destinations \
    | awk '/platform:iOS,/ && !/Simulator/ && !/placeholder/ {
        if (match($0, /id:[^,}]+/)) { print substr($0, RSTART + 3, RLENGTH - 3); exit }
      }'
}

# 设备是否已解锁且可用于开发（锁屏时 xcodebuild 会以 needs to be unlocked 失败，
# 且要白等 60 秒才报错，所以先探测一次更省时间）。
device_ready() {
  local out
  out="$(show_destinations || true)"
  echo "$out" | grep -q "id:${DEVICE_ID}" && ! echo "$out" | grep -q "needs to be unlocked"
}

# --- ① 签名账号自检 ---------------------------------------------------
CODESIGN_IDS="$(security find-identity -v -p codesigning 2>/dev/null | grep -c "Apple Develop" || true)"
if [ "${CODESIGN_IDS:-0}" -eq 0 ]; then
  echo "✗ Xcode 里没有任何 Apple 开发账号，无法签名（会报 No Accounts / No profiles）。" >&2
  echo "  修复：打开 Xcode ▸ Settings… ▸ Accounts ▸ 左下角 + ▸ Apple ID 登录。" >&2
  echo "        免费个人账号即可；登录后在 Signing & Capabilities 里选中该 Team。" >&2
  exit 1
fi
echo "✓ 已检测到 ${CODESIGN_IDS} 个可用的 Apple 开发签名身份"

# ⚠️ 有证书 ≠ 有账号。xcodebuild 是在「无账号」状态下无法自动生成描述文件的。
# 这里只做提示（不阻断）：账号列表在命令行可能读不到但实际可用。
ACCOUNT_LIST="$(defaults read com.apple.dt.Xcode DVTDeveloperAccountManagerAppleIDLists 2>/dev/null || true)"
if ! echo "${ACCOUNT_LIST}" | grep -q "identifier = "; then
  echo "! 命令行读不到 Xcode 的 Apple ID 账号（只有证书）。"
  echo "  若下面构建报 “No Accounts”，请在 Xcode 里选中这台 iPhone 按一次 ⌘R："
  echo "  Xcode 会自行创建描述文件并安装，之后本脚本即可直接使用。"
fi

if [ -z "${DEVICE_ID:-}" ]; then
  DEVICE_ID="$(detect_udid)"
fi

if [ -z "${DEVICE_ID}" ]; then
  echo "✗ 未探测到已连接的真机 iPhone。" >&2
  echo "  请：① 用数据线连接 iPhone；② 解锁屏幕并点「信任」；③ 确认 Xcode ▸ Window ▸ Devices 里能看到它。" >&2
  exit 1
fi

echo "▶ [0/3] 目标设备  UDID=${DEVICE_ID}  team=${TEAM_ID}  bundle=${BUNDLE_ID}"

if [ "$WAIT_SECONDS" -gt 0 ]; then
  echo "▶ 等待设备解锁（最多 ${WAIT_SECONDS}s）… 请在 iPhone 上解锁屏幕"
  waited=0
  until device_ready; do
    if [ "$waited" -ge "$WAIT_SECONDS" ]; then
      echo "✗ 等待 ${waited}s 仍未就绪。" >&2
      echo "  请在 iPhone 上解锁屏幕（并保持解锁），然后重新运行本脚本。" >&2
      exit 1
    fi
    sleep 5
    waited=$((waited + 5))
    [ $((waited % 30)) -eq 0 ] && echo "  … 已等待 ${waited}s"
  done
  echo "  ✓ 设备已就绪（等待 ${waited}s）"
fi

echo "▶ [1/3] 构建 Debug（真机）"
LOG="$(mktemp -t bsa_build).log"

build_with() {
  xcodebuild \
    -project "${XCODEPROJ}" \
    -scheme BSACameraStreamer \
    -configuration Debug \
    -destination "$1" \
    -allowProvisioningUpdates \
    DEVELOPMENT_TEAM="${TEAM_ID}" \
    -derivedDataPath "${DERIVED}" \
    build >"${LOG}" 2>&1
}

# 首选：指定设备构建。设备未解锁 / 纯 Wi-Fi 挂不上 DDI 时这里会失败。
if ! build_with "id=${DEVICE_ID}"; then
  if grep -q "could not be mounted" "${LOG}"; then
    echo "  ! 开发者磁盘映像（DDI）挂载失败 —— 常见于**纯 Wi-Fi 连接**。" >&2
    echo "    建议用数据线连接 iPhone（USB-C），首次安装必须走线；" >&2
    echo "    之后 Xcode 才会允许 Wi-Fi 调试。现在改用「通用 iOS 设备」目标继续构建。" >&2
  elif grep -q "needs to be unlocked" "${LOG}"; then
    echo "  ! 设备锁屏。请在 iPhone 上解锁后重试（或用 --wait 让脚本等你解锁）。" >&2
    exit 1
  fi
  # 兜底：不依赖具体设备，纯离线构建，装包交给 devicectl
  if ! build_with "generic/platform=iOS"; then
    if grep -q "No Accounts\|No profiles for" "${LOG}"; then
      echo "✗ 签名失败：命令行读不到 Xcode 的 Apple 账号（报 No Accounts / No profiles）。" >&2
      echo "  证书是有的，但 xcodebuild 无法据此自动生成描述文件。" >&2
      echo "  ▶ 请这样解决（一次即可）：" >&2
      echo "     1) 回到 Xcode，左上角设备选择器里选中「Jingtao的iPhone」" >&2
      echo "     2) 选中左侧项目 ▸ Signing & Capabilities ▸ 勾选 Automatically manage signing" >&2
      echo "        Team 选 “Jingtao Zhang (Personal Team)”" >&2
      echo "     3) 按 ⌘R 运行一次 —— Xcode 会创建描述文件并自动装机" >&2
      echo "     4) 之后再跑本脚本，就能全命令行完成" >&2
    else
      echo "✗ 构建失败，日志摘录：" >&2
      grep -E "error:|BUILD FAILED" "${LOG}" | head -12 >&2
    fi
    echo "  （完整日志：${LOG}）" >&2
    exit 1
  fi
fi

APP_PATH="${DERIVED}/Build/Products/Debug-iphoneos/BSACameraStreamer.app"
if [ ! -d "${APP_PATH}" ]; then
  echo "✗ 未找到真机构建产物：${APP_PATH}" >&2
  echo "  常见原因：Signing 未配置（Xcode ▸ Signing & Capabilities ▸ Automatically manage signing）。" >&2
  exit 1
fi

echo "▶ [2/3] 安装到 iPhone"
xcrun devicectl device install app --device "${DEVICE_ID}" "${APP_PATH}"

echo "▶ [3/3] 校验设备 App 列表（应看到 BSA 之眼）"
xcrun devicectl device info apps --device "${DEVICE_ID}" 2>/dev/null \
  | grep -E "BSA|BlindCorridor|spatialeye" || true

echo
echo "✅ 完成"
echo
echo "下一步：在手机 App 里填服务器地址并开始推流"
echo "    IP   : $(ipconfig getifaddr en0 2>/dev/null || echo '（见服务端启动日志的「局域网」一行）')"
echo "    端口 : 8000"
echo "    路径 : /ws/camera"
