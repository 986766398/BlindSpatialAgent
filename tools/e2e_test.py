#!/usr/bin/env python3
"""端到端联调脚本：一条命令验证「iPhone 图片流 → Agent → 导航建议」全链路。

它做四件事（全部真实进程、真实协议，不打桩在业务代码里）：
    1. 起一个本地假大模型（tools/fake_llm_server.py，OpenAI 兼容），不需要任何 API Key
    2. 起 BlindSpatialAgent 服务（main.py --serve），大模型指向上面那个假服务
    3. 用 tools/iphone_stub.py 按 iOS App 的同一协议推 JPEG（默认 2 fps，共 10 帧）
    4. 断言并打印链路每一环的实测结果

断言项：
    [图片接收]  接收器收到了帧、latest.jpg 被刷新
    [状态融合]  SpatialState.camera.image_available == True
    [模型输入]  假大模型侧确认请求里带了图片（base64 内联）
    [Agent 输出]最近一次 Action 来自大模型，且是导航建议（SPEAK/CONTINUE…）

用法：
    python tools/e2e_test.py
    python tools/e2e_test.py --frames 20 --fps 2 --port 8010
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


# =====================================================================
# 小工具
# =====================================================================
def http_json(port: int, path: str, timeout: float = 4.0) -> dict | None:
    """用 http.client 直连，避开环境里的 HTTP 代理。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            return None
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def wait_ready(port: int, path: str, label: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if http_json(port, path) is not None:
            print(f"  [ready] {label} 已在 127.0.0.1:{port} 就绪")
            return True
        time.sleep(0.4)
    print(f"  [FAIL] {label} 在 {timeout:.0f}s 内未就绪")
    return False


class Child:
    """被管理的子进程（日志写文件，避免管道阻塞）。"""

    def __init__(self, name: str, cmd: list[str], env: dict[str, str], log_path: Path) -> None:
        self.name = name
        self.log_path = log_path
        self._fh = log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd, cwd=str(PROJECT_ROOT), env=env,
            stdout=self._fh, stderr=subprocess.STDOUT, start_new_session=True,
        )

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                self.proc.terminate()
            try:
                self.proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    self.proc.kill()
        self._fh.close()

    def tail(self, n: int = 25) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return "(无日志)"


# =====================================================================
# 主流程
# =====================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BlindSpatialAgent 摄像头链路端到端测试")
    parser.add_argument("--port", type=int, default=8010, help="Agent 服务端口")
    parser.add_argument("--llm-port", type=int, default=8901, help="假大模型端口")
    parser.add_argument("--frames", type=int, default=10, help="推流帧数")
    parser.add_argument("--fps", type=float, default=2.0, help="推流帧率")
    parser.add_argument("--source", choices=("auto", "camera", "synthetic"), default="synthetic",
                        help="推流画面来源（camera = 用本机摄像头）")
    parser.add_argument("--keep-running", action="store_true", help="测试结束后不杀掉服务（便于手动继续观察）")
    args = parser.parse_args(argv)

    failures: list[str] = []
    report: list[tuple[str, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> bool:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{('  → ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)
        report.append((name, detail))
        return ok

    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            # 让 openai/httpx 直连本机假大模型，别走环境里的 HTTP 代理
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "BSA_LLM_API_KEY": "sk-local-fake-key",
            "BSA_LLM_BASE_URL": f"http://127.0.0.1:{args.llm_port}/v1",
            "BSA_LLM_MODEL": "fake-multimodal",
        }
    )

    latest = PROJECT_ROOT / "logs" / "latest.jpg"
    if latest.exists():
        latest.unlink()
    print(f"清理旧帧：{latest.relative_to(PROJECT_ROOT)}")

    fake_llm: Child | None = None
    server: Child | None = None
    ok_overall = False

    try:
        # -------------------------------------------------------------
        print("\n=== 1. 启动本地假大模型（无需 API Key）===")
        fake_llm = Child(
            "fake-llm",
            [PYTHON, "tools/fake_llm_server.py", "--port", str(args.llm_port)],
            env,
            PROJECT_ROOT / "logs" / "e2e_fake_llm.log",
        )
        if not wait_ready(args.llm_port, "/_stats", "假大模型"):
            print(fake_llm.tail())
            return 1

        # -------------------------------------------------------------
        print("\n=== 2. 启动 Agent 服务（main.py --serve）===")
        server = Child(
            "bsa-server",
            [PYTHON, "main.py", "--serve", "--port", str(args.port)],
            env,
            PROJECT_ROOT / "logs" / "e2e_server.log",
        )
        if not wait_ready(args.port, "/api/camera", "Agent 服务"):
            print(server.tail())
            return 1

        # -------------------------------------------------------------
        print(f"\n=== 3. 推流 {args.frames} 帧 @ {args.fps} fps（模拟 iPhone）===")
        stub_log = PROJECT_ROOT / "logs" / "e2e_stub.log"
        best_state: dict = {}
        best_camera: dict = {}

        with stub_log.open("w", encoding="utf-8") as fh:
            stub = subprocess.Popen(
                [
                    PYTHON, "tools/iphone_stub.py",
                    "--host", "127.0.0.1", "--port", str(args.port),
                    "--frames", str(args.frames), "--fps", str(args.fps),
                    "--source", args.source,
                ],
                cwd=str(PROJECT_ROOT), env=env,
                stdout=fh, stderr=subprocess.STDOUT, start_new_session=True,
            )

            # 关键：必须"边推流边采样"。
            # config.camera.stale_after_s = 3.0，推流一停，帧很快就过期，
            # 事后再查只能看到 image_available=False —— 那是设计如此，不是故障。
            deadline = time.time() + args.frames / max(0.1, args.fps) + 10.0
            while time.time() < deadline:
                snap = http_json(args.port, "/api/state") or {}
                if snap.get("state", {}).get("camera", {}).get("image_available"):
                    best_state = snap
                    best_camera = http_json(args.port, "/api/camera") or {}
                    act = snap.get("action", {})
                    # 最理想的样本：既有新鲜图，行动又确实来自大模型并引用了画面
                    if act.get("source") == "llm" and "画面" in str(act.get("reason", "")):
                        break
                if stub.poll() is not None:
                    break
                time.sleep(0.3)

        try:
            stub.wait(timeout=30)
        except subprocess.TimeoutExpired:
            stub.terminate()
        for line in stub_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-7:]:
            print("    " + line)
        if stub.returncode != 0:
            print(stub_log.read_text(encoding="utf-8", errors="replace"))
            return 1

        # 推流结束后的累计计数（best_camera 是推流中途采的，计数不全）
        final_camera = http_json(args.port, "/api/camera") or {}

        # -------------------------------------------------------------
        print("\n=== 4. 断言链路每一环 ===")

        cstats = final_camera.get("stats", {})
        csnap = best_camera.get("snapshot", {})
        record(
            "接收器收到帧",
            cstats.get("received", 0) >= args.frames,
            f"received={cstats.get('received')} accepted={cstats.get('accepted')} "
            f"rejected={cstats.get('rejected')} throttled={cstats.get('throttled')}",
        )
        record(
            "latest.jpg 已落盘",
            latest.is_file() and latest.stat().st_size > 0,
            f"{latest.stat().st_size} B" if latest.is_file() else "文件不存在",
        )
        record(
            "新鲜帧标记为可用",
            bool(csnap.get("image_available")),
            f"age={csnap.get('age_s')} 阈值=3.0s",
        )

        st = best_state.get("state", {})
        action = best_state.get("action", {})
        cam_state = st.get("camera", {})
        nav = st.get("navigation", {})
        risk = st.get("risk", {})

        record(
            "SpatialState.camera.image_available",
            cam_state.get("image_available") is True,
            f"age_s={cam_state.get('age_s')}",
        )

        llm_stats = http_json(args.llm_port, "/_stats") or {}
        record(
            "多模态模型收到调用",
            llm_stats.get("calls", 0) > 0,
            f"calls={llm_stats.get('calls')}",
        )
        record(
            "其中带图片的调用",
            llm_stats.get("calls_with_image", 0) > 0,
            f"with_image={llm_stats.get('calls_with_image')} "
            f"最大图 {llm_stats.get('max_image_bytes')} B",
        )
        record(
            "状态文本一并送达",
            llm_stats.get("last_text_chars", 0) > 200,
            f"{llm_stats.get('last_text_chars')} 字符",
        )
        record(
            "工具 schema 已下发",
            llm_stats.get("last_tool_count", 0) > 0,
            f"{llm_stats.get('last_tool_count')} 个工具",
        )

        record(
            "Action 来自大模型",
            action.get("source") == "llm",
            f"source={action.get('source')} type={action.get('action_type')}",
        )
        record(
            "输出是可执行的导航行动",
            action.get("action_type") in {"SPEAK", "CONTINUE", "WAIT", "REPLAN", "ASK_USER"},
            str(action.get("action_type")),
        )
        record(
            "建议确实依据了画面（多模态生效）",
            "画面" in str(action.get("reason", "")),
            str(action.get("reason")),
        )

        # -------------------------------------------------------------
        print("\n=== 5. Agent 当前输出 ===")
        print(f"  时间      : {st.get('time')}  (tick {st.get('tick')})")
        print(f"  位置      : {st.get('user', {}).get('position')}  "
              f"朝向 {st.get('user', {}).get('heading_deg')}°")
        print(f"  导航      : {nav.get('next_instruction')}  "
              f"剩余 {nav.get('distance_to_goal')}m  进度 {nav.get('route_progress')}")
        print(f"  环境      : 前方 {st.get('environment', {}).get('front_distance')}m  "
              f"风险 {risk.get('level')}（{risk.get('reason')}）")
        print(f"  画面      : {'有' if cam_state.get('image_available') else '无'}  "
              f"（{cam_state.get('age_s')}s 前）")
        print(f"  行动      : [{action.get('action_type')}] {action.get('message')}")
        print(f"  依据      : {action.get('reason')}")

        ok_overall = not failures

    finally:
        if args.keep_running and ok_overall:
            print("\n(--keep-running：服务保持运行，端口 "
                  f"{args.port} / {args.llm_port}；结束时请手动清理)")
        else:
            print("\n=== 清理子进程 ===")
            for child in (server, fake_llm):
                if child is not None:
                    child.stop()
                    print(f"  已停止 {child.name}")

    print("\n" + "=" * 68)
    if ok_overall:
        print("端到端测试：全部通过 ✅")
        print("iPhone 图片流 → SpatialState → 多模态模型 → 导航行动，链路完整。")
    else:
        print(f"端到端测试：{len(failures)} 项未通过 → {', '.join(failures)}")
        if server is not None:
            print("\n--- Agent 服务日志尾部 ---")
            print(server.tail(15))
    print("=" * 68)
    return 0 if ok_overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
