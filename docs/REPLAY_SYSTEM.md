# 录制与回放系统（Replay System）

版本：v0.3.0（`config/config.yaml:system.version = 0.3.0`）
适用范围：`recording/schemas.py`、`recording/session_recorder.py`、`recording/session_player.py`、`main.py`

本文档描述 BlindSpatialAgent 的实验录制与回放能力。核心承诺：**回放 = 在历史状态流上重放 Agent，不调用 simulator**。

---

## 1. 为什么需要录制 / 回放

任务书第十五节（Phase 12）把"Replay / Recording"列为独立阶段，动机有三类：

| 动机 | 说明 |
| --- | --- |
| 科研可复现 | 一次真实盲人实验不该只能看一遍。换成模型 A / 模型 B / 规则基线后，必须能在**同一段历史数据**上重跑并逐轮对比，而不是"现场跑一次"就再也找不回来 |
| 失败可复盘 | 线上出问题时，把会话目录拷回来，离线重放即可定位"哪一轮判断变了"，不必复现当时环境 |
| 三方对比 | 同一 `session` 上做 Model A / Model B / Rule Baseline 三方对比，是评估"换模型到底改了哪些决策"的唯一干净方法 |

三个纪律（实现约束）：

1. **录制不得改变行为**：所有挂钩都在 `try/except` 里，失败只写一行 warn 日志；录制器挂掉 = 少一份数据，绝不拖垮导航主循环。
2. **线程安全**：`record_llm` 可能在认知 worker 线程调用，`record_state/action` 在主线程；内部分段加锁（只护住一次 append 与计数），不跨 IO 长持。
3. **图片不进 JSONL**：画面写 `images/`，JSONL 只留文件名引用。

---

## 2. 会话目录结构

一次录制产物是一个会话目录，默认位于 `recordings/session_YYYYMMDD_HHMMSS/`（命名来自 `new_session_id`，天然按时间排序；`is_valid_session_id` 仅允许字母数字与 `_.-`，挡住 `../` 路径穿越）。

真实文件名（`recording/schemas.py` 注释与 `session_recorder.py` 一致确认）：

```
recordings/session_YYYYMMDD_HHMMSS/
├── states.jsonl     # 每轮完整 SpatialState（完整 dump，可往返重建）
├── events.jsonl     # 每一条事件（一行一条；无事件的轮次不写行）
├── actions.jsonl    # 每轮决策结果 + 来源 + 表达审查 + 延迟
├── llm.jsonl        # 每次大模型调用输入/输出（不含隐藏思维链）
├── images/          # 画面帧（文件名引用进 llm.jsonl 的 request.image_ref）
├── meta.json        # 会话元信息（先读它判断格式是否可读）
└── map.json         # 静态地图快照（zone_at 依赖它；不接模拟器时必读）
```

格式版本：`SCHEMA_VERSION = "1.0"`（`schemas.py:40`）。`meta.json` 缺失或主版本号不符时，回放器直接报错（宁可报错也别读出半个会话）。

### 各 JSONL 的每行列字段（取自 `schemas.py` 真实记录类）

**`states.jsonl` — `StateRecord`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `t` | float | 仿真秒（轮次时间） |
| `tick` | int | 系统轮次号 |
| `zone` | str \| null | 区域名（由 `provider.zone_at` 得出；失败为 null） |
| `state` | dict | **完整** SpatialState dump（`model_dump(mode="json")`），非给模型看的裁剪视图，可 `SpatialState.model_validate` 往返重建 |

**`events.jsonl` — `EventRecord`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `t` | float | 仿真秒 |
| `tick` | int | 轮次号 |
| `event_type` | str | 事件类型值（如 `OBSTACLE_APPROACHING`） |
| `severity` | str | 严重度值 |
| `source` | str | 来源 |
| `subject` | str | 主体（`e.key()`） |
| `payload` | dict | 事件载荷 |

注意：**`events.jsonl` 是一行一条事件，不是一行一轮**。某轮没有事件就不写行（`record_events` 只遍历传入的 events 列表逐条 append）。

**`actions.jsonl` — `ActionRecord`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `t` | float | 仿真秒 |
| `tick` | int | 轮次号 |
| `action` | dict | 行动（`Action.as_dict()`） |
| `source` | str | 来源（`rule` / `llm` / `user`） |
| `llm_used` | bool | 本轮是否用了大模型 |
| `decision` | dict \| null | 认知决策可公开部分（白名单，见第 4 节） |
| `gate` | dict \| null | 表达审查结果（`speak`/`discard`/`code`/`reason`/`channel`） |
| `latency_s` | float \| null | 认知延迟 |
| `visual_need` | dict \| null | 是否需要画面（如 `{"needs_visual": true}`） |
| `error` | str \| null | 错误（若有） |

**`llm.jsonl` — `LlmRecord`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `t` | float | 仿真秒 |
| `tick` | int | 轮次号 |
| `kind` | str | `cognitive` / `tools` |
| `model` | str | 模型名 |
| `ok` | bool | 是否成功 |
| `latency_s` | float | 延迟（秒） |
| `error` | str \| null | 错误（若有） |
| `request` | dict | 请求摘要（含 `image_ref` 引用、`image_bytes`、`prompt_text`、`tool_count` 等） |
| `response` | dict | 响应（仅 `parsed` / `content` / `reason` / `tool_calls`，**不含隐藏思维链**） |

**`meta.json` — `SessionMeta`**：`session_id` / `schema_version` / `created_at` / `closed_at` / `system_version` / `map_name` / `map_size` / `destination` / `seed` / `llm_model` / `llm_base_url` / `llm_enabled` / `notes` / `counts`（各文件条数）/ `extra`。

用 JSONL 而非一个大 JSON 的原因：实验动辄几百上千轮，JSONL 可边跑边追加、崩了只丢最后一行、还能 `grep`/`head` 直接看；一次 dump 必须写完整才有效，且几十 MB 起。

---

## 3. 写入路径与时序

`SpatialAgentSystem.step()`（`agent_core.py:479`）内的录制时序：

```
with self.state_lock:
    env.advance_environment(dt)                          # 1) 环境先变
    state = state_manager.build(...)                     # 2) 融合出 SpatialState
    memory.add_state_snapshot(state)

    if self.llm is not None:
        self.llm.record_clock = (self.elapsed, self.tick)   # ★时钟必须在决策之前写
    if self.recorder is not None:
        recorder.record_state(state, ...)                # 写 states.jsonl

    events = self.events.process(state, self.elapsed)    # 2.5) 事件引擎
    if self.recorder is not None:
        recorder.record_events(events, ...)             # 写 events.jsonl（无事件则 0 行）

    outcome = orchestrator.decide(...)                  # 3) 编排决策（内部非阻塞提交 LLM）
    action = outcome.action
    self._apply(action)                                 # 4) 执行
    env.advance_user(...)                               # 5) 世界推进

recorder.record_action(action, ...)                    # 写 actions.jsonl（step 末尾，本轮结果已知）
```

为什么 `record_clock` 必须在决策**之前**写（`agent_core.py:504` 上方注释 + `llm_client.py:79`）：

认知工作线程会在 `orchestrator.decide()` 内部立刻发起大模型调用（`cognitive.submit` → worker 线程 `think_bundle` → `LLMClient.chat_multimodal`），而大模型录制要打时间戳用的就是 `self.llm.record_clock`（仿真秒, tick）。如果时钟在决策之后才写，worker 线程读到的就是旧值甚至初始 `(0.0, 0)`，导致 `llm.jsonl` 的时间戳整体错位、`replay` 时钟偏移 1 秒，使所有"距上次播报过了多久"的判据错位（实测表现为 ~95% 一致而非 100%）。

写入顺序的语义保证：

- `states` 先写（决策前），保证回放能拿到"决策那一刻看到的完整状态"。
- `events` 紧随（决策前），保证回放时的事件上下文与当时一致。
- `actions` 最后写（决策后），此时 `action` / `source` / `gate` / `latency` 都已确定。
- `llm.jsonl` 由 `LLMClient._record_llm` 在认知线程内、每次调用结束时自行追加（带 `record_clock` 时间戳）。

---

## 4. 字段白名单守卫

录制绝不保存隐藏 chain-of-thought。实现是**白名单而非黑名单**（黑名单"去掉 reasoning_content"在 SDK 加字段时会静默泄露；白名单默认什么都不泄露）。

两处白名单（`session_recorder.py`）：

- `_public_decision`（`session_recorder.py:323`）：只从 `AgentDecision` 拷贝**显式枚举的允许字段**：
  ```python
  for k in ("rationale", "information_gaps", "needs_visual", "model",
            "latency_s", "prompt_chars", "image_attached"):
      if hasattr(decision, k):
          out[k] = jsonable(getattr(decision, k))
  ```
  并额外只取 `action.action_type` 与 `action.reason`——模型的完整 action 对象不整份落盘。
- `_public_gate`（`session_recorder.py:349`）：只取 `speak` / `discard` / `code` / `reason` / `channel`。

注意实现方式：这里用**固定元组枚举允许的字段名**（`hasattr` 逐个取），不是 `dataclasses.fields` 反射。之所以仍叫"白名单守卫"，是因为它明确列出"能落盘什么"，任何未列出的字段（含 SDK 的 `reasoning_content`、内部状态）默认被丢弃。白名单的价值有两层：

1. 防泄露：思维链、内部调试字段不进日志。
2. 防误写大对象：只取标量/小字典，避免把 `SpatialState`、图片字节之类的大对象误塞进 `decision`/`gate` 行撑爆 JSONL（图片走 `images/`、状态走 `states.jsonl`，各自有专门通道）。

通用序列化 `jsonable`（`schemas.py:69`）也做了收敛：`bytes` 只留 `<N bytes>` 占位、**绝不把图片塞进 JSONL**；`Enum` 落 `value`；pydantic 走 `model_dump(mode="json")`。

---

## 5. 回放器架构

`ReplaySource`（`session_player.py:51`）是一个**不接任何模拟器**的替身，靠鸭子类型同时扮演三个角色，让 `SpatialAgent` / `AgentTools` / `ContextBuilder` 一行不改就能在历史状态流上跑：

```
                    ReplaySource（无 simulator）
        ┌───────────────────┬────────────────────┬───────────────────┐
        │ SensorProvider    │ WorldStepper        │ StateManager       │
        │ （只读"看到什么"）│ （推进全空实现）   │ （只需 last_state） │
        ├───────────────────┼────────────────────┼───────────────────┤
        │ read_pose         │ advance_environment │ last_state         │
        │ measure_depth     │ advance_user        │                   │
        │ scan_wall_ahead   │ replan              │                   │
        │ scan_sides        │ world_reference     │                   │
        │ perceive          │ arrived             │                   │
        │ map_snapshot      │ stats               │                   │
        │ zone_at           │ obstacle_centers    │                   │
        │ floor             │ reset               │                   │
        │ read_navigation   │ debug_ground_truth  │                   │
        │ confidence        │                    │                   │
        │ health / sensor_stats                  │                    │                   │
        └───────────────────┴────────────────────┴───────────────────┘
                        装配处（build_agent）：
        AgentTools(cfg, src, src, src, memory, world, None)
        #          provider  stepper  state_manager   camera=None
```

- **角色一 `SensorProvider`**：`read_pose` / `read_navigation` / `confidence` 直接返回当前历史状态里的字段；`perceive` 返回占位 `RawPerception(source="replay")`——回放不重新感知。
- **角色二 `WorldStepper`**：`advance_environment` / `advance_user` 是**空实现**（注释明确"回放的世界是冻结的"）；`replan` 只计数并返回成功（真实结论看下一帧状态）；`reset` 空实现。
- **角色三 `StateManager`**：`AgentTools` 只用 `last_state` 这一属性，返回当前帧历史状态。

`SessionPlayer.build_agent`（`session_player.py:342`）把"主系统里那两行 `provider=SimulatedProvider(...)`"换成 `ReplaySource`，下游一行不动——这正是铁律 3（换硬件不改 Agent，只换 `sensors/` 实现与装配处一行）与铁律 7（除 `SpatialAgentSystem.__init__` 外任何模块不得 `import simulator`）的红利。

### 为什么必须做到零 `import simulator`（铁律 7）

回放的价值在于**把"世界"冻结住，只让"判断"变**。这样同一段真实数据可以反复跑：换模型、换 Prompt、换交互策略、调阈值，然后逐轮比对"这次和上次哪一轮不一致"。一旦回放器 `import simulator`，它就会在自己造的世界里重新随机障碍、重新积分用户位姿，两次结果没有可比性，验证价值归零。

自检硬验收：

- 自检用例 **47**（分层铁律）扫描 `spatial/state_manager.py`、`agent/tools.py`、`agent/agent_core.py`、`recording/session_player.py`、`recording/session_recorder.py` 以及 `sensors/base.py` / `sensors/__init__.py`，确认这些模块均无顶层 `import simulator` / `from simulator` 语句；并反向确认 `sensors.simulated` 只出现在装配处 `agent_core.py`。
- 自检用例 **73**（回放）用正则 `^(?:from|import)\s+simulator\b`（匹配真实 import 语句，不是子串"simulator"）断言 `session_player.py` 没 import 模拟器；并断言 `ReplaySource` 具备全部 19 个方法（缺方法会让装配处静默失效），且 `advance_environment` / `advance_user` 返回 `None`（世界没动才是关键）。

---

## 6. 一致率与差异报告

`SessionPlayer.replay`（`session_player.py:369`）逐轮把历史状态 `advance_to` 给 `ReplaySource`，调用 `agent.decide(state, elapsed, None)` 重算决策，并**同样调用 `agent.apply_action`**（说话之后记忆要变的那一半——交互策略的"是否重复/距上次播报多久"全靠 memory，不重放这半段一致率会掉到 ~82%）。

比对与报告（`ReplayReport`）：

- `compared`：参与比对的轮数（与录制 `actions.jsonl` 行数对齐）。
- `matched`：`action.action_type` 与录制中同 tick 的原 `action_type` **完全一致**的轮数。
- `match_rate = matched / compared`（即"一致率"）。
- `divergences`：不一致轮次的 `{tick, t, original, replay}` 列表（最多记 200 条）。

```python
was = str((original.get("action") or {}).get("action_type") or "")
if was == at:
    report.matched += 1
else:
    report.divergences.append({"tick": ..., "t": ..., "original": was, "replay": at})
```

为什么按 `action_type` 比对、要剔除哪些字段：

- 回放是"同配置重算"，时钟、时间戳、`age_s`、`elapsed` 这类天然不同的字段不应参与判等。实现上直接选 `action_type` 这个**稳定的判别量**作为比对键——它既代表"这轮做了什么性质的决策"，又不受时间字段影响。
- 若要做全字段 diff（例如比 `message`/`reason`），则必须显式剔除 `timestamp` / `age_s` / `elapsed` 这类每轮必然不同的字段，否则会把"时钟偏移"误判成"判断变了"。这就是文档层面"剔除天然不同字段"的含义；本实现的 `action_type` 比较等价于"先做了这层剔除"。
- 一个真实坑（`session_player.py:408`）：首帧 `t` 恰好是 `0.0`（falsy），若用 `raw_t or state.tick` 会静默回退成 tick 号，整条回放时钟偏移 1 秒，一致率掉到 ~95%。必须显式 `if raw_t is not None`。

如何解读不一致：

- 一致率 100%（同配置回放，实测 30/30）→ 证明"换模型/Prompt 之后哪几轮变了"这个对比是有意义的基线。
- 一致率低于 100% → 看 `divergences`：`original` 是录制时的动作类型，`replay` 是本次重算的。若开启了 `--replay-llm` 而录制时是规则模式，差异即"模型相对规则改了哪些决策"，正是科研要找的信号。

实测（自检用例 73 / 验收 S10）：录制 30 轮 → 回放 30/30（100%）。

---

## 7. 命令用法

所有开关定义在 `main.py` 的 `build_parser`（`--mode` 取值 `live/serve/demo/replay`）。

录制（边跑边录）：

```
# 边跑边录到默认 recordings/ 下新建的 session_*
python main.py --serve --record

# 录到指定目录
python main.py --serve --record logs/run1

# 不存画面（省磁盘；元数据仍记录"当时有没有图"）
python main.py --serve --record --no-images

# 自定义会话 id 与实验备注
python main.py --serve --record --session-id exp_promptv3 --record-notes "换了一套新 prompt"

# 限制画面张数上限
python main.py --serve --record --max-images 500
```

回放：

```
# 回放某个会话（默认只用规则基线，不接模拟器）
python main.py --mode replay --session session_20260917_193000

# 不指定 --session 时取 recordings/ 下最近一个 session_*
python main.py --mode replay

# 回放结果另存为新会话（便于与原始并排对比）
python main.py --mode replay --session session_20260917_193000 --replay-out logs/replay1

# 回放时启用大模型（与录制时的规则模式对比"模型改了哪些决策"）
python main.py --mode replay --session session_20260917_193000 --replay-llm

# 只回放前 N 轮
python main.py --mode replay --session session_20260917_193000 --replay-ticks 50
```

参数速查：

| 参数 | 含义 |
| --- | --- |
| `--record [DIR]` | 开启录制；不给值用 `config.recording.root`，给值存到指定目录 |
| `--session-id` | 录制：指定会话 id（默认 `session_YYYYMMDD_HHMMSS`） |
| `--record-notes` | 录制：实验备注（写进 `meta.json.extra`） |
| `--keep-images` / `--no-images` | 录制：是否连画面一起存（默认存） |
| `--max-images` | 录制：画面张数上限（防磁盘写满） |
| `--mode replay` | 进入回放模式（需配合 `--session`） |
| `--session` | 回放：会话 id 或目录名（默认取最近一个） |
| `--replay-out DIR` | 回放：结果另存为新会话（前缀 `replay_`） |
| `--replay-llm` | 回放：启用大模型（默认仅规则基线） |
| `--replay-ticks N` | 回放：最多多少轮（`0`=全部） |

程序化入口（脚本/测试）：`recording.session_player.SessionPlayer(root, session_id)` → `.load()` → `.replay(cfg, enable_llm=..., max_ticks=..., out_root=...)`；`.summary()` 不重放只报"会话里有什么"。

---

## 8. 与任务书 Gate 10 的对应

| 任务书要求 | 对应实现 | 证明测试 / 断言 |
| --- | --- | --- |
| **Gate 10**：Replay 可以读取历史数据 | `SessionPlayer.load` 读 `meta.json` + 四个 jsonl + `map.json`；`iter_states` 逐条 `SpatialState.model_validate` 重建；重放不调用 simulator | `tests/acceptance.py` **S10**（录制 → 回放 100% 一致，不接模拟器）；`tests/selftest.py` **73**（30 轮零差异、世界类推进为空操作、`import simulator` 扫描通过） |

补充说明：

- S10 的断言链：先 `SpatialAgentSystem(step × 30)` 录制，再 `SessionPlayer` 回放同配置，断言 `report.match_rate == 1.0`、`compared == 30`、`ticks == 30`，且回放器模块级正则确认无 `import simulator`。
- 自检用例 73 额外封堵三种"假通过"：① 回放器 import 回 simulator（变"自己造世界"）；② 只复现动作类型但复现不了带状态的判据（错 1 秒整体错位）；③ 行动只是从 `actions.jsonl` 抄出来而非重新算的（回放仍走 `agent.decide`）。

---

## 9. 已知限制与未来扩展

| 项 | 现状 | 说明 |
| --- | --- | --- |
| 比对粒度 | 按 `action_type` | 全字段 diff 需自行剔除 `elapsed`/`timestamp`/`age_s`，见第 6 节 |
| 回放时 LLM | 默认关闭 | `--replay-llm` 开启后，模型输出依赖当时录制的 `images/`（若 `--no-images` 录制则无图） |
| 坏数据 | 单条跳过 | `read_jsonl` 坏行跳过；`iter_states` 单条 `model_validate` 失败跳过，不毁整段回放（自检 73 ⑦） |
| 地图缺失 | 可回放但缺 zone | `map.json` 读不出时 `zone_at` 返回 None，仅区域信息缺失 |
| 时钟起点 | 依赖录制时 `t` | 首帧 `t=0.0` 必须显式判 None，否则整体偏移（见第 6 节） |

未来扩展（v0.4 方向）：

- **真实模型对比实验**：同一 `session` 上跑 Model A / Model B / Rule Baseline 三份回放，用 `divergences` 生成"哪些 tick 决策发生了变化"的对照表，作为论文/评估的量化输入。
- **Prompt / policy 敏感度扫描**：固定历史数据，批量回放不同 `config`（不同 `interaction.*`、`safety.*` 阈值），输出一致率随配置的变化曲线。
- **长实验压缩**：`max_images` 与大 `keep_events` 下的存储优化，以及坏帧自动剔除。

---

## 附：关键函数索引

| 函数 / 类 | 位置 | 作用 |
| --- | --- | --- |
| `SessionRecorder.record_state/events/action/llm` | `recording/session_recorder.py:155/179/202/237` | 四类记录写入 |
| `SessionRecorder._public_decision/_public_gate` | `recording/session_recorder.py:323/349` | 白名单守卫（不落隐藏思维链） |
| `SessionRecorder.save_image` | `recording/session_recorder.py:285` | 画面写 `images/`，返回相对引用 |
| `ReplaySource`（三角色替身） | `recording/session_player.py:51` | 不接 simulator 的鸭子类型替身 |
| `SessionPlayer.load` | `recording/session_player.py:293` | 读会话、校验 schema 版本 |
| `SessionPlayer.replay` | `recording/session_player.py:369` | 历史状态流上重放 Agent + 比对 |
| `ReplayReport.match_rate` | `recording/session_player.py:237` | 一致率 |
| `build_parser` 录制/回放参数 | `main.py:579` 起 | CLI 开关 |
