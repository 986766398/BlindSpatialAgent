#!/bin/zsh
# =============================================================================
# BlindSpatialAgent 一键启动器（macOS）
#
#   用法一：在 Finder 里双击本文件        → 启动服务并自动打开浏览器面板
#   用法二：终端里执行  ./启动服务.command  → 同上
#   用法三：./启动服务.command 8010        → 换端口 8010 启动（8000 被占用时）
#
#   停止：在这个终端窗口按 Ctrl+C，或直接关掉窗口。
#   说明：想只跑自检、不起服务，用 `python main.py --selftest` 即可。
# =============================================================================

cd "$(dirname "$0")" || exit 1

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "❌ 找不到虚拟环境：$PWD/$PY"
  echo "   请先初始化："
  echo "     python3 -m venv .venv"
  echo "     .venv/bin/pip install -r requirements.txt"
  echo
  echo "按回车键关闭…"
  read -r _
  exit 1
fi

PORT="${1:-8000}"

# 本机若设了 HTTP_PROXY，访问 127.0.0.1 必须绕过，否则会被代理以 502 拒绝
export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="127.0.0.1,localhost,::1"
export PYTHONPATH="$PWD"

# 端口被占用就先停掉旧进程（通常是上一次没关干净的服务）
OLD=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null)
if [ -n "$OLD" ]; then
  echo "端口 $PORT 已被占用，先停止旧进程（PID: $(echo "$OLD" | tr '\n' ' ')）…"
  echo "$OLD" | xargs kill 2>/dev/null
  sleep 1
fi

echo "正在启动 BlindSpatialAgent（端口 $PORT）…"
"$PY" main.py --serve --port "$PORT" &
SRV=$!

# 等端口真的起来再开浏览器（最多等 15 秒）
for _ in {1..30}; do
  if curl -s --noproxy '*' -o /dev/null "http://127.0.0.1:$PORT/api/state" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

# BSA_NO_OPEN=1 时只起服务、不开浏览器（供脚本/自动化调用）
if [ -z "$BSA_NO_OPEN" ]; then
  open "http://127.0.0.1:$PORT/" 2>/dev/null
fi

echo
echo "✅ 面板地址：http://127.0.0.1:$PORT/"
echo "   停止服务：按 Ctrl+C，或直接关掉这个窗口。"
echo

# 前台等待服务进程；Ctrl+C 会一并结束它
wait $SRV
