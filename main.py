#!/usr/bin/env python3
"""BlindSpatialAgent —— 视障空间智能体模拟系统 · 启动入口。

用法：
    python main.py                      # 终端实时运行模拟循环（默认）
    python main.py --serve              # 启动 FastAPI + WebSocket 服务（浏览器/iPhone 推流）
    python main.py --demo               # 打印一个完整 SpatialState（JSON）
    python main.py --selftest           # 运行全套自检（70+ 项）
    python main.py --check              # 只做环境自检

录制 / 回放（v0.3 Stage 9）：
    python main.py --serve --record                    # 边跑边录到 recordings/session_*
    python main.py --serve --record logs/run1          # 录到指定目录
    python main.py --serve --record --no-images        # 不存画面（省磁盘）
    python main.py --mode replay --session session_20260917_193000
    python main.py --mode replay --replay-out logs/replay1   # 回放结果另存也可对比

常用参数：
    --ticks N        最多运行 N 轮后退出（默认不限）
    --seed N         固定随机种子，保证可复现
    --no-obstacles   关闭动态障碍
    --no-llm         禁用大模型，强制走规则决策
    --brief          精简输出（只打印动作行）
    --fast           不按真实时间节拍，全速运行
    --port P         服务端口（--serve 时生效）
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config.loader import PROJECT_ROOT, ConfigError, load_config, llm_ready, resolve_path

# =====================================================================
# 项目骨架自检
# =====================================================================
EXPECTED_DIRS: tuple[str, ...] = (
    "config",
    "agent",
    "spatial",
    "simulator",
    "camera",
    "api",
    "logs",
)

EXPECTED_FILES: tuple[str, ...] = (
    "main.py",
    "requirements.txt",
    "README.md",
    "config/config.yaml",
    "spatial/spatial_state.py",
    "spatial/state_manager.py",
    "spatial/world_model.py",
    "simulator/map_simulator.py",
    "simulator/navigation_simulator.py",
    "simulator/obstacle_simulator.py",
    "simulator/sensor_simulator.py",
    "agent/agent_core.py",
    "agent/decision.py",
    "agent/tools.py",
    "agent/memory.py",
    "agent/prompt_template.py",
    "agent/llm_client.py",
    "camera/iphone_receiver.py",
    "camera/image_processor.py",
    "api/websocket_server.py",
    "api/test_page.html",
)

REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("yaml", "PyYAML"),
    ("pydantic", "pydantic"),
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("cv2", "opencv-python"),
    ("numpy", "numpy"),
    ("httpx", "httpx"),
)

LOG = logging.getLogger("bsa")


# =====================================================================
# 日志
# =====================================================================
def setup_logging(cfg: dict[str, Any]) -> Path:
    """初始化日志：控制台（仅警告以上）+ logs/BSA-YYYY-MM-DD.log（全量）。"""
    sys_cfg = cfg["system"]
    log_dir = resolve_path(sys_cfg.get("log_dir", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"BSA-{datetime.now():%Y-%m-%d}.log"

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(sys_cfg.get("log_level", "INFO")).upper(), logging.INFO))
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.WARNING)  # 正常播报由横幅展示，控制台只留异常
    root.addHandler(ch)

    return log_file


# =====================================================================
# 自检
# =====================================================================
def check_runtime() -> tuple[bool, str]:
    v = sys.version_info
    detail = f"Python {v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < (3, 10):
        return False, detail + "（要求 3.10+）"
    if not (PROJECT_ROOT / ".venv").is_dir():
        return False, detail + "（未检测到 .venv）"
    if not sys.prefix.startswith(str(PROJECT_ROOT)):
        return True, detail + "（当前不在项目 .venv 中运行）"
    return True, detail + "（已激活项目 .venv）"


def check_structure() -> tuple[bool, str]:
    missing_dirs = [d for d in EXPECTED_DIRS if not (PROJECT_ROOT / d).is_dir()]
    missing_files = [f for f in EXPECTED_FILES if not (PROJECT_ROOT / f).is_file()]
    if missing_dirs or missing_files:
        parts = []
        if missing_dirs:
            parts.append("缺目录: " + ", ".join(missing_dirs))
        if missing_files:
            parts.append("缺文件: " + ", ".join(missing_files))
        return False, "; ".join(parts)
    return True, f"{len(EXPECTED_DIRS)} 个目录 / {len(EXPECTED_FILES)} 个模块文件齐备"


def check_dependencies() -> tuple[bool, str]:
    missing: list[str] = []
    for module, pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(module)
        except Exception:  # noqa: BLE001
            missing.append(pkg)
    if missing:
        return False, "未安装: " + ", ".join(missing)
    return True, f"{len(REQUIRED_PACKAGES)} 个核心依赖可用"


def check_llm(cfg: dict[str, Any]) -> tuple[bool, str]:
    llm = cfg["llm"]
    if llm_ready(cfg):
        return True, f"{llm['provider']} / {llm['model']} / key=已配置"
    mode = "规则决策降级" if llm.get("fallback_to_rules") else "未配置且未开启降级"
    return True, f"{llm['provider']} / {llm['model']} / key=未配置 → {mode}"


def check_map(cfg: dict[str, Any]) -> tuple[bool, str]:
    m = cfg["simulator"]["map"]
    b = m["bounds"]
    if b["x_max"] <= b["x_min"] or b["y_max"] <= b["y_min"]:
        return False, "bounds 非法"
    for p in m["route_landmarks"]:
        if not (b["x_min"] <= p["x"] <= b["x_max"] and b["y_min"] <= p["y"] <= b["y_max"]):
            return False, f"路径点 {p['name']} 越出地图边界"
    return True, (
        f"{m['name']} floor={m['floor']} "
        f"{b['x_max']:.0f}x{b['y_max']:.0f}m / "
        f"区域 {len(m['zones'])} / 路径点 {len(m['route_landmarks'])} / "
        f"物体 {len(m['static_objects'])} + 区域障碍 {len(m.get('area_objects', []))}"
    )


def check_camera(cfg: dict[str, Any]) -> tuple[bool, str]:
    cam = cfg["camera"]
    img = resolve_path(cam["latest_image_path"])
    if img.is_file():
        age = time.time() - img.stat().st_mtime
        fresh = "新鲜" if age <= cam["stale_after_s"] else "已过期"
        return True, f"latest.jpg 存在（{age:.1f}s 前，{fresh}）"
    return True, f"尚无图片帧 → 等待 WebSocket 端口 {cam['port']} 接入"


def run_self_check(cfg: dict[str, Any]) -> list[tuple[str, bool, str]]:
    return [
        ("运行环境", *check_runtime()),
        ("项目结构", *check_structure()),
        ("依赖安装", *check_dependencies()),
        ("配置中心", True, f"config.yaml 已加载（{len(cfg)} 个段落）"),
        ("空间地图", *check_map(cfg)),
        ("大模型接口", *check_llm(cfg)),
        ("摄像头通道", *check_camera(cfg)),
    ]


# =====================================================================
# 输出
# =====================================================================
WIDTH = 64


def print_self_check(results: list[tuple[str, bool, str]]) -> None:
    print("--- 启动自检 ---")
    for i, (name, ok, detail) in enumerate(results, 1):
        print(f"[{i}/{len(results)}] {name:<12} {'OK  ' if ok else 'FAIL'} {detail}")
    failed = [r[0] for r in results if not r[1]]
    if failed:
        print(f"!! 存在未通过项: {', '.join(failed)}")
    print()


# =====================================================================
# 运行模式
# =====================================================================
def build_system(cfg: dict[str, Any], args: argparse.Namespace) -> Any:
    """按命令行参数装配系统（延迟导入，未用到的模块不加载）。"""
    from agent.agent_core import SpatialAgentSystem, SystemConfig
    from camera.iphone_receiver import IphoneReceiver

    receiver = IphoneReceiver(cfg)
    options = SystemConfig(
        seed=args.seed,
        enable_obstacles=not args.no_obstacles,
        enable_llm=not args.no_llm,
    )
    # ★Stage 9★ `--record` 打开录制：不传值用配置里的 root，传了就存到指定目录。
    system = SpatialAgentSystem(
        cfg, camera=receiver, options=options, recorder=make_recorder(cfg, args)
    )

    if args.seed is None:  # 每次运行都换一个随机场景，避免"每次都一样"
        system.obstacles.rng.seed(datetime.now().microsecond)
        system.sensors.rng.seed(datetime.now().microsecond + 7)
    return system


def make_recorder(cfg: dict[str, Any], args: argparse.Namespace) -> Any | None:
    """按 `--record` 造一个录制器；没给就返回 None（完全不录制，行为与 v0.2 一致）。"""
    if getattr(args, "record", None) is None:
        return None
    from recording.session_recorder import SessionRecorder

    rec_cfg = dict(cfg)
    node = dict(rec_cfg.get("recording", {}) or {})
    if getattr(args, "keep_images", None) is not None:
        node["keep_images"] = bool(args.keep_images)
    if getattr(args, "max_images", None):
        node["max_images"] = int(args.max_images)
    rec_cfg["recording"] = node

    root = (args.record or "").strip() or str(node.get("root") or "recordings")
    return SessionRecorder(
        rec_cfg,
        root=root,
        session_id=getattr(args, "session_id", None) or None,
        notes=getattr(args, "record_notes", "") or "",
        seed=args.seed,
    )


def run_live(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """终端实时运行模拟循环（默认模式）。"""
    system = build_system(cfg, args)
    hz = float(cfg["system"]["tick_hz"])
    dt = 1.0 / hz
    max_ticks = args.ticks or int(cfg["agent"]["loop"].get("max_ticks", 0)) or 10**9

    print(f"BlindSpatialAgent 启动 | 频率 {hz} Hz | 最高 {max_ticks} 轮 | Ctrl+C 退出")
    print(f"目的地: {system.map.destination} | 地图: {system.map.info()['name']}")
    print(f"大模型: {'已接入' if (system.llm and system.llm.available) else '未配置，使用规则决策'}\n")

    t0 = time.perf_counter()
    reached = False
    try:
        for _ in range(max_ticks):
            loop_start = time.perf_counter()
            result = system.step(dt)

            if args.brief:
                st = result.state
                print(
                    f"t={result.elapsed:5.1f}s [{result.action.action_type.value:8s}] "
                    f"pos=({st.user.position.x:5.1f},{st.user.position.y:5.1f}) "
                    f"front={st.environment.front_distance:4.2f} risk={st.risk.level.value:8s} "
                    f"| {result.action.message or '(沉默)'}"
                )
            else:
                print(system.banner())

            if system.finished:
                reached = True
                print(f"\n*** 已到达目的地：{system.map.destination}（仿真用时 {system.elapsed:.1f}s）")
                break

            if not args.fast:
                time.sleep(max(0.0, dt - (time.perf_counter() - loop_start)))
    except KeyboardInterrupt:
        print("\n收到中断，正在退出…")

    wall = time.perf_counter() - t0
    print("\n--- 运行统计 ---")
    print(
        f"仿真时长 {system.elapsed:.1f}s | 实际耗时 {wall:.1f}s | 轮次 {system.tick}"
        f" | 到达={reached}"
    )
    print(
        f"进度 {system.nav.route_progress() * 100:.0f}% | 剩余 {system.nav.remaining_distance():.1f}m"
        f" | 重规划 {system.nav.replan_count} 次"
    )
    print(
        f"播报 {len(system.tools.utterance_log)} 条 | 工具调用 {len(system.tools.call_log)} 次"
        f" | 动态障碍 {system.obstacles.spawn_total} 个"
        f" | 丢帧率 {system.sensors.drop_rate() * 100:.1f}%"
    )
    print("\n--- 播报记录 ---")
    for u in system.tools.utterance_log:
        print(f"  [{u['t']:6.1f}s] ({u['kind']:9s}) {u['text']}")
    return 0


def run_serve(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """启动 FastAPI + WebSocket 服务。"""
    import uvicorn

    from api.websocket_server import create_app

    system = build_system(cfg, args)
    app = create_app(system, cfg)
    host = str(cfg["api"]["host"])
    port = int(args.port or cfg["api"]["port"])

    local_ip = _guess_lan_ip()
    print(f"{cfg['system']['name']} v{cfg['system']['version']} —— 服务模式")
    print(f"  本机访问 : http://127.0.0.1:{port}/")
    if local_ip:
        print(f"  局域网   : http://{local_ip}:{port}/   （iPhone 用 Safari 打开）")
    print(f"  图片上行 : ws://<host>:{port}{cfg['api']['ws_image_path']}")
    print(f"  状态下行 : ws://<host>:{port}{cfg['api']['ws_path']}")
    print("  提示: 浏览器调用摄像头需要 HTTPS 或 localhost；用 http 访问局域网 IP 时请改用「选择本地图片上传」。")
    print()
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def is_lan_ip(ip: str) -> bool:
    """判断一个地址「像不像真实的局域网地址」。

    为什么需要这个校验：本机常开着代理/VPN，UDP connect 选路会选中隧道网卡，
    拿到 198.18.x.x（代理/基准测试常用的虚拟网段）这类地址。
    用户照着启动日志把它填进 iPhone，必然连不上 —— 所以打印前必须过滤掉。
    """
    import ipaddress

    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified:
        return False
    # 198.18.0.0/15 是保留的基准测试网段，代理软件常用作虚拟网卡地址
    if a in ipaddress.ip_network("198.18.0.0/15"):
        return False
    return a.is_private


def _guess_lan_ip() -> str | None:
    """尽力猜出真实的局域网 IP，仅用于启动提示。

    优先级：物理网卡直接查询 > UDP 选路 > 主机名解析，每一层都要过 is_lan_ip 校验，
    避免把 VPN/代理的虚拟地址当成局域网地址报给用户。
    """
    import socket
    import subprocess

    # ① 首选：直接问系统要物理网卡的地址（macOS/BSD 的 ipconfig；其它平台会走异常分支）
    for name in ("en0", "en1", "en2"):
        try:
            out = subprocess.run(
                ["ipconfig", "getifaddr", name],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break  # 本机没有 ipconfig（非 macOS），直接跳到下一层
        if out and is_lan_ip(out):
            return out

    # ② 回退：UDP connect 选路（UDP 是无连接的，不会真的发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if is_lan_ip(ip):
            return ip
    except OSError:
        pass

    # ③ 最后：遍历本机所有 IPv4 地址，挑第一个私有地址
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if is_lan_ip(ip):
                return ip
    except OSError:
        pass
    return None


def run_demo(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """打印一个完整的 SpatialState，方便校对字段结构。"""
    import json

    from agent.prompt_template import SYSTEM_PROMPT

    system = build_system(cfg, args)
    result = system.step(1.0)

    print("=== SpatialState（完整结构） ===")
    print(json.dumps(result.state.model_dump(mode="json"), ensure_ascii=False, indent=2))
    print("\n=== 精简视图（送大模型的部分） ===")
    print(json.dumps(result.state.to_prompt_dict(), ensure_ascii=False, indent=2))
    print("\n=== Agent 行动 ===")
    print(json.dumps(result.action.as_dict(), ensure_ascii=False, indent=2))
    print("\n=== 送模型的系统提示词 ===")
    print(SYSTEM_PROMPT)
    return 0


def run_replay(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """回放一个录制会话。

    ★**全程不调用 simulator**★（任务书第十五节）：
        读历史 `SpatialState` → 重放 Agent → 产出行动。
        世界是冻结的，所以"换了模型 / 换了 Prompt / 换了 policy 之后
        到底哪几轮判断变了"才可比。
    """
    from recording.session_player import SessionPlayer

    node = cfg.get("recording", {}) or {}
    root = (getattr(args, "record", None) or "").strip() or str(node.get("root") or "recordings")

    try:
        player = SessionPlayer(root, args.session or args.session_id)
        player.load()
    except (FileNotFoundError, ValueError) as e:
        print(f"[回放失败] {e}", file=sys.stderr)
        return 2

    info = player.summary()
    print("=" * 68)
    print("BlindSpatialAgent 回放（不调用模拟器）")
    print("=" * 68)
    print(f"  会话        : {info['session_id']}")
    print(f"  目录        : {info['dir']}")
    print(f"  录制时间    : {info['created_at']}   系统版本 {info['system_version']}")
    print(f"  地图        : {info['map']}  →  {info['destination']}")
    print(
        f"  数据        : 状态 {info['states']} / 事件 {info['events']} / "
        f"行动 {info['actions']} / 大模型 {info['llm_calls']}（失败 {info['llm_errors']}）"
        f" / 图片 {info['images']}"
    )
    if info["states"] == 0:
        print("[回放失败] 这个会话里没有状态数据。", file=sys.stderr)
        return 2

    try:
        report = player.replay(
            cfg,
            enable_llm=bool(args.replay_llm and not args.no_llm),
            max_ticks=args.replay_ticks or None,
            out_root=(args.replay_out or None),
            notes=f"replay of {info['session_id']}",
        )
    except Exception as e:  # noqa: BLE001 - CLI 入口必须把异常变成可读的退出码
        print(f"[回放失败] {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("\n--- 回放结果 ---")
    print(f"  重放轮次      : {report.ticks}")
    print(f"  行动分布      : {report.action_types}")
    print(
        f"  与原录制一致  : {report.matched}/{report.compared}"
        f"（{report.match_rate * 100:.1f}%）"
    )
    print(
        f"  模型参与      : {report.llm_used} 轮 | 带图 {report.images_attached} 轮"
        f" | 判定需要视觉 {report.needs_visual} 轮"
    )
    print(f"  平均单轮耗时  : {report.latency_avg_s * 1000:.2f} ms")
    if report.out_dir:
        print(f"  结果已另存    : {report.out_dir}")
    if report.divergences:
        print("\n  与原录制不一致的轮次（最多列 10 条）:")
        for d in report.divergences[:10]:
            print(f"    t={d['t']}s  原 {d['original']}  →  回放 {d['replay']}")
    print("=" * 68)
    return 0


def run_acceptance(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """运行 v0.3 验收场景（任务书第二十八/二十九节的 Gate 与最终 Demo 剧本）。

    与 `--selftest` 的分工：
        --selftest     覆盖**每一层**的细粒度断言（74 项），改代码时用；
        --acceptance   只回答"v0.3 到底能不能按剧本演出来"（10 个场景），验收时用。
    """
    from tests.acceptance import gate_report, run_all

    summary = run_all(cfg, verbose=True, seed=args.seed or 20260917)
    print()
    print(gate_report())
    print("=" * 68)
    status = "全部通过 ✅" if summary["failed"] == 0 else "存在失败项"
    print(
        f"验收场景: 通过 {summary['passed']} / {summary['total']}"
        f" | 失败 {summary['failed']} | 耗时 {summary['duration_s']:.1f}s | {status}"
    )
    if summary["failures"]:
        print("\n失败项：")
        for name, err in summary["failures"]:
            print(f"  ✗ {name}\n    {err}")
    print("=" * 68)
    return 0 if summary["failed"] == 0 else 1


def run_acceptance_demo(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """运行 v0.3 最终验收 Demo（任务书第二十九节「七拍剧本」，一次连续会话）。

    与 `--acceptance` 的区别：验收场景是 10 个互相独立、只回答 Gate 的判定用例；
    本 Demo 把剧本按顺序连续演一遍，证明这些能力能在同一条时间线上串起来。
    """
    from tools.acceptance_demo import Demo

    out = getattr(args, "demo_out", None)
    from pathlib import Path as _Path

    return Demo(
        cfg, seed=args.seed or 20260917, out=_Path(out) if out else None
    ).run()


def run_selftest(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """运行全套自检。"""
    from tests.selftest import run_all

    summary = run_all(cfg, verbose=True, seed=args.seed or 20260917)
    print()
    print("=" * 68)
    print(
        f"自检结果: 通过 {summary['passed']} / {summary['total']}"
        f" | 失败 {summary['failed']} | 跳过 {summary['skipped']}"
        f" | 耗时 {summary['duration_s']:.1f}s"
    )
    if summary["failures"]:
        print("\n失败项：")
        for name, err in summary["failures"]:
            print(f"  ✗ {name}\n    {err}")
    print("=" * 68)
    return 0 if summary["failed"] == 0 else 1


# =====================================================================
# 入口
# =====================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Blind Spatial Agent Simulator")
    p.add_argument("--config", default=None, help="自定义配置文件路径")
    p.add_argument("--serve", action="store_true", help="启动 FastAPI + WebSocket 服务")
    p.add_argument("--demo", action="store_true", help="打印完整 SpatialState JSON")
    p.add_argument("--selftest", action="store_true", help="运行全套自检")
    p.add_argument(
        "--acceptance",
        action="store_true",
        help="运行 v0.3 验收场景（Gate 1~10 + 任务书第二十九节最终 Demo 剧本）",
    )
    p.add_argument(
        "--acceptance-demo",
        dest="acceptance_demo",
        action="store_true",
        help="运行 v0.3 最终验收 Demo（第二十九节七拍剧本，一次连续会话）",
    )
    p.add_argument(
        "--demo-out",
        dest="demo_out",
        default="docs/V03_DEMO_TRANSCRIPT.md",
        help="验收 Demo 的转写输出路径（空串则不落盘）",
    )
    p.add_argument("--check", action="store_true", help="只做环境自检")
    p.add_argument("--ticks", type=int, default=0, help="最多运行多少轮")
    p.add_argument("--seed", type=int, default=None, help="随机种子（可复现）")
    p.add_argument("--port", type=int, default=0, help="服务端口")
    p.add_argument("--no-obstacles", action="store_true", help="关闭动态障碍")
    p.add_argument("--no-llm", action="store_true", help="禁用大模型，强制规则决策")
    p.add_argument("--brief", action="store_true", help="精简输出")
    p.add_argument("--fast", action="store_true", help="不按真实时间节拍，全速运行")
    p.add_argument("--verbose", action="store_true", help="DEBUG 日志")
    # --- v0.3 Stage 9：录制 / 回放 ---
    p.add_argument(
        "--mode",
        choices=["live", "serve", "demo", "replay"],
        default=None,
        help="运行模式（等价于 --serve/--demo 等开关；replay 需要 --session）",
    )
    p.add_argument(
        "--record",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="开启录制（可选指定目录；不给值则用 config.recording.root）",
    )
    p.add_argument("--session", default=None, help="回放：会话 id 或目录名（默认取最近一个）")
    p.add_argument("--session-id", dest="session_id", default=None, help="录制：指定会话 id")
    p.add_argument("--record-notes", dest="record_notes", default="", help="录制：实验备注")
    p.add_argument(
        "--keep-images",
        dest="keep_images",
        action="store_true",
        default=None,
        help="录制：连画面一起存（1 Hz 约 70 MB/小时）",
    )
    p.add_argument("--no-images", dest="keep_images", action="store_false", help="录制：不存画面")
    p.add_argument("--max-images", dest="max_images", type=int, default=None, help="录制：画面张数上限")
    p.add_argument("--replay-out", dest="replay_out", default=None, metavar="DIR", help="回放：结果另存为新会话")
    p.add_argument("--replay-llm", dest="replay_llm", action="store_true", help="回放：启用大模型（默认只用规则基线）")
    p.add_argument("--replay-ticks", dest="replay_ticks", type=int, default=0, help="回放：最多多少轮（0=全部）")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"[配置错误] {e}", file=sys.stderr)
        return 2

    if args.verbose:
        cfg["system"]["log_level"] = "DEBUG"
    log_file = setup_logging(cfg)

    if args.selftest:
        return run_selftest(cfg, args)
    if args.acceptance:
        return run_acceptance(cfg, args)
    if getattr(args, "acceptance_demo", False):
        return run_acceptance_demo(cfg, args)
    mode = getattr(args, "mode", None)
    if mode == "replay":
        return run_replay(cfg, args)
    if args.demo or mode == "demo":
        return run_demo(cfg, args)
    if args.serve or mode == "serve":
        return run_serve(cfg, args)

    print(f"{cfg['system']['name']} v{cfg['system']['version']} ({cfg['system']['codename']})")
    print(f"项目根目录: {PROJECT_ROOT}")
    print(f"日志文件  : {log_file.relative_to(PROJECT_ROOT)}")
    print()
    print_self_check(run_self_check(cfg))

    if args.check:
        return 0
    return run_live(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
