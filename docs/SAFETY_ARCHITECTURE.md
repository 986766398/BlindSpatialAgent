# 安全架构（Safety Architecture）

版本：v0.3.0（`config/config.yaml:system.version = 0.3.0`）
适用范围：`agent/safety/`、`agent/orchestrator.py`、`agent/agent_core.py`、`agent/interaction_policy.py`、`agent/llm_client.py`

本文档描述 BlindSpatialAgent 的安全层设计。核心结论：**安全判断不是"模型的事"，而是一组纯函数规则，每一轮主循环必跑，且永远在安全结论之上合并 LLM 决策**。

---

## 1. 设计目标：为什么安全不能依赖大模型

项目目标用户是盲人，安全层要回答的是"现在要不要让人停下"。把这件事交给大模型有三个根本性问题，任何一条都足以致命：

| 理由 | 说明 | 后果 |
| --- | --- | --- |
| 延迟不可控 | LLM 一次多模态调用可能几百毫秒到数秒，且工具调用会多轮累加 | 用户正前方 0.3m 有墙时，等模型想完已经撞上 |
| 可能失败或返回非法结果 | Key 失效、限流、网络抖动、JSON 解析失败、超时报错都会让模型"没回话" | 失败时若只有模型在守安全，就没有人在守 |
| 不可解释 | 模型输出是概率分布的采样，无法证明"它在所有危险场景都判对了" | 无法做形式化审查，也无法逐条覆盖测试 |

因此安全判断被抽成 `agent/safety/risk_rules.py` 里的**确定性纯函数规则表**，由 `agent/safety/safety_engine.py` 的 `SafetyEngine.evaluate()` 每 tick 求值（实测约 0.05 ms）。它只读 `SpatialState` + 事件 + 阈值，不联网、不调 LLM、不写状态。

这条原则对应架构铁律 4（**安全底线不可被模型推翻**）与铁律 1（**大模型不直接读传感器，一切先归一化为 `SpatialState`**）。

---

## 2. 双循环分工图

主循环 `SpatialAgentSystem.step()` 的骨架（`agent/agent_core.py:479`）：

```
 感知 SensorProvider
      ↓
 空间状态融合 StateManager.build()  →  SpatialState（归一化后的唯一事实源）
      ↓
 事件引擎 EventEngine.process()
      ↓
 编排决策 AgentOrchestrator.decide()           ← 安全在这里被求值
      ↓
 执行行动 apply_action()  →  tools.call()（唯一副作用出口）
      ↓
 世界推进 env.advance_user(motion_allowed=action.permits_motion)
```

双循环在 `decide()` 内部交汇：

```
┌─────────────────── 快循环 Fast Safety Loop ───────────────────┐
│ 线程：主线程（每 tick，默认 1 Hz）                              │
│ 职责：安全求值 + 规则基线（确定性兜底）                        │
│ 频率：100% 每轮必跑                                            │
│ 性质：纯计算、不联网、不等 IO、不阻塞                          │
│ 数据：read SpatialState / events / thresholds                  │
│ 输出：SafetyVerdict（level / action / message / triggers）      │
└───────────────────────────────────────────────────────────────┘
                         │  converge
                         ▼
┌─────────────────── 慢循环 Cognitive Loop ────────────────────┐
│ 线程：worker 线程（daemon，事件驱动 + 心跳 5s）                 │
│ 职责：多模态大模型推理 → Action（理解 + 表达）                  │
│ 频率：仅在 IMPORTANT 事件 / 用户提问 / 心跳时触发              │
│ 性质：可能阻塞数秒；结果带 TTL（result_ttl_s = 4.0）           │
│ 数据：主线程打好的 ContextBundle 快照（只读）                  │
│ 输出：Action（可能被 Expression Gate 审查后合并）               │
└───────────────────────────────────────────────────────────────┘

合并顺序（AgentOrchestrator.decide，高 → 低）：
  ① 安全层 EMERGENCY  →  must intervene，直接采用规则基线动作（不交给 LLM 改写）
  ② 认知循环结果       →  若未过期且未 discard，经 Expression Gate 后与基线合并
  ③ 规则基线           →  任何情况下都在跑，确定性兜底
```

关键事实：

- 安全层在**任何 LLM 结果之前**被求值，且**永远**被求值（`orchestrator.py:348` 的 `self.safety.evaluate(...)` 排在最前）。
- LLM 调用通过 `cognitive.submit()` 非阻塞地丢给 worker 线程；本轮行动先由安全层 + 规则基线给出，认知结果在后续轮次被 `poll()` 取用（`orchestrator.py:351`、`agent_core.py:483`）。
- 这正是任务书第八节强制验收（LLM 睡 5 秒，安全循环照常工作）成立的前提。

---

## 3. 风险规则表

规则定义于 `agent/safety/risk_rules.py:DEFAULT_RULES`，共 **10 条**。每条规则是 `SafetyRule(name, level, action, predicate, message, reason, cooldown_s)`。顺序即"同级别优先级"，越靠前越先被选中；命中多条时只保留最高级别的那几条，`SafetyVerdict` 取其中第一条的 `message`/`action`。

`SafetyLevel` 枚举（`risk_rules.py:34`）：`OK / CAUTION / WARNING / EMERGENCY`（值为小写字符串）。
`SafetyAction` 枚举（`risk_rules.py:51`）：`NONE / NOTICE / ALERT / STOP`。

| # | 规则名 | 触发条件（predicate 依据字段） | 输出等级 | 建议动作 | 冷却 s |
| --- | --- | --- | --- | --- | --- |
| 1 | `dropoff_ahead` | `environment.dropoff` 为真（悬空/坑洞） | EMERGENCY | STOP | 1.0 |
| 2 | `collision_imminent` | `risk.level == CRITICAL`，或 `not front_clear and front_distance <= critical_distance_m` | EMERGENCY | STOP | 1.0 |
| 3 | `stairs_ahead` | `environment.stairs` 为真（台阶） | WARNING | ALERT | 6.0 |
| 4 | `front_blocked` | `risk.level == HIGH`，或 `not front_clear and front_distance <= high_distance_m` | WARNING | ALERT | 4.0 |
| 5 | `severe_deviation` | `navigation.off_route` 为真（严重偏航） | WARNING | ALERT | 5.0 |
| 6 | `localization_untrusted` | `not user.valid` 或 `confidence.localization_confidence < min_localization` | WARNING | ALERT | 30.0 |
| 7 | `obstacle_approaching` | 本轮回合出现 `OBSTACLE_APPROACHING` 事件 | WARNING | ALERT | 3.0 |
| 8 | `sensor_map_conflict` | 本轮回合出现 `MAP_SENSOR_CONFLICT` 事件 | CAUTION | NOTICE | 10.0 |
| 9 | `perception_untrusted` | `confidence.perception_confidence < min_perception` | CAUTION | NOTICE | 20.0 |
| 10 | `narrow_passage` | `environment.narrow_passage` 且 `corridor_width is None or <= narrow_width_m` | CAUTION | NOTICE | 8.0 |

要点：

- **EMERGENCY 规则（#1、#2）豁免冷却**（`safety_engine.py:125`）：冷却防的是噪音，不防危险。dropoff / 碰撞这类"不可恢复"事故，每条命中都必须立即判出。
- 下划线字段（`critical_distance_m` / `high_distance_m` / `narrow_width_m` / `min_localization` / `min_perception`）全部来自配置，规则函数不写死数值。
- 规则函数均只读 `state` / `events`，不抛危险异常：单条规则求值异常会被 `safety_engine.py:121` 捕获并跳过，不影响整层。
- 新增规则必须同时补自检用例，否则等于死代码（`risk_rules.py:187` 上方注释）。

---

## 4. 四级安全等级与行动处置

`SafetyVerdict` 上有两个布尔属性决定处置：

- `intervene`（`safety_engine.py:58`）：`level is EMERGENCY` → 必须立即干预（阻止继续前进）。
- `should_alert`（`safety_engine.py:63`）：`level in (WARNING, EMERGENCY)` → 需要让用户知道。

| 等级 | intervene | 是否放行前进（permits_motion） | 物理动作由谁决定 | 提示话术从哪来 |
| --- | --- | --- | --- | --- |
| `OK` | 否 | 不影响（沿用决策） | 决策层 | 无。且 `StepResult.safety` 直接为 `None`（见第 6 节） |
| `CAUTION` | 否 | 放行（不拦） | 决策层 | 安全结论为建议性（NOTICE）。`SafetyVerdict.message` 进入上下文与诊断，不强制播报；是否出声由规则基线/认知层按各自闸门决定 |
| `WARNING` | 否 | 放行（"允许继续走"） | 决策层 | 同上（ALERT，建议性） |
| `EMERGENCY` | **是** | **不放行**（`permits_motion=False`） | 编排器直接采用规则基线（基线独立产出 WAIT/critical 动作） | 由规则基线 `RuleDecisionEngine` 从同一几何事实独立生成（见下） |

EMERGENCY 路径在 `AgentOrchestrator.decide`（`orchestrator.py:379`）：

```python
if verdict.intervene:
    action = baseline          # 直接采用规则基线，不交给 LLM 改写
    llm_used = False
```

规则基线（`agent/decision.py:89`）在 `risk.level == CRITICAL` 时独立产出 `ActionType.WAIT` + `urgency="critical"` + 消息 `请停下，前方{d:.1f}米有障碍`。因此当安全层判 EMERGENCY 时，**规则基线在同样条件下本来就会拦**，编排器采用基线动作即可——安全层的价值是"保证拦得住且不等 LLM"，而不是换一套话术（换话术只会让同一场景的播报前后不一致）。

安全层的话术来源说明：`SafetyVerdict.message` 由命中规则的 `message` 闭包生成，但编排器并不把它直接写进最终 Action。它的去向有两处：

1. 暴露给认知层：`SafetyEngine.last_verdict` 经 `ContextBuilder` 注入提示词，让模型知道"安全层刚刚怎么判的"，而不是让模型自己从环境字段重新猜。
2. 诊断落盘：`StepResult.safety`（仅非 OK 时存在）与录制中的 `actions.jsonl` / `llm.jsonl`。

---

## 5. 阈值与配置

安全阈值集中在 `config/config.yaml` 的 `safety:` 段，由 `SafetyThresholds.from_cfg()`（`risk_rules.py:75`）读取。所有数值均来自配置，代码不写死。

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `safety.critical_distance_m` | `null` | 碰撞临界距离。**`null` = 沿用 `agent.risk.critical_distance_m`（0.35）** |
| `safety.high_distance_m` | `null` | 高风险距离。**`null` = 沿用 `agent.risk.high_distance_m`（0.70）** |
| `safety.narrow_width_m` | `0.9` | 可通行宽度低于该值算"通道变窄" |
| `safety.min_perception_confidence` | `0.5` | 感知置信度低于该值 → CAUTION（记一笔，不打断用户） |
| `safety.dropoff_is_emergency` | `true` | 坠落风险一律按最高优先级（不可恢复事故） |
| `safety.stairs_is_warning` | `true` | 台阶按 WARNING 处理（可慢行通过） |
| `safety.camera_loss_is_safety` | `false` | 摄像头断流**不算**安全问题，不得因此停住用户 |

`critical` / `high` 的口径与 `null` 兜底（铁律之一，读取处必须兜底）：

- `agent.risk.critical_distance_m = 0.35`、`agent.risk.high_distance_m = 0.70` 是融合层风险判级（`RiskLevel.CRITICAL/HIGH`）使用的口径。
- `safety.critical_distance_m` / `safety.high_distance_m` 默认为 `null`，语义是"沿用 `agent.risk` 的口径"。
- **读取处必须显式兜底**：`SafetyThresholds.from_cfg` 写成
  ```python
  crit = safety.get("critical_distance_m")
  high = safety.get("high_distance_m")
  critical_distance_m=float(crit if crit is not None else risk["critical_distance_m"])
  ```
  若直接 `float(None)` 会在启动瞬间 `TypeError` 导致进程起不来。
- `min_localization` 沿用 `agent.confidence.min_localization`（= `0.4`）：定位置信度低于该值 → WARNING。
- `camera_loss_is_safety = false`：摄像头断流只降级视觉（方向感/定位仍可支撑继续导航），**不进安全层、不得停住用户**。这正是验收 Demo 的要求；若误把它当安全问题，盲人会因一次网络抖动被无故钉在原地。

---

## 6. 与其他层的合并语义

### 铁律 4：安全底线不可被模型推翻

合并发生在两个地方：

- 编排器（`orchestrator.py:379`）：`verdict.intervene`（仅 EMERGENCY）为真时，直接采用规则基线动作，LLM 结论完全不参与。
- 决策器（`agent_core.py:245` `_merge_with_safety`）：当规则基线判定 `ActionType.WAIT` 且 `urgency == "critical"`（即规则自己已判碰撞风险），而 LLM 结论却 `permits_motion == True` 时，规则覆盖 LLM：
  ```python
  if rule.action_type == ActionType.WAIT and rule.urgency == "critical" and llm.permits_motion:
      return rule.model_copy(update={"reason": f"安全覆盖模型决策（模型建议 {llm.action_type.value}）：{rule.reason}"})
  ```
  即：**规则判定"立即停下"时，模型不能让用户继续走。**

### 铁律 5：表达层的拦截不得改变物理层的后果

表达审查 `InteractionPolicy.gate()`（`interaction_policy.py:235`）只决定"说不说 / 说多长 / 走哪个通道"，它的输出经 `apply()`（`interaction_policy.py:354`）只改 `message` / `channel` / `priority`，**绝不改 `action_type` 与 `permits_motion`**。因此：

- 紧急停行（EMERGENCY → 基线 WAIT，`permits_motion=False`）即使被表达层判定为"本次不播报"（如 `too_frequent` / `user_speaking`），**用户仍然被物理停住**——"防刷屏"只影响声音，不影响刹车。这正是铁律 5 的要求。
- 反向危险（`interaction_policy.py:90` 的 `DISCARD_CODES`）：当判定为 `expired` 或 `low_confidence` 时，结论本身已不可信，**必须整条丢弃并退回规则基线**（`orchestrator.py:407`、`agent_core.py:163`），不能只清空 `message`。否则一个 `permits_motion=False` 的过期"安全告警"会被保留，用户被一条没人听见、也没人复核的结论永久钉在原地。

具体例子：前方 0.3m 有墙，安全层判 EMERGENCY，基线动作 `WAIT(permits_motion=False)`。若这一轮恰好"距上次播报不足 4s"，表达层把 `message` 清空（不念第二遍），但 `permits_motion` 仍为 `False`——用户照样被拦下。物理后果（停）与表达后果（不念）解耦，互不影响。

---

## 7. 故障与降级

### 7.1 LLM 熔断（必须用 `usable` 而非 `available`）

`LLMClient`（`llm_client.py`）区分两个可用性：

- `available`（`llm_client.py:91`）：有 key 且有模型名。
- `usable`（`llm_client.py:101`）：**已配置且未熔断**（`available and not circuit_open`）。

调用方（编排器、决策器）一律用 `usable`。若误用 `available`，Key 失效时主循环会以 1 Hz 无限重试：白白消耗配额、拖慢实时性、刷爆日志（实测 600 轮稳定性测试因此跑了数分钟并被误判为"卡死"）。

熔断冷却时长（`llm_client.py:40`）：

| 失败类型 | 冷却（秒） | 常量 |
| --- | --- | --- |
| 认证失败（Key 无效 / 401 / 403） | 600 | `COOLDOWN_AUTH` |
| 接口路径或模型名错误（404 / unknown model） | 600 | `COOLDOWN_NOT_FOUND` |
| 限流或额度不足（429 / quota） | 30 | `COOLDOWN_RATE_LIMIT` |
| 网络 / 服务端异常（兜底） | 15 | `COOLDOWN_NETWORK` |

解除条件：**一次成功即解除**（`_note_success`，`llm_client.py:108`）—— 连续失败计数清零、熔断时间归零。

### 7.2 时间预算

| 参数 | 值 | 说明 |
| --- | --- | --- |
| `max_retries` | `0` | 配置显式 0。**OpenAI SDK 默认是 2**：一次"30s 超时"最坏变 3 次尝试 ≈ 90s，叠加工具调用轮次后单轮 step 最坏 360s。对实时系统不可接受，重试改交给上层熔断。 |
| `total_deadline_s` | 配置 20（代码兜底 `timeout_s * 2`） | 单轮决策总预算，含工具调用来回。超了就放弃本次推理，让规则基线兜底。 |

**总闸门必须放在发起请求之前**：`chat_multimodal` 的工具调用 `while` 循环（`llm_client.py:183`）在每次 `client.chat.completions.create(...)` 之前先检查：

```python
if time.time() - started > self.total_deadline_s:
    ... 放弃本次推理，返回已解析的 last_content 或 None
```

理由：工具调用是多轮循环，各轮 `timeout` 会累加；若不在入口处设总闸门，多轮重试会累加到分钟级。自检用例 62 钉死这一点——把 `total_deadline_s` 设为 `-1.0` 时，断言"超预算后**一次请求都没发出**"（`called["n"] == 0`）。

### 7.3 非法 JSON 与超时

- 模型返回无法解析为 JSON：`_parse` 返回 `None`（`llm_client.py:326`），本轮降级规则基线；不抛异常。
- 超时被 `max_retries=0` + `total_deadline_s` 双重约束；超时异常在 `chat_multimodal` 的 `except` 里被捕获，`_note_failure` 登记熔断，返回 `None`，规则基线接管。
- 任何失败都返回 `None`，**绝不向上抛异常打断主循环**（铁律 6 的"规则路径必须可证明不变"在此体现为"LLM 路径失败不影响快循环"）。

---

## 8. 并发模型与"LLM 卡住不丢安全"的证明

### 单写者 + 快照（铁律之一）

- 主线程是**唯一**修改 `WorldModel` / `StateManager` / `memory` / 模拟器的线程。
- 交给认知 worker 线程的是 `state.model_copy(deep=True)` 的**只读快照**（`orchestrator.py:184`），跨线程传递的全是字符串/字节/字典副本，不需要业务锁。
- 唯一例外是 function calling 的工具执行：`AgentTools.call()` 内部持 `RLock`（`agent_core.py:404`），**锁只在执行的几毫秒内持有，绝不跨越网络等待**，所以 LLM 再慢也挡不住快循环。

### 为什么"只能靠真线程"

单线程里无论把 LLM 超时调多小，`time.sleep()` 与网络阻塞都释放 GIL，但主循环在那段时间里是停的。"停下来的安全层"对盲人用户等于不存在——3 秒够走 3 米。所以 LLM 必须离开主线程（见 `orchestrator.py` 的 `CognitiveLoop`）。

### 实测证明（验收 S08）

`tests/acceptance.py:592` `s08_llm_stall`：把 `LLMClient` 换成 `SlowVLM(5.0)`（每次调用睡 5 秒），放置 0.20m 障碍，连续跑 5 轮 `step(1.0)`。断言：

```python
expect(wall < 1.0, ...)                       # 5 轮 step 共花 < 1.0s
expect(all(r.safety["level"] == "emergency" for r in results), ...)  # 每轮都判 emergency
expect(all(not r.action.permits_motion for r in results), ...)        # 紧急不放行
expect(system.safety.evaluations >= 6, ...)   # 快循环每轮求值
expect(system.orchestrator.cognitive.thinking, ...)  # 此刻 LLM 仍在工作线程睡眠（证真并发）
expect(slow.calls <= 2, ...)                   # 睡眠期间不反复发起调用
```

实测数字：LLM 睡 5 秒时，5 轮 `step` 仅耗时 **0.00~0.01s**，安全层每轮照常判出 `emergency`，`permits_motion` 始终为 `False`。

---

## 9. 验收对照

| 任务书要求 | 对应实现 | 证明测试 / 断言 |
| --- | --- | --- |
| **Gate 6**：LLM 失败不会让系统崩 | `chat_multimodal` 全路径 `try/except` 返回 `None`；`decide()` 用 `usable` 而非 `available`；熔断避免 1 Hz 无限重试 | `tests/acceptance.py` **S08**（LLM 卡 5s → 主循环 < 1s 且照常推进）；`tests/selftest.py` **62**（无 Key 时 `usable=False and available=False`，不重试） |
| **Gate 7**：Safety Loop 独立于 LLM | 安全层在 `orchestrator.decide` 内排第一、每轮必跑；认知循环在独立 worker 线程，非阻塞提交 | `tests/acceptance.py` **S08** 断言 `safety.evaluations >= 6` 且 `cognitive.thinking` 为真（证真并发）；`tests/selftest.py` **61**（认知循环异步产出并被取用、过期丢弃） |

补充说明：

- Gate 6 的另一半由铁律 4 保证——即便 LLM 正常返回了"错误/危险"的结论，安全层 EMERGENCY 仍直接采用规则基线，模型推不动物理层。
- 自检用例 47（分层铁律）扫描 `spatial/state_manager.py`（融合层）、`agent/tools.py`（工具层）、`agent/agent_core.py`（Agent 核心）、`recording/session_player.py`（回放器）、`recording/session_recorder.py`（录制器），以及 `sensors/base.py` / `sensors/__init__.py`，确认这些模块均无顶层 `import simulator` 语句；并反向断言 `sensors.simulated` 只出现在装配处 `agent_core.py`。这是铁律 7 的硬验收。
- 自检用例 62（大模型调用预算）断言 `max_retries == 0` 与 `total_deadline_s > 0`，并验证超预算后零请求发出——与第 7.2 节一致。

---

## 附：关键类型与文件索引

| 类型 / 函数 | 位置 | 作用 |
| --- | --- | --- |
| `SafetyEngine.evaluate` | `agent/safety/safety_engine.py:107` | 快循环入口，每 tick 一次 |
| `SafetyVerdict` | `agent/safety/safety_engine.py:46` | 一轮判定（`intervene` / `should_alert` / `as_dict`） |
| `SafetyLevel` / `SafetyAction` | `agent/safety/risk_rules.py:34/51` | 四级等级 / 四类建议动作 |
| `DEFAULT_RULES` | `agent/safety/risk_rules.py:187` | 10 条默认规则 |
| `SafetyThresholds.from_cfg` | `agent/safety/risk_rules.py:75` | 阈值读取与 `null` 兜底 |
| `AgentOrchestrator.decide` | `agent/orchestrator.py:335` | 安全 / 认知 / 基线合并 |
| `SpatialAgent._merge_with_safety` | `agent/agent_core.py:245` | 规则覆盖危险 LLM 结论 |
| `InteractionPolicy.gate` / `apply` | `agent/interaction_policy.py:235/354` | 表达审查（不改物理后果） |
| `LLMClient.usable` / `chat_multimodal` | `agent/llm_client.py:101/154` | 熔断与总预算闸门 |
