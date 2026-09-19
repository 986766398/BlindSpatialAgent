#!/usr/bin/env bash
# =====================================================================
# BlindSpatialAgent 一键自验脚本
#
# 用途：每次改完代码后，在 VSCode 终端里跑这一个脚本，就能完成
#       「自检 → 实时运行 → 真实视觉验证 → 起服务并打开测试页」全流程。
#
# 用法：
#   bash scripts/dev_check.sh              # 全流程
#   bash scripts/dev_check.sh --quick      # 只做自检（改小改动时用）
#   bash scripts/dev_check.sh --no-serve   # 自检 + 实时运行 + 视觉验证，不起服务
#
# 退出码：自检失败立即退出（非 0），避免"带病上线"。
#        真实视觉验证失败只告警不中断（未配 Key 时自动跳过）。
# =====================================================================
set -euo pipefail

# 切到项目根目录（脚本在 scripts/ 下）
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PY=".venv/bin/python"
PORT="${BSA_PORT:-8000}"

# 本机若设了 HTTP_PROXY（常见于公司网络/科学上网），必须让 127.0.0.1 绕过代理，
# 否则自检里的 REST/WebSocket 项会被代理以 502 拒绝，表现为「假失败」。
export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"

QUICK=0
DO_SERVE=1
for arg in "$@"; do
  case "$arg" in
    --quick)     QUICK=1 ;;
    --no-serve)  DO_SERVE=0 ;;
    -h|--help)   sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg（可用: --quick / --no-serve）" >&2; exit 2 ;;
  esac
done

# --- 0. 环境检查 ------------------------------------------------------
if [[ ! -x "$PY" ]]; then
  echo "[错误] 找不到虚拟环境 $PROJECT_ROOT/.venv" >&2
  echo "       请先执行: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

hr() { printf '=%.0s' {1..68}; echo; }

hr
echo "BlindSpatialAgent 自验  |  $(date '+%Y-%m-%d %H:%M:%S')"
echo "项目: $PROJECT_ROOT"
echo "解释器: $($PY -V 2>&1)"
hr

# --- 1. 40 项自检（硬门槛）-------------------------------------------
echo
echo ">>> [1/3] 自检（40 项）"
echo
if ! "$PY" main.py --selftest; then
  echo
  echo "[失败] 自检未通过，后续步骤中止。" >&2
  exit 1
fi
echo
echo "[通过] 自检全绿"

[[ "$QUICK" -eq 1 ]] && { echo; echo "（--quick 模式，到此结束）"; exit 0; }

# --- 2. 实时运行一段（看 Agent 边走边说话）-------------------------
TICKS="${BSA_TICKS:-20}"
echo
echo ">>> [2/3] 实时运行 ${TICKS} 轮（真实节拍，约 ${TICKS} 秒）"
echo
"$PY" main.py --ticks "$TICKS" --brief --seed "${BSA_SEED:-42}" || true

# --- 3. 真实大模型视觉验证（可选，需 Key + 网络）---------------------
# 这一步回答的是「真实模型看到真实画面后决策会不会变」，是假模型测不出来的。
# 没配 Key 或没网时自动跳过，不算失败；配了 Key 却失败会显著告警。
echo
echo ">>> [3/4] 真实大模型视觉验证（A/B 对照）"
echo
set +e
"$PY" tools/verify_real_vision.py
VISION_RC=$?
set -e
case "$VISION_RC" in
  0) echo; echo "[通过] 真实多模态链路正常（模型确实看到了画面）" ;;
  2) echo; echo "[跳过] 未配置大模型 Key，跳过真实视觉验证" ;;
  *) echo
     echo "[警告] 真实视觉验证未通过（退出码 $VISION_RC）。" >&2
     echo "       其它功能不受影响（会自动降级规则决策），但请排查 Key / Base URL / 模型名。" >&2
     echo "       排查工具: $PY tools/set_llm_key.py --show" >&2 ;;
esac

[[ "$DO_SERVE" -eq 0 ]] && { echo; echo "（--no-serve 模式，到此结束）"; exit 0; }

# --- 4. 起服务并打开浏览器 -------------------------------------------
echo
echo ">>> [4/4] 启动服务 http://127.0.0.1:${PORT}/  （Ctrl+C 停止）"
echo
# 后台延时打开浏览器；3 秒足够 uvicorn 完成启动
( sleep 3; command -v open >/dev/null && open "http://127.0.0.1:${PORT}/" ) &
exec "$PY" main.py --serve --port "$PORT"
