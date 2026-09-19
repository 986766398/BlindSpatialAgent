"""Agent 工具集。

核心约束：**Agent 不能直接输出答案，必须通过工具获取事实、通过工具产生影响。**

这样做的理由：
1. 模型只能看到被允许看到的信息（受控的信息面），避免它凭想象编造空间事实；
2. 所有副作用（播报、重规划）都经过程序校验，模型无法绕过安全策略；
3. 未来接真实硬件时，只要给每个工具换上真实实现，Agent 侧不变。

工具清单：
    get_current_position()   当前位置与朝向
    get_navigation_route()   目标、剩余路线、下一步指令
    get_environment()        前方距离、左右障碍、可通行宽度
    capture_camera()         获取当前摄像头帧状态
    request_visual_observation()  ★Stage 10★ 主动请求一张新画面（信息不足时用）
    replan_route()           触发重新规划
    speak()                  语音播报（模拟）
    ask_user()               向用户提问
    get_world_memory()       空间历史记忆

★v0.3 Stage 3 起，本层只依赖两个协议★
    `SensorProvider`（静态空间知识、导航状态、传感器质量）
    `WorldStepper`（参考位姿 / 路线 / 进度 / 重规划）
本文件不再出现 `from simulator import ...`。

⚠️ 工具读的是 `WorldStepper.world_reference()`（**不加噪**），不是位姿估计。
   两个理由：
   1. 这是程序内部记账用的几何量（"我在哪、还有多远"），再走一遍 UWB 噪声
      没有意义；
   2. 更重要的是**可复现性**：工具调用次数由大模型决定，如果每次调用都消耗
      传感器随机数，同一 seed 下"模型多调一次工具"就会让整条轨迹改变。
"""

from __future__ import annotations

import base64
import threading
from typing import Any, Callable

from agent.memory import AgentMemory
from sensors.base import SensorProvider, WorldStepper
from spatial.state_manager import StateManager
from spatial.world_model import WorldModel


class AgentTools:
    """工具注册表：既供大模型 function calling 使用，也供规则引擎与测试直接调用。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        provider: SensorProvider,
        stepper: WorldStepper,
        state_manager: StateManager,
        memory: AgentMemory,
        world: WorldModel,
        camera: Any | None = None,
        lock: "threading.RLock | None" = None,
    ) -> None:
        self.cfg = cfg
        self.sensor = provider
        self.stepper = stepper
        self.sm = state_manager
        self.memory = memory
        self.world = world
        self.camera = camera
        # ★v0.3 Stage 7★ 认知循环在工作线程里执行工具调用（模型 function calling），
        # 主线程同时在推进模拟器。用一把可重入锁把「工具执行」与「主循环推进」
        # 串行化：锁只在**执行的那几毫秒**内持有，网络等待期间不持锁，
        # 所以 LLM 再慢也不会挡住快循环，同时避免两个线程同时改导航状态。
        self._lock: Any = lock or threading.RLock()

        self.call_log: list[dict[str, Any]] = []
        self.utterance_log: list[dict[str, Any]] = []
        self.elapsed: float = 0.0  # 由 Agent 每轮更新，供 speak 记录时间

        # ★Stage 10 主动感知★ 模型显式索要画面的**一次性**标志。
        #   为什么是一次性：模型每次调用工具都会说"我要看看"，但画面只有一个时刻；
        #   如果不清零，"请求一次"就退化成"从此每轮都附一张图"，又变回 v0.2 的行为。
        #   消费点：`SpatialAgent.build_context()` → `consume_visual_request()`。
        self._visual_request: bool = False
        self.visual_requests: int = 0

        self._registry: dict[str, Callable[..., dict[str, Any]]] = {
            "get_current_position": self.get_current_position,
            "get_navigation_route": self.get_navigation_route,
            "get_environment": self.get_environment,
            "capture_camera": self.capture_camera,
            "request_visual_observation": self.request_visual_observation,
            "replan_route": self.replan_route,
            "speak": self.speak,
            "ask_user": self.ask_user,
            "get_world_memory": self.get_world_memory,
        }

    # =================================================================
    # 1. 位置
    # =================================================================
    def get_current_position(self) -> dict[str, Any]:
        """返回当前位置、楼层、朝向、速度与所属区域。"""
        st = self.sm.last_state
        ref = self.stepper.world_reference()
        return {
            "x": round(ref.x, 2),
            "y": round(ref.y, 2),
            "floor": ref.floor,
            "heading_deg": round(ref.heading, 1),
            "speed_mps": round(ref.speed, 2),
            "zone": ref.zone,
            "walking_status": ref.walking_status.value,
            "arrived": ref.arrived,
            "localization_confidence": (
                st.confidence.localization_confidence if st else None
            ),
        }

    # =================================================================
    # 2. 路线
    # =================================================================
    def get_navigation_route(self) -> dict[str, Any]:
        """返回目的地、剩余路线点、下一步指令与进度。"""
        ref = self.stepper.world_reference()
        return {
            "destination": ref.destination,
            "route": [
                {"name": p.name, "x": round(p.x, 2), "y": round(p.y, 2)}
                for p in ref.route
            ],
            "next_instruction": ref.next_instruction,
            "distance_to_goal_m": round(ref.remaining_distance, 2),
            "progress": round(ref.route_progress, 3),
            "current_landmark": ref.current_landmark,
            "off_route": ref.off_route,
            "replan_count": ref.replan_count,
        }

    # =================================================================
    # 3. 环境
    # =================================================================
    def get_environment(self) -> dict[str, Any]:
        """返回前方可通行性、左右障碍与可通行宽度。"""
        st = self.sm.last_state
        if st is None:
            return {"error": "尚无状态数据"}
        env = st.environment
        return {
            "front_clear": env.front_clear,
            "front_distance_m": round(env.front_distance, 2),
            "left_obstacle": env.left_obstacle,
            "right_obstacle": env.right_obstacle,
            "corridor_width_m": None if env.corridor_width is None else round(env.corridor_width, 2),
            "narrow_passage": env.narrow_passage,
            "obstacles": [
                {
                    "type": o.type.value,
                    "distance_m": round(o.distance, 2),
                    "direction": o.direction.value,
                    "dynamic": o.is_dynamic,
                }
                for o in env.obstacles
            ],
            "risk_level": st.risk.level.value,
            "risk_reason": st.risk.reason,
        }

    # =================================================================
    # 4. 摄像头
    # =================================================================
    def capture_camera(self, include_base64: bool = False) -> dict[str, Any]:
        """获取当前摄像头帧状态。

        返回的是元数据；图像本身会作为多模态输入直接附加在对话里，
        不通过工具结果回传（避免 base64 撑爆上下文）。
        """
        if self.camera is None:
            return {"image_available": False, "reason": "未接入摄像头"}
        snap = self.camera.snapshot()
        out: dict[str, Any] = {
            "image_available": bool(snap.get("image_available")),
            "source": snap.get("source", "none"),
            "frame_id": snap.get("frame_id", 0),
            "age_s": None if snap.get("age_s") is None else round(float(snap["age_s"]), 2),
            "path": str(snap.get("path", "")),
        }
        if include_base64 and out["image_available"]:
            data = self.camera.latest_bytes()
            out["image_base64"] = base64.b64encode(data).decode("ascii") if data else None
        return out

    # =================================================================
    # 5. 主动感知（Stage 10）
    # =================================================================
    def request_visual_observation(self, reason: str = "") -> dict[str, Any]:
        """主动请求一张新的第一视角画面（"我现在信息不足，别让我瞎猜"）。

        任务书第十三节：
            Agent 只有在 ① 事件需要视觉 ② 置信度不足 ③ 用户询问环境 时
            才请求图片。**不要每轮都上传图片。**

        本工具做两件事，都**不阻塞、不改世界**：
          1. 登记一次"下一次认知调用请带上最新画面"（一次性标志，
             `SpatialAgent.build_context()` 会消费它）；
          2. 回一份当前视觉链路的状态，让模型知道请求有没有意义
             （相机根本没接 / 帧已过期 / 帧还新鲜不用再要）。

        ★为什么不让工具直接去取图★
            工具在工作线程里执行（function calling），而相机缓存是主线程独占写的
            （见 Stage 8 的"跨线程只传不可变副本"）。取图统一由主线程的
            `ContextBuilder.take_image()` 做，这里只举手。
        """
        meta: dict[str, Any] = {
            "requested": True,
            "reason": (reason or "").strip(),
            "will_attach_next_cycle": True,
        }
        # 先登记再评估：即使相机没接，"模型请求过视觉"这件事本身也是诊断信号
        self._visual_request = True
        self.visual_requests += 1
        if self.camera is None:
            meta.update(
                image_available=False,
                note="未接入摄像头，请求已登记但不会有画面",
            )
            return meta
        try:
            snap = self.camera.snapshot()
            age = snap.get("age_s")
            meta.update(
                image_available=bool(snap.get("image_available")),
                source=snap.get("source", "none"),
                age_s=None if age is None else round(float(age), 2),
                stale=bool(snap.get("stale", False)),
            )
            if meta["image_available"] and age is not None and float(age) <= 1.0:
                meta["note"] = "已有新鲜画面，本轮直接用最新的即可"
            elif meta["image_available"]:
                meta["note"] = "当前帧偏旧，已请求更新"
            else:
                meta["note"] = "当前无可用画面，已请求，画面到达前请依据传感器结论谨慎行事"
        except Exception as e:  # noqa: BLE001 - 工具失败不得中断主循环
            meta["error"] = f"读取相机状态失败: {e}"
        return meta

    def consume_visual_request(self) -> bool:
        """取走（并清空）一次性取帧请求。返回 True 表示下一轮要带最新画面。"""
        pending = bool(self._visual_request)
        self._visual_request = False
        return pending

    @property
    def visual_request_pending(self) -> bool:
        return bool(self._visual_request)

    # =================================================================
    # 6. 重规划
    # =================================================================
    def replan_route(self, reason: str = "") -> dict[str, Any]:
        """触发重新规划，返回新路线是否可用。"""
        ok = self.stepper.replan()
        self.world.note_replan(self.elapsed, reason)
        ref = self.stepper.world_reference()
        return {
            "success": ok,
            "reason": reason,
            "off_route": ref.off_route,
            "new_route_points": ref.local_path_points,
            "next_instruction": ref.next_instruction,
            "distance_to_goal_m": round(ref.remaining_distance, 2),
        }

    # =================================================================
    # 7. 语音播报（模拟）
    # =================================================================
    def speak(self, message: str, urgency: str = "normal") -> dict[str, Any]:
        """模拟语音播报，并记录进记忆（用于打扰频率控制）。"""
        text = (message or "").strip()
        if not text:
            return {"ok": False, "error": "空播报"}
        self.memory.add_utterance(text, self.elapsed, urgency=urgency, action_type="SPEAK")
        self.utterance_log.append(
            {"t": round(self.elapsed, 2), "text": text, "urgency": urgency, "kind": "speak"}
        )
        return {"ok": True, "spoken": text, "urgency": urgency}

    # =================================================================
    # 8. 询问用户
    # =================================================================
    def ask_user(self, question: str) -> dict[str, Any]:
        """向用户提问，并让用户停下等待回答。"""
        text = (question or "").strip()
        if not text:
            return {"ok": False, "error": "空问题"}
        self.memory.add_utterance(text, self.elapsed, urgency="normal", action_type="ASK_USER")
        self.utterance_log.append(
            {"t": round(self.elapsed, 2), "text": text, "urgency": "normal", "kind": "ask_user"}
        )
        return {"ok": True, "question": text, "user_paused": True}

    # =================================================================
    # 9. 世界记忆
    # =================================================================
    def get_world_memory(self) -> dict[str, Any]:
        """返回空间历史记忆：走过哪些区域、遇到过什么障碍。"""
        ref = self.stepper.world_reference()
        near = self.world.obstacles_near(ref.x, ref.y, radius=5.0)
        return {
            "summary": self.world.summarize(),
            "visited_zones": self.world.visited_zones,
            "replan_total": self.world.replan_total,
            "distance_travelled_m": round(self.world.distance_travelled(), 2),
            "obstacles_nearby": [m.as_dict() for m in near[:5]],
            "spin_warning": self.world.spin_warning,
        }

    # =================================================================
    # 调用与 Schema
    # =================================================================
    @property
    def registry(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return self._registry

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """按名字调用工具，异常一律转成结构化错误，不让模型把系统打崩。

        ★锁的粒度★ 只包住「执行」这一小段：工具读取世界模型、必要时推进导航。
        不要在调用方持锁跨越网络请求，否则快循环会被大模型拖住。
        """
        args = arguments or {}
        fn = self._registry.get(name)
        with self._lock:
            if fn is None:
                result: dict[str, Any] = {"error": f"未知工具: {name}"}
            else:
                try:
                    result = fn(**args)
                except TypeError as e:
                    result = {"error": f"参数错误: {e}"}
                except Exception as e:  # noqa: BLE001 - 工具失败不应中断主循环
                    result = {"error": f"工具执行失败: {e}"}

            self.call_log.append(
                {"tool": name, "arguments": args, "result": result, "t": round(self.elapsed, 2)}
            )
        return result

    def schemas(self) -> list[dict[str, Any]]:
        """导出 OpenAI function calling 格式的工具声明。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_current_position",
                    "description": "获取用户当前位置、楼层、朝向、速度与所在区域",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_navigation_route",
                    "description": "获取目的地、剩余路线点、下一步导航指令与行程进度",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_environment",
                    "description": "获取前方是否可通行、前方距离、左右障碍与可通行宽度",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "capture_camera",
                    "description": "获取当前第一视角摄像头帧是否可用及其新鲜度",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "include_base64": {
                                "type": "boolean",
                                "description": "是否同时返回图片 base64（通常不需要）",
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "request_visual_observation",
                    "description": (
                        "当你信息不足、看不清环境（需要确认前方是什么、地图与传感器矛盾、"
                        "定位置信度低）或用户询问周围环境时，主动请求一张新的第一视角画面。"
                        "不要每一轮都调用；只在结论会因这张画面而改变时才用。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "description": "为什么要看这一眼（简短说明）",
                            }
                        },
                        "required": ["reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "replan_route",
                    "description": "当原路线不可行时重新规划路线",
                    "parameters": {
                        "type": "object",
                        "properties": {"reason": {"type": "string", "description": "重规划原因"}},
                        "required": ["reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "speak",
                    "description": "向用户语音播报一句话（不超过 20 字）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                            "urgency": {"type": "string", "enum": ["low", "normal", "high", "critical"]},
                        },
                        "required": ["message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ask_user",
                    "description": "向用户提问以消除歧义，用户会停下等待回答",
                    "parameters": {
                        "type": "object",
                        "properties": {"question": {"type": "string"}},
                        "required": ["question"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_world_memory",
                    "description": "获取空间历史记忆：走过哪些区域、遇到过什么障碍、是否原地打转",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for c in self.call_log:
            counts[c["tool"]] = counts.get(c["tool"], 0) + 1
        return {
            "calls": len(self.call_log),
            "by_tool": counts,
            "utterances": len(self.utterance_log),
            # ★Stage 10★ 主动取帧请求次数（"Agent 主动想看几眼"）
            "visual_requests": self.visual_requests,
        }


__all__ = ["AgentTools"]
