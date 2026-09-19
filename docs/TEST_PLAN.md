# BlindSpatialAgent 测试计划（v0.2.0 → v0.3.0）

> 本文档基于仓库真实源码核对编写。覆盖的测试资产：
> `tests/selftest.py`（75 项）、`tests/acceptance.py`（10 场景）、
> `tools/acceptance_demo.py`（七拍 Demo）、`tools/ui_smoke_test.py`（前端冒烟）、
> `tools/e2e_test.py`（端到端）、`scripts/dev_check.sh`（一键自验）。
>
> 所有命令统一使用虚拟环境解释器：`.venv/bin/python`（Python 3.13）。
> 当前版本：`config/config.yaml` 中 `system.version = 0.3.0`（Stage 11 收尾时由 `0.2.0` 升入）。

---

## 1. 测试哲学：为什么分层、为什么「改完必须自验」

BlindSpatialAgent 是一个感知 / 避障 / 室内导航 / 语音交互的实时闭环系统，
既有可离线验证的纯函数（融合、A\*、规则、安全表），也有强依赖运行时的部分
（认知循环是「工作线程 + 20ms 轮询」、WebSocket 上行、iPhone 图流、真机相机）。
不同部分需要的测试手段完全不同，强行塞进单一测试框架只会互相拖累，因此按
「单元契约 → 子系统自洽 → 验收 Gate → 端到端 → 真机链路」五层分离。

分层的核心收益：

- **细粒度自检（75 项）** 能在毫秒级定位「哪一层契约被破坏」，是回归的主防线。
- **验收（10 场景）** 直接对齐任务书 Gate 1~10，回答的是「对用户而言这系统还
  能不能用」，而不是「代码有没有语法错」。
- **七拍 Demo / e2e / 真机工具** 覆盖自检与验收**永远打不到**的真实链路
  （iPhone 推流、真实多模态 LLM、浏览器渲染管线）。

「改完必须自验」是硬要求而非建议，原因有三：

1. 系统存在「分层铁律」——融合层与工具层不得反向依赖 `simulator`（自检用例 47）。
   任何重构只要动了一根越界依赖，单测可能依然全绿，但整条服务链路会悄悄断裂。
2. 认知循环、安全快循环、表达层闸门三者解耦，靠肉眼看日志极易漏掉「安全层仍
   在工作但 Agent 没说话」这类静默退化。
3. 本机 `main.py --serve` 不热加载，改 `.py` 后必须重启服务，否则冒烟测试会
   跑在旧代码上，制造「我明明改了却还失败」的假象（详见第 8 节陷阱清单）。

> 实测基线（本机，本次发布前）：`--selftest` 75/75、`--acceptance` 10/10、
> `--acceptance-demo` 7/7、`ui_smoke_test` 44/44、`e2e_test` 全通过；
> 纯重构回归「规则模式 120 轮 `Action` 逐轮零差异」。

---

## 2. 测试分层总表

| 层级 | 文件 / 入口 | 用例数 | 覆盖范围 | 运行命令 | 耗时量级 | 是否离线 |
|------|------------|--------|----------|----------|----------|----------|
| L1 环境自检 | `main.py --check` | 1（环境） | 目录/文件/依赖包/配置加载 | `.venv/bin/python main.py --check` | <1s | 离线 |
| L2 细粒度自检 | `tests/selftest.py` | **75** | 数据模型、融合、A\*、世界模型、事件、安全、双循环、表达层、录制回放、主动感知、地图连通性 | `.venv/bin/python main.py --selftest` | 数秒~十数秒 | 离线（默认清空 `llm.api_key`） |
| L3 验收 | `tests/acceptance.py` | **10** | 任务书 Gate 1~10 逐条可判定 | `.venv/bin/python main.py --acceptance` | 十数秒 | 离线（默认 `enable_llm=False`） |
| L3 七拍 Demo | `tools/acceptance_demo.py` | **7 拍** | 第二十九节连续剧本（一次会话） | `.venv/bin/python main.py --acceptance-demo` | 十数秒 | 离线（camera 用 stub） |
| L4 前端冒烟 | `tools/ui_smoke_test.py` | **44+** | 测试页 `api/test_page.html` 渲染管线 | 先 `.venv/bin/python main.py --serve`，再 `.venv/bin/python tools/ui_smoke_test.py` | 数秒 | 需本机服务 + Node |
| L4 端到端 | `tools/e2e_test.py` | 4 断言环 | iPhone 图流 → Agent → 导航建议（含摄像头链路） | `.venv/bin/python tools/e2e_test.py` | 数十秒 | 自动起假 LLM + 服务 + iPhone stub，离线 |
| L4 真实链路 | `tools/verify_real_vision.py` / `verify_live_camera.py` | 各若干 | 真实多模态 A/B、真机 iPhone 相机 | 见第 3.6 节 | 视网络 | 需真 Key / 真机 |
| 一键自验 | `scripts/dev_check.sh` | 串联 | 自检→实时跑→视觉验证→起服务开页 | `bash scripts/dev_check.sh [--quick\|--no-serve]` | 分钟级 | 视觉验证可跳过 |

> 注：`scripts/dev_check.sh` 注释里写「40 项自检」是过时文案，实际
> `main.py --selftest` 跑满 **75 项**（见第 3.1 节清单）。

ASCII 分层示意：

```
                  BlindSpatialAgent 测试分层
+---------------------------------------------------------------+
| L5 真机 / 真实链路  verify_real_vision / verify_live_camera    |
|                        (需 Key / 真 iPhone，人工辅助)           |
+--------------------------+------------------------------------+
| L4 e2e_test (端到端)     |  ui_smoke_test (前端渲染, 需 serve) |
|    iPhone图流→Agent→导航  |    44+ check 断言                  |
+--------------------------+------------------------------------+
| L3 acceptance (10 场景)  |  acceptance_demo (七拍连续剧本)     |
|        ← 任务书 Gate 1~10                                  |
+--------------------------+------------------------------------+
| L2 selftest (75 项)       单元契约 → 子系统自洽               |
+--------------------------+------------------------------------+
| L1 main.py --check        目录/依赖/配置 环境自检             |
+---------------------------------------------------------------+
```

---

## 3. 逐层详解

### 3.1 细粒度自检（75 项，入口 `main.py --selftest`）

每项以 `runner.run("编号 名称", fn)` 注册，编号 1–75 唯一（源码中标签 69/70 的
声明顺序互换，但编号集合完整；75 为地图 v2 时新增的连通性用例）。默认
`base_cfg()` 会清空 `llm.api_key`，保证
离线、确定、快速。主要分组如下：

| 组 | 编号 | 名称 | 覆盖要点 |
|----|------|------|----------|
| A 配置与模型契约 | 1 | 配置加载与必填段落 | system/llm/camera/simulator/agent/api 段、地图 zones、安全间隙配置 |
| | 2 | SpatialState 校验与 JSON 往返 | Pydantic v2 模型序列化幂等 |
| | 3 | 模型拒绝非法数据 | 边界/类型校验 |
| B 地图与 A\* | 4 | 地图栅格与路点可通行性 | `is_walkable` |
| | 5 | A\* 全局寻路 | 全局路径存在 |
| | 6 | 路径安全间隙且不穿障碍 | `path_clearance_m` |
| | 7 | A\* 绕开动态障碍 | 动态重规划 |
| C 传感器与障碍 | 8 | UWB/IMU/LiDAR 噪声与丢帧 | 传感器模拟 |
| | 9 | UWB 噪声不会把用户推入墙内 | 安全下界 |
| | 10 | 动态障碍生成/移动/清除 | 生命周期 |
| | 11 | 通道变窄事件成对生成 | 事件成对 |
| D 多源融合 | 12 | 多源融合产出合法状态 | 60 轮校验、风险分布统计 |
| | 13 | 摄像头状态融合 | camera 状态入融合 |
| E 工具层 | 14 | 全部工具可调用且 schema 一致 | 工具契约 |
| | 15 | 重规划工具真实生效 | `replan_count +1` |
| F 规则决策 | 16 | 临界风险 → WAIT（用户停步） | 临界闸门 |
| | 17 | 打扰频率控制（不重复播报） | CONTINUE 防刷屏 |
| | 18 | 到达目的地 → 主动告知 | 到达播报 |
| | 19 | 无路可走 → 重规划 → 求助 | 离线求助 |
| | 20 | 通道变窄 → 主动提醒 | 窄道提醒 |
| | 21 | 高风险 → 提醒但允许继续 | 高风险的放行语义 |
| G 安全底线 | 22 | 安全底线覆盖模型决策 | 安全层否决模型 |
| H 记忆/世界模型 | 23 | 记忆窗口与重复检测 | 短期记忆 |
| | 24 | 世界模型记忆与趋势 | 趋势统计 |
| | 25 | 原地打转检测 | spin 检测 |
| I 图像/摄像头 | 26 | 图像解码/缩放/编码 | OpenCV |
| | 27 | 摄像头接收与新鲜度 | 新鲜度三态 |
| J LLM 降级/提示词 | 28 | 大模型不可用时优雅降级 | fallback_to_rules |
| | 29 | 大模型连接失败不中断系统 | 异常隔离 |
| | 30 | 多模态消息构造正确 | 图片内联 |
| | 31 | 提示词渲染与输出解析 | prompt 模板 |
| K 端到端仿真 | 32 | 端到端：入口→出口 全程导航 | 全链路到达 |
| | 33 | 用户提问 → 有回应 | 问答闭环 |
| | 34 | 系统重置 | reset 语义 |
| L 稳定性/API | 35 | 长时间稳定性（600 轮） | 长稳 |
| | 36 | REST 接口（首页/地图/状态/上传/预览帧/诊断/统计/控制） | HTTP |
| | 37 | WebSocket 双通道（图片上行/状态下行） | WS |
| M 安全/网络 | 38 | 大模型失败熔断（避免无限重试） | circuit breaker |
| | 39 | 大模型 Key 体检工具（断行/大小写/套餐配对） | `set_llm_key` |
| | 40 | 局域网 IP 识别（排除代理/VPN 虚拟网段） | LAN 过滤 |
| N v0.3 契约/不确定性/事件/分层 | 41 | v0.3 状态契约（别名+往返幂等+新字段） | |
| | 42 | 五维不确定性（自动补全+权重正确） | |
| | 43 | 可行动性推导（AffordanceState） | |
| | 44 | 摄像头新鲜度三态（none/fresh/stale） | |
| | 45 | 新增自洽性校验（高危地形/零宽度） | |
| | 46 | 传感器适配层契约（双协议+无副作用+健康度） | |
| | **47** | **分层铁律（融合层/工具层不得依赖 simulator）** | ★核心约束 |
| | 48 | 数据新鲜度三态（fresh/stale/missing+阈值可配） | |
| | 49 | 置信度估计（P0-7：不再恒 0.9，退化时下降） | |
| | 50 | 低定位置信度可构造并触发降级（Stage 4 硬验收） | |
| | 51 | 融合层分层与门面转发（Stage 4） | |
| | 52 | 对象记忆（同一物体跨帧持续+淘汰+网格兜底） | |
| | 53 | 短期空间记忆（趋势/峰值/窗口淘汰） | |
| | 54 | 地图四层接口（度量/语义/拓扑/经验） | |
| | 55 | 世界模型分层门面（兼容+五层+reset 语义） | |
| | 56 | 事件去重三道闸（同一椅子不重复报） | |
| | 57 | 事件覆盖面（路线/到达/摄像头/置信度/冲突） | |
| | 58 | 事件引擎接入运行时（产出/不刷屏/订阅/重置） | |
| O v0.7 安全双循环/认知 | 59 | 安全规则表（纯函数+阈值可配+异常隔离） | |
| | **60** | **★硬验收★ LLM 卡 5 秒时快循环与安全层照常工作** | |
| | 61 | 认知循环（异步取用+过期丢弃+触发策略） | |
| | **62** | **大模型调用预算（关闭隐式重试+单轮总闸门）** | ★ |
| | 63 | P0 复核：主循环速率不被 LLM 阻塞 | |
| P v0.8 Action/交互/认知 | **64** | **Action Schema（10 种行动+严格校验+线格式冻结）** | ★`Action.as_dict()` |
| | 65 | 交互策略（v0.2 等价闸门+认知层新增闸门） | |
| | 66 | 上下文构建器（受控信息面+零传感器泄露） | |
| | 67 | 认知智能体（严格校验+失败降级） | |
| | **68** | **★Gate 9★ 事件触发 Agent + 认知输出经策略审查** | |
| | 69 | 规则模式中性（120 轮零新增行为，可证明回归） | ★回归基准 |
| | 70 | ★安全底线复核★ 不可信的模型结论不得停住用户 | |
| Q v0.9 录制/回放/手动 | 71 | 手动驾驶（WASD 接管：前进/转向/撞墙停住/可回自动） | |
| | 72 | 录制（目录契约+只追加+不落盘隐藏思维链） | |
| | 73 | 回放（不接模拟器+历史状态重建+30 轮零差异） | |
| R v1.0 主动感知 | **74** | **★Gate 10★ 主动感知（三条触发条件+节流+一次性握手）** | |
| S 地图连通性 | **75** | **★防静默故障★ 房间可达 + 封闭房间有门 + 门洞有效宽度 + 无孤岛** | 地图 v2 新增 |

实测：**75/75 通过**。

> 第 75 项为什么必须存在：地图是**数据驱动**的（`config.yaml` 的
> `zones` / `static_objects` / `area_objects`），往里加一件家具、挪一个门洞，
> **A\* 不会报错** —— 它只会找不到路，然后智能体在原地打转。这类「导航没停、
> 就是不动」的故障从日志上完全看不出来，必须在改地图的当下被拦住。
> `MapSimulator.connectivity_report()` 是它的实现，也可以单独调用做排查。

### 3.2 验收场景（S01~S10，入口 `main.py --acceptance`）

每个场景给出 `PASS/FAIL` 与一行证据。所有场景默认 `enable_llm=False`（Gate 5），
需要真实视觉的分支用 stub/mock。判定要点与证据行来自 `tests/acceptance.py` 实际
断言逻辑。

| 场景 | 对应 Gate | 判定要点 | 证据行（实际打印） |
|------|-----------|----------|--------------------|
| **S01** 启动 + 导航激活 + 状态契约 | 1/2/8 | 首轮前 `last_state()` 为 None；`user/environment/navigation/semantic_scene` 均有非空 `source`；`user/environment/camera` 有 `timestamp`；`confidence` 在 [0,1]；目的地已装载、`distance_to_goal>0`、有规划路线、首轮 `permits_motion` 且 `message` 非空；提交「带我去卫生间」后目的地保持且仍放行 | `目的地=... 剩余 X.Xm 路线 N 点 首条='...'` |
| **S02** 正常直行保持安静 | 2 | 13 轮直行推进 >5m；连续重复播报数 = 0；播报条数 ≤4；13 秒未到达 | `13 轮直行 X.Xm / 播报 N 条且无重复` |
| **S03** 障碍突现 → 事件 + 安全层立即干预 | 9 | 前方 0.5m 放椅子（净空 0.20m < critical 0.35m）→ 产生 `OBSTACLE_APPEARED`；`safety` 非 None 且 `level=emergency`、`intervene=True`；不放行前进；`message` 非空 | `事件=OBSTACLE_APPEARED 安全=emergency 放行=False 播报='...'` |
| **S04** 路线被阻挡 → 提醒 + 允许继续 + 重规划 | — | 路线临时被挡时仍给出提醒但允许用户继续前进，并触发重规划（`replan_count` 增加） | 提醒播报非空、仍 `permits_motion`、`replan_count` +1 |
| **S05** 障碍消失 → 事件 + 不重复刷播报 | 9 | 障碍清除后产生 `OBSTACLE_CLEARED`，且同一障碍不被重复播报（依赖世界模型时间记忆，非 `obstacles.clear()` 即触发） | 事件含 `OBSTACLE_CLEARED`，重复播报计数 = 0 |
| **S06** 用户问环境 → 主动取图 → 简短回答 | 3/5 | 提问「我旁边有什么？」后 Agent 主动取一帧图、走 mock 视觉分支、返回简短回答（mock 下不依赖真实 Key） | 主动取图标记置位、回答词数受控、来源徽章显示 |
| **S07** 摄像头断线 → 继续导航不误报 | 3 | 摄像头断线后 `camera` 状态标 unavailable，导航继续、安全层不误判为障碍告警 | `camera.unavailable`、无新增误报、仍 `permits_motion` |
| **S08** LLM 卡 5 秒 → 安全快循环照常 | 6/7 | 用慢 LLM（卡 5 秒）时：主循环速率不被阻塞、安全快循环每轮照常判定、不崩溃（Gate 6）、安全层独立于 LLM（Gate 7） | 快循环每轮 `safety` 正常、墙钟总耗时 < 阈值、无未捕获异常 |
| **S09** 全程导航到达 + 主动告知 | 4/2 | 从入口到目的地全程导航，`GOAL_REACHED` 触发后 Agent 主动告知到达（Simulator 可运行 Gate 4） | 到达事件产生、主动告知播报非空、系统进入 finished |
| **S10** 录制 → 回放 100% 一致 | 10 | 先录制再回放，回放不接模拟器、用历史状态重建，逐轮与原始 `SpatialState` 比对零差异（Gate 10） | `回放 N 轮 零差异` |

实测：**10/10 通过**。

### 3.3 七拍 Demo（入口 `main.py --acceptance-demo`）

把任务书第二十九节的「七拍剧本」落成**一次连续会话**，每拍独立判定并输出证据行。
默认 `demo_out = docs/V03_DEMO_TRANSCRIPT.md`（空串则不落盘）。

| 拍 | 剧本 | 在证明什么（对应 Gate/能力） |
|----|------|------------------------------|
| 第 1 拍 | 用户指令「带我去卫生间」→ 导航激活 | 启动 + 指令解析 + 导航激活（Gate 1/2） |
| 第 2 拍 | 正常直行保持安静 | 无障碍路段不刷屏（Gate 2，CONTINUE 抑制） |
| 第 3 拍 | 椅子突现 → 事件 / 安全层 / 绕行 | 事件触发 + 安全层 emergency 干预 + 避障（Gate 9） |
| 第 4 拍 | 障碍消失不刷屏 | `OBSTACLE_CLEARED` 产生且不重复播报 |
| 第 5 拍 | 用户提问 → 取图 → 简短回答 | 主动取图 + 简短回答链路（Gate 3/5） |
| 第 6 拍 | camera 断线继续导航 | 降级不误报（Gate 3） |
| 第 7 拍 | LLM timeout 5 秒安全循环照常 | 安全快循环独立于 LLM（Gate 6/7） |

实测：**7/7 拍通过**。

### 3.4 UI 冒烟（入口 `tools/ui_smoke_test.py`）

不开浏览器，用 **Node + 最小 DOM 桩**把 `api/test_page.html` 的真实 JS 跑起来，
喂真实接口数据（`/api/map`、`/api/state`），断言各面板确实被填充。覆盖：

- 地图渲染（分区/路点/规划路径/用户位姿/动态障碍）
- KPI 四卡（位置/速度/进度/风险）
- 导航与环境面板
- 播报文本 + 决策来源徽章 + 允许前进徽章
- 行动流事件：**同时钉死两条路径**——`CONTINUE` 抑制刷屏 + 非 `CONTINUE` 必插入（含 `data-a` 属性）
- 摄像头画面：本机推流 / 服务端帧 / 空态 三条互斥路径，外加「无 `getUserMedia` 时按钮必须禁用并写明原因」「高频刷新不得越权改显隐」
- 统计面板 / 时钟 / 顶部状态 chip / 原始数据导出
- 手动驾驶（WASD）：首次按键切手动 / 组合键聚合 / 松键回直行 / 输入框不劫持 / 小地图高亮圈跟随 / 按钮绑定
- 大模型诊断面板（走真实 `auth_error` 分支）

**关于用例数**：脚本以 JS `check()` 断言驱动，运行末尾打印 `通过 N / N`。
经源码盘点，共有 **45 处 `check()`**（地图/WS 5、KPI 5、导航环境 5、播报徽章 3、
行动流 4、摄像头 5、统计时钟 4、手动 10、诊断 4），其中诊断 4 项仅在诊断按钮
成功绑定后计入；故实际打印总数在 42~45 之间浮动，常被称为「44 项」。
**结论：写「44+ 项」更准确**，不要写死成固定 44。

前置：`main.py --serve` 已在运行（默认 8000）、本机装有 Node 18+。
实测：**44/44 通过**。

### 3.5 端到端（入口 `tools/e2e_test.py`）

一条命令验证真实链路「iPhone 图片流 → Agent → 导航建议」。它**不桩业务代码**，
而是拉起真实进程：

1. 起本地假大模型 `tools/fake_llm_server.py`（OpenAI 兼容，无需任何 Key）
2. 起 BlindSpatialAgent 服务 `main.py --serve`，大模型指向上面那个假服务
3. 用 `tools/iphone_stub.py` 按 iOS App 同一协议推 JPEG（默认 2 fps，共 10 帧）
4. 断言并打印链路每一环的实测结果

断言环（4 项）：

| 断言环 | 验证内容 |
|--------|----------|
| `[图片接收]` | 接收器收到帧、`latest.jpg` 被刷新 |
| `[状态融合]` | `SpatialState.camera.image_available == True` |
| `[模型输入]` | 假大模型侧确认请求里带了图片（base64 内联） |
| `[Agent 输出]` | 最近一次 `Action` 来自大模型，且是导航建议（SPEAK/CONTINUE…） |

实测：**全部通过**。

### 3.6 真机 / 真实链路验证工具（需人工辅助，非 CI 阻断项）

| 工具 | 用途 | 前置 |
|------|------|------|
| `tools/verify_real_vision.py` | 真实多模态链路 A/B 验证：同一帧分别走真实 LLM 与规则，比对决策差异 | 需真实 Key + 网络；无 Key 自动跳过 |
| `tools/verify_live_camera.py` | 真机 iPhone 相机链路验证：经局域网推流到接收器并刷新 `latest.jpg` | 需真 iPhone + 同一局域网 |
| `tools/verify_image_link.py` | 校验图片链接可达性 / 内联链路 | — |
| `tools/set_llm_key.py` | 写入 / 体检大模型 Key（断行、大小写、套餐配对） | — |
| `tools/fake_llm_server.py` | OpenAI 兼容假 LLM，离线供 e2e 使用 | e2e 自动拉起 |
| `tools/iphone_stub.py` | 模拟 iOS App 推流协议，供 e2e 使用 | e2e 自动拉起 |

---

## 4. Gate 1~10 → 测试映射表

来源：`tests/acceptance.py` 头部对照表，逐条可审计。

| Gate | 要求 | 证明场景 / 用例 | 落点 |
|------|------|----------------|------|
| **Gate 1** | 项目可以启动 | S01 | 启动 + 导航激活 |
| **Gate 2** | 旧 Demo 基本能力还在 | S01 / S02 / S09 | 直行安静、到达告知 |
| **Gate 3** | iPhone 图片仍可接收 | S06 / S07（真机上行另见 `e2e_test.py`、`verify_live_camera.py`） | 取图回答、断线降级 |
| **Gate 4** | Simulator 可运行 | S09 | 全程导航到达 |
| **Gate 5** | LLM 可关闭并用 Mock 测试 | 全部场景 `enable_llm=False` + S06 / S08（stub） | 离线可测 |
| **Gate 6** | LLM 失败不会让系统崩 | S08 | 慢 LLM 下不崩溃 |
| **Gate 7** | Safety Loop 独立于 LLM | S08 | 安全快循环照常 |
| **Gate 8** | SpatialState 有 timestamp/source/confidence | S01 | 状态契约断言 |
| **Gate 9** | Event 可触发 Agent | S03 / S05（另见自检 68 ★Gate 9★） | 事件→Agent |
| **Gate 10** | Replay 可读取历史数据 | S10（另见自检 73/74） | 录制回放一致 |

---

## 5. 命令速查

### 5.1 离线核心（改完必跑）

```bash
# 项目根目录
cd /Users/jingtaozhang/WorkBuddy/2026-09-17-16-21-21/BlindSpatialAgent

# 1) 环境自检（L1）
.venv/bin/python main.py --check

# 2) 细粒度自检 75 项（L2）—— 改完任何 .py 的硬门槛
.venv/bin/python main.py --selftest

# 3) 验收 10 场景（L3）
.venv/bin/python main.py --acceptance

# 4) 七拍 Demo（L3）
.venv/bin/python main.py --acceptance-demo
#    转写落盘到 docs/V03_DEMO_TRANSCRIPT.md（可用 --demo-out "" 关闭）
```

### 5.2 端到端（自动拉起依赖，离线）

```bash
# e2e：自动起假 LLM + --serve + iPhone stub，跑完自清理
.venv/bin/python tools/e2e_test.py
.venv/bin/python tools/e2e_test.py --frames 20 --fps 2 --port 8010
```

### 5.3 前端冒烟（必须先起服务）

```bash
# 终端 A：起服务（默认端口 8000，不热加载）
NO_PROXY="127.0.0.1,localhost,::1" .venv/bin/python main.py --serve

# 终端 B：跑冒烟（依赖上面服务 + 本机 Node）
NO_PROXY="127.0.0.1,localhost,::1" .venv/bin/python tools/ui_smoke_test.py
#    指定地址： tools/ui_smoke_test.py --url http://127.0.0.1:8000
```

### 5.4 一键自验（推荐日常入口）

```bash
bash scripts/dev_check.sh            # 全流程：自检 → 实时跑 → 视觉验证 → 起服务开页
bash scripts/dev_check.sh --quick    # 只做自检（小改动）
bash scripts/dev_check.sh --no-serve # 自检 + 实时跑 + 视觉验证，不起服务
```

### 5.5 本机环境注意事项（都曾浪费大量时间）

```bash
# (a) 本机设了 HTTP_PROXY：任何连本机端口的客户端都要绕过，否则被代理 502。
#     连 127.0.0.1 的：设 NO_PROXY；curl 用 --noproxy '*'
export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"
curl --noproxy '*' http://127.0.0.1:8000/api/state

# (b) 改完 .py 必须重启 --serve（不热加载）：先杀再起
lsof -nP -tiTCP:8000 -sTCP:LISTEN | xargs kill
.venv/bin/python main.py --serve

# (c) main.py 没有 --no-browser 参数（曾误用导致 argparse 报错），不要加。

# (d) 后台 --serve 不能跨回合存活；要常驻必须你在自己的终端里跑。
```

---

## 6. 断言设计原则（规约条目 + 为什么）

> 以下条目来自真实踩坑。每条都是「必须怎么做」的硬规约，后接一句话解释「为什么」。

| # | 规约 | 为什么 |
|---|------|--------|
| 1 | 断言**不许依赖运行时状态**：界面有 `CONTINUE` 防刷屏，必须分别钉死「`CONTINUE` 抑制」与「非 `CONTINUE` 必插入」两条独立路径 | 只断言「最终有 N 条」会被防刷屏机制偶发吞掉，表现成时通时不通 |
| 2 | 纯重构必须用「行为快照逐轮比对」证明，不能只看测试变绿 | 测试变绿只代表新代码没崩，不代表行为等价（见第 7 节） |
| 3 | 涉及过期 / TTL / `expires_at` 的断言**只能用仿真时刻**，不能用 `time.time()` | 仿真秒 ≠ 墙钟秒，用墙钟比较会永远失败 |
| 4 | 认知循环是「工作线程 + 20ms 轮询」：等待认知结果时，每轮 `step()` 之间必须 `time.sleep(0.05)` | 连续跑 20 轮 step 不让出时间片，工作线程一次都轮不到，症状像「模型链路不通」实为线程没机会 |
| 5 | 摆障碍要用 `system.nav.pos` / `system.nav.heading`（用户当前真实朝向正前方 X 米），不要用 `state.user.position` | `step()` 顺序是「融合建 state → 决策 → 执行 → 推进位姿」，`state` 比真值晚一拍（1 m/s 下差 1.1 米） |
| 6 | `events.jsonl` 是一行一条事件，不是一行一轮；断言只能数「本轮实际发布了多少条」 | 按「一轮一条」计数会错，事件可能一轮多条或零条 |
| 7 | `GOAL_REACHED` 比仿真器 `finished` 晚若干轮（到达半径比事件判据宽松）；断言到达必须再多走几轮 | 绑在 `finished` 上的断言会提前失败 |
| 8 | `OBSTACLE_CLEARED` 依赖世界模型时间记忆（`ObjectMemoryStore.evict_after_s = 60s`）与「用户走出感知窗口」，非 `obstacles.clear()` 那一刻；断言不能绑在清空列表上 | 清空列表当时事件不会立即产生，断言会永远等不到 |
| 9 | `StepResult.safety` 判定为 ok 时是 `None`；断言前先归一化（非 None 才当字典用） | 直接 `.get(...)` 会在 None 上抛异常 |
| 10 | `recent_alerts()` 返回**整个会话**告警历史；要断言「某次故障没被误判成安全问题」，必须先记录历史长度、只看**新增**告警 | 否则会把此前合法告警误算进来，制造假阴性/假阳性 |
| 11 | 智能体会原地转身避让：做「安全层每轮都拦」类断言时，要么每轮把障碍重锚定到当前朝向正前方，要么改成更精确的不变式（「只要前方净空进入危险档，就必须 emergency 且禁止前进」） | 障碍钉死正前方后，被挡时智能体可能转身把障碍甩出前向锥，安全层判 ok 是**正确**行为，断言会误失败 |

---

## 7. 回归方法论：用行为快照逐轮比对证明「行为不变」

纯重构（只改内部结构、不动对外契约）最大的危险是「测试全绿但行为悄悄变了」。
本项目的标准做法不是看测试变色，而是**把每一轮的外部可观察行为序列化下来逐字段 diff**。

### 7.1 存快照（脚本怎么做）

跑 N 轮（本项目基准为规则模式 **120 轮**），每轮把对外契约对象序列化保存：

```python
from spatial.state_manager import StateManager  # 及 Action

snapshots = []
for i in range(120):
    r = system.step(1.0)
    snapshots.append({
        "tick": i,
        "state": r.state.model_dump(),        # SpatialState 完整快照
        "action": r.action.as_dict(),         # Action 冻结线格式
    })
```

- `SpatialState.model_dump()`：Pydantic v2 完整字典，覆盖所有对外字段。
- `Action.as_dict()`：用例 64 锁定的「冻结线格式」，行为对比以此为准。

### 7.2 怎么 diff（剔除哪些字段）

两轮之间逐字段比对，但**先剔除天然每轮不同的字段**：

```
必须剔除的字段：
  - timestamp      （每次序列化都不同）
  - age_s          （相对年龄，随轮递增）
  - elapsed        （耗时，随负载浮动）
```

其余字段（位姿、风险档、动作类型、放行标志、播报文本、来源徽章等）必须**逐字段完全相同**。

### 7.3 什么算通过

- 剔除上述字段后，120 轮 `SpatialState` + `Action` 逐字段**零差异** → 行为等价，重构安全。
- 若出现差异：定位到具体轮次与具体字段，回到源码确认是预期改动还是回归。

### 7.4 已落地的实现与实测结果

- 该比对逻辑固化在**自检用例 69「规则模式中性（120 轮零新增行为，可证明回归）」**
  （`t_v08_schema_neutral`）：在同一随机种子下跑规则模式 120 轮，断言行为快照逐轮一致。
- 本次发布前的纯重构回归：**规则模式 120 轮 `Action` 逐轮零差异**，即「行为不变」已用
  快照比对证明，而非仅依赖测试变绿。

---

## 8. 已知测试陷阱清单（症状 → 真因 → 正确做法）

| 症状 | 真因 | 正确做法 |
|------|------|----------|
| 测试偶发失败、多跑几次又过了（尤其播报条数） | 断言只数「最终条数」，被 `CONTINUE` 防刷屏偶发吞掉 | 分别钉死「`CONTINUE` 抑制」与「非 `CONTINUE` 必插入」两条路径（第 6 节 #1） |
| 涉及过期/TTL 的断言永远失败 | 用 `time.time()` 比仿真时钟 | 全部改用仿真时刻（第 6 节 #3） |
| 连跑 20 轮 step 后「模型没反应 / 取不到认知结果」 | 认知循环是工作线程 + 20ms 轮询，连续 step 不让出时间片，线程饿死 | 每轮 step 之间 `time.sleep(0.05)`（第 6 节 #4） |
| 障碍明明放在正前方，断言却说「前方无障碍」 | `state.user.position` 比真值晚一拍（移动前的位姿） | 用 `system.nav.pos` / `system.nav.heading` 摆障碍（第 6 节 #5） |
| 事件数断言差一两个 | 把 `events.jsonl` 当「一行一轮」 | 数「本轮实际发布了多少条事件」（第 6 节 #6） |
| 到达断言提前失败 | 绑在仿真器 `finished`，但 `GOAL_REACHED` 更晚 | 到达后多走几轮再断言（第 6 节 #7） |
| 等不到 `OBSTACLE_CLEARED` | 断言绑在 `obstacles.clear()` | 改为等待世界模型时间记忆 + 走出感知窗口后事件产生（第 6 节 #8） |
| `AttributeError: 'NoneType' object has no attribute 'get'` | `StepResult.safety` 判 ok 时为 `None` | 断言前归一化，非 None 才当字典（第 6 节 #9） |
| 「这次故障被算成安全告警」误报 | `recent_alerts()` 返回整个会话历史 | 先记历史长度，只看新增告警（第 6 节 #10） |
| 「安全层没每轮拦截」断言失败，但系统其实正常 | 智能体被挡后原地转身把障碍甩出前向锥，安全层判 ok 是正确行为 | 每轮重锚定障碍到当前朝向，或改不变式「危险档必 emergency 且禁前进」（第 6 节 #11） |
| 连 127.0.0.1 的服务返回 502 / WS 连不上 | 本机 `HTTP_PROXY` 把本机流量也代理了 | 设 `NO_PROXY=127.0.0.1,localhost,::1`；`curl` 加 `--noproxy '*'` |
| 改了代码冒烟还是旧行为 / 失败依旧 | `--serve` 不热加载，跑在旧进程上 | 改 `.py` 后 `lsof ... | xargs kill` 再起重服务 |
| `main.py: error: unrecognized arguments: --no-browser` | `main.py` 没有该参数 | 不要加 `--no-browser` |
| 后台 `--serve` 下一回合没了 | 后台进程不跨会话存活 | 常驻需在你的终端里手动跑 |
| `python main.py --selftest` 实测 75 项，但文档/脚本注释写「40 项」 | 注释过时 | 以实际 `runner.run` 数量为准：75 项 |

---

## 9. 发布前检查清单（v0.2.0 → v0.3.0）

> 勾选前先跑第 5 节命令；全部离线项应 75/75、10/10、7/7、44+/44、e2e 通过。
> 以下「9 份文档」= `docs/` 下 8 份 markdown + 根目录 `README.md`，与
> `V03_MIGRATION_LOG.md` Stage 11「十项验收场景 + 验收 Demo + 9 份文档 + 版本号
> `0.2.0 → 0.3.0`」完全对应。

9 份文档（更新/复核）：

- [ ] `README.md`（根目录）：版本号与本次新增能力（录制/回放、主动感知等）
- [ ] `docs/ARCHITECTURE_V03.md`：目标架构（Stage 1~11）
- [ ] `docs/SAFETY_ARCHITECTURE.md`：安全架构（快/慢循环、安全层否决）
- [ ] `docs/SPATIAL_STATE_SCHEMA.md`：SpatialState 字段契约（Gate 8 的 timestamp/source/confidence）
- [ ] `docs/V02_ARCHITECTURE_AUDIT.md`：v0.2 架构审计基准（复核分层铁律用例 47 仍成立）
- [ ] `docs/V03_MIGRATION_LOG.md`：迁移记录（追加本次改动）
- [ ] `docs/V03_DEMO_TRANSCRIPT.md`：由 `main.py --acceptance-demo` 重新生成转写
- [ ] `docs/技术交付文档.md`：技术交付文档（补充 v0.3 验收结论与 Gate 对照）
- [ ] `docs/TEST_PLAN.md`：本文件（测试哲学 / 分层 / Gate 映射 / 陷阱 / 回归方法）

版本号（硬门槛之一）：

- [ ] `config/config.yaml`：`system.version` 由 `0.2.0` 升 `0.3.0`

附加硬门槛（发布前必须全绿）：

- [ ] `.venv/bin/python main.py --selftest` → **75/75**
- [ ] `.venv/bin/python main.py --acceptance` → **10/10**
- [ ] `.venv/bin/python main.py --acceptance-demo` → **7/7**
- [ ] `.venv/bin/python tools/ui_smoke_test.py` → **44+/44**（需先起 `--serve` + Node）
- [ ] `.venv/bin/python tools/e2e_test.py` → 全通过
- [ ] 纯重构回归：规则模式 120 轮 `Action` 逐轮零差异（用例 69）
