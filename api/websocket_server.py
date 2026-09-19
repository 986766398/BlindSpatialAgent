"""FastAPI + WebSocket 服务层。

职责：把本地运行的 SpatialAgentSystem 暴露成一个可交互的实时服务。

    GET  /                  摄像头模拟测试页（浏览器 / iPhone Safari）
    GET  /api/state         当前 SpatialState + 最近一次 Agent 行动
    GET  /api/stats         全系统统计（模拟器 / 传感器 / 记忆 / 工具 / 大模型）
    GET  /api/camera        摄像头通道状态（接收计数 + 最新帧元数据）
    GET  /api/map           地图几何（边界 / 分区 / 路点 / 静态物体），供前端画小地图
    GET  /api/llm_check     大模型连通性诊断（真实请求，暴露原始错误；?vision=1 测看图能力）
    GET  /api/test_frame    合成测试图（JPEG），无需真实摄像头
    POST /api/query         提交用户文本指令（模拟语音输入）
    POST /api/image         上传一帧图片（multipart，替代 WebSocket 的兜底通道）
    POST /api/control       暂停 / 继续 / 重置
    WS   /ws/camera         iPhone 图片上行（二进制 JPEG）+ JSON 控制帧（ping/status/hello）
    WS   /ws/agent          Agent 状态与行动下行 + 用户指令上行

主循环跑在后台 asyncio 任务里，真正的计算放进线程池，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from agent.agent_core import SpatialAgentSystem
from camera.image_processor import make_test_frame
from camera.iphone_receiver import IphoneReceiver

LOG = logging.getLogger("bsa.api")
TEST_PAGE = Path(__file__).with_name("test_page.html")


def _extract_error(resp: Any) -> str:
    """从失败的 HTTP 响应里尽量挖出人话错误信息（OpenAI 兼容格式优先）。"""
    try:
        body = resp.json()
    except ValueError:  # 不是 JSON（HTML 错误页 / 空体）
        return str(getattr(resp, "text", ""))[:400]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:400]
        if body.get("message"):
            return str(body["message"])[:400]
    return json.dumps(body, ensure_ascii=False)[:400]


def probe_llm(cfg: dict[str, Any], vision: bool = False) -> dict[str, Any]:
    """对配置里的大模型做一次**真实请求**，返回结构化诊断结果。

    与 LLMClient 的关键差别：LLMClient 出于「主循环绝不能被单次失败打断」的设计，
    会把异常吞掉只留一句 last_error；排查 Key / base_url 问题时，真正有用的是
    HTTP 状态码和 `Incorrect API key provided: ...` 这句原文，所以这里故意全暴露。

    vision=True 时额外发一张合成测试图，验证「模型是否真的能看图」。
    """
    import base64

    import httpx

    c = cfg["llm"]
    key = str(c.get("api_key") or "")
    base = str(c.get("base_url") or "").rstrip("/")
    model = str(c.get("model") or "")

    result: dict[str, Any] = {
        "configured": bool(key and model),
        "provider": c.get("provider"),
        "model": model,
        "base_url": base,
        "key_masked": (key[:6] + "..." + key[-4:]) if len(key) > 12 else ("(未配置)" if not key else "(过短)"),
        "key_length": len(key),
        "vision": vision,
        "ok": False,
        "category": "unknown",
        "hint": "",
    }

    if not result["configured"]:
        result["category"] = "not_configured"
        result["hint"] = "缺少 Key 或模型名：检查 .env 中的 BSA_LLM_API_KEY / BSA_LLM_MODEL。"
        return result

    if vision:
        frame = make_test_frame(text="BSA VISION PROBE")
        content: Any = [
            {"type": "text", "text": "这是一张测试图。只回答一句：你看到了什么颜色或文字？"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii")},
            },
        ]
    else:
        content = "只回复两个字：收到"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 64,
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = f"{base}/chat/completions"

    started = time.perf_counter()
    try:
        # 不给 trust_env=False：本机设了 HTTP_PROXY 时，访问外网恰恰需要走代理。
        with httpx.Client(timeout=float(c.get("timeout_s", 30))) as client:
            resp = client.post(url, json=payload, headers=headers)
    except Exception as e:  # noqa: BLE001 - 网络层异常种类繁多，统一转成结构化诊断
        result["category"] = "network_error"
        result["latency_s"] = round(time.perf_counter() - started, 2)
        result["error"] = f"{type(e).__name__}: {e}"
        result["hint"] = "连不上服务商：检查 base_url 拼写与网络/代理。"
        return result

    result["latency_s"] = round(time.perf_counter() - started, 2)
    result["http_status"] = resp.status_code

    if resp.status_code == 200:
        try:
            body = resp.json()
            text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except (ValueError, AttributeError, TypeError, IndexError):
            text = ""
        result["ok"] = True
        result["category"] = "ok"
        result["reply"] = str(text).strip()[:200]
        result["hint"] = "模型可用，认证与网络均正常。"
    elif resp.status_code in (401, 403):
        result["category"] = "auth_error"
        result["error"] = _extract_error(resp)
        result["hint"] = "认证失败：Key 无效、已过期，或与该 base_url 的服务商不匹配。"
    elif resp.status_code == 404:
        result["category"] = "bad_endpoint"
        result["error"] = _extract_error(resp)
        result["hint"] = "接口或模型名不存在：确认 base_url 是否需以 /v1 结尾、model 名是否拼对。"
    elif resp.status_code == 429:
        result["category"] = "rate_limited"
        result["error"] = _extract_error(resp)
        result["hint"] = "被限流或额度耗尽，稍后重试或换 Key。"
    else:
        result["category"] = "http_error"
        result["error"] = _extract_error(resp)
        result["hint"] = "服务商返回非预期状态码，详见 error 字段。"

    return result


def attach_camera(system: SpatialAgentSystem, receiver: IphoneReceiver) -> None:
    """把摄像头接收器接到系统的各个使用方（Agent / 状态融合 / 工具）。"""
    system.camera = receiver
    system.state_manager.camera = receiver
    system.tools.camera = receiver
    system.agent.camera = receiver


class Broadcaster:
    """管理 /ws/agent 的客户端集合与广播。"""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    @property
    def count(self) -> int:
        return len(self._clients)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._clients)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 - 客户端断开是常态
                dead.append(ws)
        for ws in dead:
            await self.remove(ws)


async def _handle_camera_text(ws: WebSocket, text: str | None, receiver: IphoneReceiver) -> None:
    """处理摄像头通道上的文本帧（JSON 控制消息，不含图像）。"""
    if not text:
        return
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        await ws.send_json({"type": "error", "ok": False, "note": "文本帧必须是 JSON；图像请发二进制 JPEG"})
        return
    if not isinstance(msg, dict):
        return

    mtype = str(msg.get("type", "")).lower()
    if mtype == "ping":
        await ws.send_json({"type": "pong", "ok": True, "t": round(time.time(), 3)})
    elif mtype == "status":
        await ws.send_json({"type": "status", "ok": True, **receiver.stats()})
    elif mtype == "hello":
        await ws.send_json(
            {
                "type": "welcome",
                "ok": True,
                "device": str(msg.get("device", "unknown")),
                "max_fps": receiver.max_fps,
            }
        )
    else:
        await ws.send_json({"type": "error", "ok": False, "note": f"未知 type: {mtype or '(空)'}"})


def create_app(system: SpatialAgentSystem, cfg: dict[str, Any]) -> FastAPI:
    """构建 FastAPI 应用（把已装配好的系统注入进来）。"""
    if system.camera is None:
        attach_camera(system, IphoneReceiver(cfg))
    receiver: IphoneReceiver = system.camera  # type: ignore[assignment]

    broadcaster = Broadcaster()
    state: dict[str, Any] = {"paused": False, "runs": 0, "last_tick_ms": 0.0}
    next_t = 1.0 / float(cfg["system"]["tick_hz"])

    def route_points(max_points: int = 240) -> list[list[float]]:
        """剩余规划路径（当前位置 → 局部避障点 → 剩余路点），供前端画实时小地图。"""
        nav = getattr(system, "nav", None)
        if nav is None:
            return []
        pts: list[list[float]] = []
        pos = getattr(nav, "pos", None)
        if pos:
            pts.append([float(pos[0]), float(pos[1])])
        for x, y in list(getattr(nav, "local_path", None) or []):
            pts.append([float(x), float(y)])
        landmarks = list(getattr(nav, "landmarks", None) or [])
        idx = int(getattr(nav, "landmark_idx", 0) or 0)
        for lm in landmarks[idx:]:
            pts.append([float(lm["x"]), float(lm["y"])])
        return pts[:max_points]

    def dynamic_obstacles() -> list[dict[str, Any]]:
        """动态障碍的**绝对坐标**（SpatialState 里只有相对距离/方位，画不了小地图）。"""
        sim = getattr(system, "obstacles", None)
        out: list[dict[str, Any]] = []
        for ob in list(getattr(sim, "obstacles", None) or []):
            out.append(
                {
                    "type": str(getattr(ob, "type", "unknown")),
                    "x": round(float(getattr(ob, "x", 0.0)), 2),
                    "y": round(float(getattr(ob, "y", 0.0)), 2),
                    "ttl": round(float(getattr(ob, "ttl", 0.0)), 1),
                }
            )
        return out

    async def tick_payload() -> dict[str, Any]:
        res = system.history[-1] if system.history else None
        st = system.last_state()
        if res is None or st is None:
            return {"type": "idle"}
        payload = res.as_dict()
        payload["type"] = "tick"
        payload["zone"] = system.map.zone_at(st.user.position.x, st.user.position.y)
        payload["stats"] = system.stats()
        payload["camera_frames"] = receiver.stats()["accepted"]
        payload["route"] = route_points()
        payload["obstacles_dyn"] = dynamic_obstacles()
        return payload

    async def run_loop() -> None:
        """后台主循环：感知 -> 融合 -> 决策 -> 行动 -> 广播。"""
        while True:
            started = time.perf_counter()
            try:
                if not state["paused"]:
                    await asyncio.to_thread(system.step, next_t)
                    state["runs"] += 1
                if broadcaster.count:
                    await broadcaster.broadcast(await tick_payload())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 循环绝不能被单次异常打断
                LOG.exception("主循环异常: %s", e)
            state["last_tick_ms"] = (time.perf_counter() - started) * 1000.0
            await asyncio.sleep(max(0.0, next_t - (time.perf_counter() - started)))

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        task = asyncio.create_task(run_loop())
        LOG.info("Agent 主循环已启动（%.1f Hz）", 1.0 / next_t)
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # ★Stage 7★ 认知循环是独立工作线程，必须显式收尾，
            # 否则热重载（uvicorn --reload）会一轮轮堆积线程。
            try:
                system.close()
            except Exception as e:  # noqa: BLE001 - 收尾失败不应影响进程退出
                LOG.warning("关闭认知循环失败：%s", e)
            LOG.info("Agent 主循环已停止")

    app = FastAPI(title="BlindSpatialAgent", version=cfg["system"]["version"], lifespan=lifespan)

    if cfg["api"].get("enable_cors", True):
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # -----------------------------------------------------------------
    # HTTP
    # -----------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """摄像头模拟测试页。"""
        if TEST_PAGE.is_file():
            return HTMLResponse(TEST_PAGE.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>BlindSpatialAgent</h1><p>测试页缺失，接口仍可用。</p>")

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        return JSONResponse(await tick_payload())

    @app.get("/api/stats")
    async def get_stats() -> JSONResponse:
        return JSONResponse(
            {
                "system": system.stats(),
                "camera": receiver.stats(),
                "api": {
                    "clients": broadcaster.count,
                    "paused": state["paused"],
                    "runs": state["runs"],
                    "last_tick_ms": round(state["last_tick_ms"], 2),
                },
            }
        )

    @app.get("/api/camera")
    async def get_camera() -> JSONResponse:
        """摄像头通道状态：接收计数器 + 最新帧元数据（供 iOS App / 联调脚本查询）。"""
        # snapshot() 里带 datetime，必须过一遍 jsonable_encoder 才能进 JSONResponse
        return JSONResponse(
            jsonable_encoder({"stats": receiver.stats(), "snapshot": receiver.snapshot()})
        )

    @app.get("/api/frame")
    async def get_frame(max_age_s: float = 60.0) -> Response:
        """返回摄像头**最新一帧图片**（JPEG 原字节），供网页预览画面。

        为什么需要它：测试页自带的「摄像头注入」面板只能显示**本机浏览器**注入的画面，
        用 iPhone App 推流时，画面只存在于服务端，页面上完全看不到，
        于是出现「手机显示已连上、网页却写着"尚未注入画面"」的困惑。

        与 /api/camera 的分工：那边给元数据（谁在推、多新、多大），这边给像素。
        拆成两个接口是为了让预览轮询不必每次拖回一大坨 JSON。

        `max_age_s` 允许取到"已经过期"的帧（默认 60s 内）：前端要能区分
        「从未收到画面」和「推流断了，最后看到的是这张」。超时才返回 404。
        """
        data = receiver.preview_bytes(max_age_s)
        if not data:
            raise HTTPException(status_code=404, detail="暂无画面（尚未收到任何帧，或已超过 max_age_s）")
        return Response(
            content=data,
            media_type="image/jpeg",
            # 必须禁缓存：否则浏览器会把第一次拿到的帧一直显示下去，预览看起来"卡住"
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    @app.get("/api/map")
    async def get_map() -> JSONResponse:
        """地图几何：边界 / 分区 / 路点 / 静态物体 —— 供前端绘制实时小地图。"""
        m = system.map
        try:
            route = [str(n) for n in m.route_names()]
        except Exception:  # noqa: BLE001 - route_names 是可选辅助信息，取不到不影响地图
            route = []
        return JSONResponse(
            {
                "bounds": {"x_min": m.x_min, "x_max": m.x_max, "y_min": m.y_min, "y_max": m.y_max},
                "grid": {"nx": m.nx, "ny": m.ny, "res": m.res},
                "start": dict(m.start),
                "destination": str(m.destination),
                "route": route,
                "zones": [dict(z) for z in m.zones],
                "landmarks": [
                    {"name": str(lm["name"]), "x": float(lm["x"]), "y": float(lm["y"])}
                    for lm in m.landmarks
                ],
                "objects": [
                    {
                        "type": str(o.type),
                        "x": float(o.x),
                        "y": float(o.y),
                        "radius": float(o.radius),
                        "blocking": bool(o.blocking),
                        # 区域型障碍（施工围挡/储物柜墙…）：hx,hy>0 时前端按矩形画
                        "hx": float(o.hx),
                        "hy": float(o.hy),
                    }
                    for o in m.objects
                ],
            }
        )

    @app.get("/api/llm_check")
    async def get_llm_check(vision: int = 0) -> JSONResponse:
        """大模型连通性诊断：真实打一次请求，暴露原始 HTTP 状态码与错误正文。

        `?vision=1` 时额外发一张合成测试图，验证模型的多模态（看图）能力。
        """
        return JSONResponse(await asyncio.to_thread(probe_llm, cfg, bool(vision)))

    @app.get("/api/test_frame")
    async def test_frame() -> Response:
        """合成一帧测试图，便于在没有摄像头时验证多模态链路。"""
        data = make_test_frame(text="BSA TEST")
        if not data:
            raise HTTPException(status_code=500, detail="测试图生成失败")
        return Response(content=data, media_type="image/jpeg")

    @app.post("/api/query")
    async def post_query(payload: dict[str, Any]) -> JSONResponse:
        text = str(payload.get("text", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="text 不能为空")
        system.submit_query(text)
        return JSONResponse({"ok": True, "queued": text})

    @app.post("/api/image")
    async def post_image(file: UploadFile = File(...)) -> JSONResponse:
        data = await file.read()
        result = receiver.submit(data, source="upload", throttle=False)
        return JSONResponse(result)

    @app.post("/api/control")
    async def post_control(payload: dict[str, Any]) -> JSONResponse:
        action = str(payload.get("action", "")).lower()
        if action == "pause":
            state["paused"] = True
        elif action == "resume":
            state["paused"] = False
        elif action == "reset":
            await asyncio.to_thread(system.reset)
            receiver.clear()
            state["paused"] = False
        elif action == "manual":
            # 切换手动驾驶（WASD）。台面上是"实验员接管仿真里的盲人"。
            on = bool(payload.get("enabled", True))
            await asyncio.to_thread(system.set_manual, on)
            return JSONResponse({"ok": True, "manual": system.manual()})
        elif action == "drive":
            # ★按键上行通道：高频（10+ Hz）、必须极轻★
            #   这里只把键位塞进仿真线程的日志（一次 deque.append），
            #   位移积分在主循环里做 —— 不抢状态、不加锁、不阻塞事件循环。
            system.drive(
                int(payload.get("forward", 0) or 0),
                int(payload.get("turn", 0) or 0),
            )
            return JSONResponse({"ok": True, "manual": system.manual()})
        else:
            raise HTTPException(
                status_code=400, detail="action 必须是 pause/resume/reset/manual/drive"
            )
        return JSONResponse({"ok": True, "paused": state["paused"]})

    # -----------------------------------------------------------------
    # WebSocket
    # -----------------------------------------------------------------
    @app.websocket(cfg["api"]["ws_image_path"])
    async def camera_ws(ws: WebSocket) -> None:
        """iPhone 上行图片通道。

        协议（与 iOS App `BSACameraStreamer` 一一对应）：
            下行 连接即发   {"type":"hello",  "max_fps":..,"max_image_side":..,"stale_after_s":..}
            上行 二进制帧   <JPEG 字节>            —— 唯一的数据帧，不加任何封装
            上行 文本帧     {"type":"ping"}        —— App 心跳，用于确认链路存活
            上行 文本帧     {"type":"status"}      —— 主动查询服务端接收统计
            下行 文本帧     {"type":"pong"} / {"type":"status"} / {"type":"ack"}
        """
        await ws.accept()
        await ws.send_json(
            {
                "type": "hello",
                "ok": True,
                "server": cfg["system"]["name"],
                "version": cfg["system"]["version"],
                "path": cfg["api"]["ws_image_path"],
                "max_fps": receiver.max_fps,
                "max_image_side": receiver.max_side,
                "jpeg_quality": receiver.quality,
                "stale_after_s": receiver.stale_after,
                "note": "二进制帧 = JPEG；文本帧 = {}",
            }
        )
        n = 0
        accepted = 0
        try:
            while True:
                msg = await ws.receive()
                data = msg.get("bytes")
                if data is None:
                    await _handle_camera_text(ws, msg.get("text"), receiver)
                    continue

                result = receiver.submit(data, source="websocket")
                n += 1
                ok = bool(result.get("accepted"))
                accepted += int(ok)
                # 每帧回一个轻量 ack：客户端（iOS App）靠它显示"服务器确认帧数"。
                # JSON 约 80 字节，相对 10KB 的图片可以忽略，且不会挤占上行带宽。
                await ws.send_json(
                    {
                        "type": "ack",
                        "ok": ok,
                        "frames": n,
                        "accepted": accepted,
                        "reason": result.get("reason"),
                        "frame_id": result.get("frame_id", 0),
                        "bytes": (result.get("size") or {}).get("bytes", 0),
                    }
                )
        except WebSocketDisconnect:
            LOG.info("摄像头通道断开，本轮共接收 %d 帧（接受 %d）", n, accepted)
        except RuntimeError:
            # 客户端已断开后再 receive 会抛 RuntimeError，属于正常收尾，不算异常
            LOG.info("摄像头通道已关闭，本轮共接收 %d 帧（接受 %d）", n, accepted)
        except Exception as e:  # noqa: BLE001
            LOG.warning("摄像头通道异常: %s", e)

    @app.websocket(cfg["api"]["ws_path"])
    async def agent_ws(ws: WebSocket) -> None:
        """Agent 状态下行 + 用户指令上行。"""
        await ws.accept()
        await broadcaster.add(ws)
        try:
            await ws.send_json(await tick_payload())
            while True:
                msg = await ws.receive_json()
                mtype = str(msg.get("type", "")).lower()
                if mtype == "query":
                    system.submit_query(str(msg.get("text", "")))
                elif mtype == "control":
                    act = str(msg.get("action", "")).lower()
                    if act in ("pause", "resume"):
                        state["paused"] = act == "pause"
                    elif act == "reset":
                        await asyncio.to_thread(system.reset)
                        receiver.clear()
                    elif act == "manual":
                        await asyncio.to_thread(system.set_manual, bool(msg.get("enabled", True)))
                    elif act == "drive":
                        # 与 HTTP 同款：一次 append，积分交给主循环
                        system.drive(int(msg.get("forward", 0) or 0), int(msg.get("turn", 0) or 0))
                elif mtype == "drive":
                    # 高频键位上行走这条（比 HTTP 省一次往返）
                    system.drive(int(msg.get("forward", 0) or 0), int(msg.get("turn", 0) or 0))
                await ws.send_json({"type": "ack", "ok": True})
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            pass  # 客户端断开后继续 receive 会抛 RuntimeError，正常收尾
        except Exception as e:  # noqa: BLE001
            LOG.warning("Agent 通道异常: %s", e)
        finally:
            await broadcaster.remove(ws)

    return app


__all__ = ["Broadcaster", "attach_camera", "create_app", "probe_llm"]
