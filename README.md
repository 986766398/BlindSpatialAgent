# BlindSpatialAgent —— 视障空间智能体模拟系统

面向盲人室内导航的 **Embodied Spatial Agent** 最小原型。当前阶段**不接任何真实硬件**：
用模拟器产生空间/导航/传感器数据，用 iPhone（或浏览器网页）提供唯一真实输入 —— 摄像头画面。

> 这不是聊天机器人。它是一个具备
> **空间状态感知 → 事件与风险判断 → 多模态理解 → 安全裁决 → 行动决策 → 语音表达** 的智能体。

- 版本：`v0.3.0`（配置中心 `config/config.yaml` 的 `system.version`）
- 规模：约 21,900 行 Python；自检 **75 项**、验收场景 **10 项**、验收 Demo **七拍**、前端冒烟 **44+ 项**
- 文档索引见文末「文档」。

## 系统循环（与机器人控制循环同构）

```
感知 Perception（模拟器 / 未来真实硬件）
  ↓
传感器适配 Sensor Adapter（统一为结构化观测）
  ↓
状态融合 State Fusion → SpatialState（Pydantic 结构化状态，带 timestamp/source/confidence）
  ↓
空间世界模型 Spatial World Model（持续空间记忆 + 地图/可通行性）
  ↓
事件与风险 Events / Risk / Uncertainty
  ↓
┌─ 安全快循环 Fast Safety Loop（每轮必跑，纯计算，不碰网络）
└─ 认知慢循环 Cognitive Loop（大模型，工作线程，可缺席）
  ↓
表达层 Interaction Policy（闸门：防刷屏 / 最小间隔 / 长度上限）
  ↓
行动执行 Action Executor（SPEAK / CONTINUE / REPLAN / WAIT / ASK_USER）→ 是否允许用户前进
  ↓
下一轮（New Perception）
```

## 七条不可动摇的架构铁律

1. **大模型永远不直接读传感器。** 所有数据先归一化为 `SpatialState`，模型只能看到被允许看到的结构化字段。
2. **换真硬件只改数据来源。** 接 UWB / LiDAR / IMU / UE5 时只替换 `sensors/` 的实现与**装配处的唯一一行**，`agent/` 与 `spatial/` 一行不动。
3. **所有副作用必须经过工具层。** 播报、提问、重规划一律走 `tools.call()`，连规则兜底决策也不例外 —— 模型无法绕过参数校验与安全策略。
4. **安全底线不可被模型推翻。** 规则引擎判定「立即停下」时，即使模型建议继续前进也会被覆盖。
5. **表达层的拦截不得改变物理层的后果。** 防刷屏只能决定「说不说」，不能把「必须停」变成「继续走」。
6. **新能力只加在非确定路径上。** 依赖大模型的判断只作用于模型输出；确定性规则路径必须可证明不变（用行为快照逐轮比对）。
7. **分层铁律。** 除 `SpatialAgentSystem.__init__` 这一处装配点外，任何模块都不得 `import simulator`（含 `recording/`，回放器靠鸭子类型假扮传感器）。该项由自检用例 47 全仓扫描强制。

> 铁律 2 与 7 合起来保证了「换硬件不改 Agent」不是口号：**唯一允许认硬件的地方，就是装配处那一行。**

## 快速开始

```bash
cd BlindSpatialAgent
source .venv/bin/activate          # 或用 .venv/bin/python 直接调用
python main.py --selftest          # 1) 先跑自检：75 项，全绿再往下
python main.py                     # 2) 终端实时运行模拟循环（Ctrl+C 退出）
python main.py --serve             # 3) 启动服务，浏览器打开 http://127.0.0.1:8000/
python main.py --demo              # 4) 打印一个完整 SpatialState，校对字段结构
```

验收（v0.3 收尾）用这三条：

```bash
python main.py --acceptance        # 10 个验收场景（逐条对应任务书 Gate 1~10）
python main.py --acceptance-demo   # 七拍验收 Demo（任务书第二十九节剧本，一次连续会话）
python main.py --record --session-id demo   # 录制会话，再用 --mode replay 回放比对
```

### 启动服务的三种方式

**方式一：双击启动器（最省事）** —— 在 Finder 里双击项目根目录的 `启动服务.command`，服务起来后会自动打开浏览器面板。换端口：终端执行 `./启动服务.command 8010`。停止：在弹开的终端窗口按 `Ctrl+C`，或直接关掉窗口。

**方式二：一行命令**（终端）

```bash
cd /Users/jingtaozhang/WorkBuddy/2026-09-17-16-21-21/BlindSpatialAgent && NO_PROXY=127.0.0.1,localhost,::1 .venv/bin/python main.py --serve
```

> `NO_PROXY` 是本机设了 `HTTP_PROXY` 时必须的：否则浏览器/curl 访问 `127.0.0.1` 会被代理以 502 拒绝。启动器已自动处理。

**方式三：VSCode 按 `F5`** —— 见下。

VSCode 里按 `F5` 有 4 个调试配置（`.vscode/launch.json`）：

| 配置名 | 作用 |
| --- | --- |
| `BSA: 服务模式（实时面板）` | **看实时面板选这个** —— 起服务，浏览器开 `http://127.0.0.1:8000/` |
| `BSA: 服务模式（端口 8010）` | 8000 被占用时用，面板改开 `http://127.0.0.1:8010/` |
| `BSA: 自检（75 项）` | 离线自检，约 12 秒 |
| `BSA: 终端演示（60 轮）` | 不起服务，只在终端看 Agent 边走边说话 |

> 停止服务：点调试工具栏的红色方块（或终端里 `Ctrl+C`）。

### 实时面板怎么看（`python main.py --serve` 之后）

浏览器打开 **`http://127.0.0.1:8000/`**，页面分三列：

| 区域 | 看什么 |
| --- | --- |
| 左列 **小地图** | 蓝点=当前位置与朝向，绿线=A\* 规划路径，红点=动态障碍，方块/圆点=静态物体（**矩形=区域型障碍**，如施工围挡/柜墙） |
| 中列 **行动流** | 模型每次决策的原文 + `llm`/`rule` 来源徽章 + 紧急度；这是最能看出"它在思考"的地方 |
| 中列 **导航/环境/风险** | 当前指令、剩余距离、前方净空、风险等级与依据 |
| 右列 **摄像头画面** | 本机摄像头推流 或 **服务端最新帧**（iPhone App 推流走这条，左上角标注「服务端画面 · X.Xs 前」）|
| 右列 **用户提问** | 打字当作"用户语音"提交，模型会结合画面回答 |
| 右列 **大模型诊断** | 真实打一次请求，暴露原始 HTTP 码/延迟/错误正文；`视觉自检` 会附带测试图验证看图能力 |
| 右列 **控制** | 暂停 / 恢复 / 重置（重置让 Agent 从起点重走）|

**手动驾驶（WASD）**：面板上有「手动驾驶」开关，打开后用 `W`/`A`/`S`/`D` 直接驱动虚拟用户移动，
用来人工制造场景（走到某处再放开让 Agent 接管）。实现上外部线程只往队列里追加带时间戳的按键记录，
时间积分由仿真主循环完成，因此不破坏「单写者」并发约束。

⚠️ 面板里显示的 `摄像头画面` 有两路来源，别混淆：
**「开启摄像头推流」= 用本机浏览器摄像头**；**iPhone App 推流 = 服务端帧**（自动显示，无需点按钮）。
详见下文「用 App 推流时，网页上『摄像头画面』一片黑？」。

### 一键自验（改完代码就跑这个）

上面三步手工敲太啰嗦，已固化成脚本。**每次改完代码，跑一条命令即可**：

```bash
bash scripts/dev_check.sh            # 全流程：自检 → 实时运行 → 起服务并打开浏览器
bash scripts/dev_check.sh --quick    # 只跑 75 项自检（小改动，约 12 秒）
bash scripts/dev_check.sh --no-serve # 自检 + 实时运行，不起服务
```

**改完测试页后，另外跑一次 UI 冒烟测试**（无需浏览器，用 Node + DOM 桩跑真实 JS）：

```bash
.venv/bin/python tools/ui_smoke_test.py     # 需先起服务；44+ 项断言，覆盖各面板是否真的被填充
```

**改完摄像头链路后，跑端到端回归**：

```bash
.venv/bin/python tools/e2e_test.py          # 自动起服务 + 假大模型，逐环断言到"模型确实带了图"
```

**改完地图 / 障碍布局后，先画一张图再看**（比跑测试更快发现"处处能过但处处要绕"）：

```bash
.venv/bin/python tools/render_map.py        # 输出 docs/map_check.png（分区/门洞/路线/区域障碍）
```

行为约定：

- **自检是硬门槛**：`--selftest` 若失败，脚本立即 `exit 1`，不会继续往下跑（避免"带病上线"）。
- 第 3 步会后台 `sleep 3` 后自动 `open http://127.0.0.1:8000/`，随即前台常驻服务，`Ctrl+C` 停止。
- 脚本已内置 `NO_PROXY=127.0.0.1,localhost`；本机若设了 `HTTP_PROXY`，自检里的 REST/WebSocket 项不会被代理以 502 误伤。
- 可用环境变量覆盖：`BSA_PORT`（默认 8000）、`BSA_TICKS`（默认 20）、`BSA_SEED`（默认 42）。

> ⚠️ 本机设了 `HTTP_PROXY` 时，**任何**连本机端口的命令都要带 `NO_PROXY=127.0.0.1,localhost,::1`；
> `curl` 另外加 `--noproxy '*'`，否则会被代理返回 502。

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--ticks N` | 最多运行 N 轮 |
| `--seed N` | 固定随机种子，结果可复现 |
| `--no-obstacles` | 关闭动态障碍（做纯导航验证） |
| `--no-llm` | 禁用大模型，强制规则决策 |
| `--brief` | 精简输出，只打印动作行 |
| `--fast` | 不按真实时间节拍，全速跑完 |
| `--port P` | 服务端口 |
| `--selftest` | 运行 75 项自检 |
| `--acceptance` | 运行 10 个验收场景（Gate 1~10） |
| `--acceptance-demo` | 运行七拍验收 Demo（可加 `--demo-out <路径>` 落盘转写） |
| `--record [DIR]` | 开启录制（会话目录），默认写到 `config.recording.root` |
| `--session-id ID` / `--record-notes TXT` | 录制会话 id / 实验备注 |
| `--keep-images` / `--no-images` / `--max-images N` | 录制是否保存画面、画面张数上限 |
| `--mode replay` / `--session ID` | 回放指定会话 |
| `--replay-out DIR` / `--replay-llm` / `--replay-ticks N` | 回放结果另存、回放时启用大模型、回放轮数上限 |

## 接入真实多模态大模型

**推荐做法：填进项目根目录的 `.env`**（已被 `.gitignore` 忽略，密钥不进版本库）

```bash
cp .env.example .env        # 首次：从模板复制一份
code .env                   # 打开，把 BSA_LLM_API_KEY= 后面填上你的 Key，保存即可
python main.py              # 下次运行自动读取，无需 export、无需重启任何东西
```

`.env` 内容形如（下面以阿里云百炼 **Token Plan 套餐版**为例）：

```dotenv
BSA_LLM_API_KEY=sk-sp-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
BSA_LLM_BASE_URL=https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
BSA_LLM_MODEL=kimi-k2.5                                              # 必须带视觉能力
```

> ⚠️ **百炼有三类 Key，各自绑定不同的 Base URL，必须配套使用，混用一律 401：**
>
> | Key 前缀 | 套餐类型 | 专属 Base URL |
> | --- | --- | --- |
> | `sk-` | 按量付费 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
> | `sk-sp-` | Token Plan | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` |
> | `sk-ws-` | Coding Plan | `https://coding.dashscope.aliyuncs.com/v1` |
>
> 前缀必须是**小写**；写成 `Sk-sp-` 即使地址正确也会 401。
> 判断套餐**只看前缀**，不要对 Key 本身做其它解读（套餐 Key 含点号属正常）。
> 文档：<https://help.aliyun.com/zh/model-studio/environment-variable-hk>

> **看图必需**：本项目要「图片 + SpatialState → 导航建议」，模型必须支持视觉。
> Token Plan 实测可用：`kimi-k2.5`（≈1.9s，默认）、`qwen3.6-plus`（≈2.9s）、`qwen3.7-plus`；
> `qwen3.8-max` 也能看图但延迟约 40s，不适合 1Hz 实时循环。
> ⚠️ `deepseek-v3.2` 等纯文本模型看不到摄像头画面。

**优先级：真实环境变量 > `.env` > `config/config.yaml`**。
临时切换（例如试另一个模型）依旧可以命令行覆盖，`.env` 不会把它压回去：

```bash
BSA_LLM_MODEL=qwen3.6-plus python main.py    # 本次运行生效，不改动 .env
```

> 不建议把 Key 直接写进 `config/config.yaml` 的 `llm.api_key`：该文件受 git 跟踪，一旦提交密钥即泄露。
> `.env` 加载器是零依赖手写实现（`config/loader.py` 的 `_load_dotenv`），未引入 `python-dotenv`。

### 填 Key 老是不生效？用体检工具（强烈推荐）

Key 明明是对的却一直 401，通常是下面三个坑之一，**本工具都能当场挡下并自动修正**：

| # | 坑 | 症状 | 工具的应对 |
| --- | --- | --- | --- |
| 1 | **粘贴断行** | `.env` 里只剩前半截（本项目出现过只剩 6 字符） | 去掉所有空白字符，自动接回 |
| 2 | **前缀大小写** | `Sk-sp-` 写成大写 → 即使地址正确也 401 | 自动纠正为小写 `sk-` |
| 3 | ★**套餐与 Base URL 不配套**★ | 用通用地址调套餐 Key → 报错长得像「Key 无效」，极易误判成 Key 复制错 | 识别前缀 → 自动改用该套餐专属地址 |

```bash
.venv/bin/python tools/set_llm_key.py                    # 交互式粘贴（推荐，输入不回显）
.venv/bin/python tools/set_llm_key.py --key "sk-xxxx"    # 直接给
.venv/bin/python tools/set_llm_key.py --key "sk-xxxx" --check   # 只验证，不写入
.venv/bin/python tools/set_llm_key.py --show             # 只看当前配置（脱敏）
.venv/bin/python tools/set_llm_key.py --models           # 列出该 Key 可用的模型
```

它会：**①** 归一化（去空白/引号 + 纠正前缀大小写）；
**②** 按前缀识别套餐并自动配对 Base URL；
**③** 按格式体检（只在真的可疑时告警）；
**④** 真的向 `base_url` 发一次最小请求，用 HTTP 状态码说话；
**⑤** 只在验证通过后才写入 `.env`（旧文件自动备份为 `.env.bak`）。

> 已在运行的 `--serve` 进程**不会热加载** `.env`，改完 Key 需要重启服务。
> 页面上的「大模型诊断」面板也能一键触发同样的真实请求，直接在浏览器里看结果。

未配置 Key、网络失败或模型输出非法 JSON 时，系统**自动降级到规则决策**并继续运行，
不会停摆、不会抛异常。

**填完 Key 后建议这样验证**（确认真的走通了模型，而不是静默降级）：

```bash
python main.py --check      # 第 6 行应显示 key=已配置
python main.py --ticks 5    # 观察决策来源：大模型 / 规则
```

## iPhone 摄像头接入

### 方式 A：原生 iOS App（推荐，真机效果最好）

`ios/BSACameraStreamer/` 是一个零依赖的 SwiftUI App（Xcode 工程已就绪）：

```bash
open ios/BSACameraStreamer/BSACameraStreamer.xcodeproj
# 选真机 → Run（需在 Signing 里选自己的 Team）
```

App 内填「电脑的局域网 IP + 端口」，默认 `8000`，路径 `/ws/camera`，即可开始推流。
已声明的权限：`NSCameraUsageDescription`、`NSLocalNetworkUsageDescription`（缺任一项都会在真机上崩）。

已验证编译通过（`xcodebuild -sdk iphonesimulator` → BUILD SUCCEEDED），
且客户端协议与服务端逐条对齐：`hello` 握手 / `ping`-`pong` 心跳 / `ack` 确认 / 断线指数退避重连。

### 方式 B：浏览器网页（无需 Xcode，快速验证）

```bash
python main.py --serve
# 本机： http://127.0.0.1:8000/
# iPhone：同一 WiFi 下用 Safari 打开 http://<你的局域网IP>:8000/
```

三种推流方式：① 调用本机摄像头持续推流；② 选择/拍摄一张图上传；③ 发送合成测试帧。
> Safari 只在 **HTTPS 或 localhost** 下允许网页调用摄像头。用 http 访问局域网 IP 时请用方式 ②。

#### 用 App 推流时，网页上「摄像头画面」一片黑？

**这不是故障。** 网页上那个面板有三条互斥的显示路径，优先级从高到低：

| 路径 | 触发条件 | 面板显示 |
| --- | --- | --- |
| 本机浏览器推流 | 点了「开启摄像头推流」 | `<video>` + LIVE 角标 |
| **服务端画面** | 服务端有新鲜帧（iPhone App 推流 / 上传） | 画面 + 左上角「服务端画面 · 0.4s 前」 |
| 空态 | 服务端没有任何帧 | 「尚未注入画面」 |

- 用 **iPhone App** 推流时走的是第 2 条：画面经 `GET /api/frame` 轮询显示，
  与「开启摄像头推流」（第 1 条，用的是浏览器自己的摄像头）互不影响。
- 「开启摄像头推流」按钮在**没有 `getUserMedia` 的环境里会被置灰并给出原因**，
  此时用 App 推流或「上传图片」即可，功能不受影响。
- 命令行自查（最权威）：
  ```bash
  curl -s -o /tmp/f.jpg -w "%{http_code} %{size_download}\n" --noproxy '*' http://127.0.0.1:8000/api/frame
  .venv/bin/python tools/verify_live_camera.py     # exit 2 = 手机没在推流
  ```

#### 画面延迟大（手机转了，网页要等一会才动）？

端到端延迟 = **手机出帧间隔** + 服务端处理 + **网页刷新间隔**。逐段排查：

| 环节 | 位置 | 默认 | 怎么调 |
| --- | --- | --- | --- |
| ① 手机出帧 | iOS App「帧率」选择器 | **5.0 fps**（可选 0.5/1/2/3/5） | App 里直接改，**即时生效**；改前先确认不是停在 1 fps |
| ② 上行限速 | `config.yaml: camera.max_fps` | `8` | 见下方「必须留余量」 |
| ③ 网页刷新 | `api/test_page.html: FRAME_REFRESH_MS` | `250` ms | 想更跟手可调 150；想省资源可调 400 |

**① 才是主要瓶颈**：1 fps 意味着画面每秒才更新一次，只这一项就有约 1s 延迟，
再怎么优化网页也没用。量一下当前实际帧率：

```bash
# 8 秒内 frame_id 涨了多少，就是实际 fps
curl -s --noproxy '*' http://127.0.0.1:8000/api/camera | python3 -c "import sys,json;print(json.load(sys.stdin)['snapshot']['frame_id'])"
```

⚠️ **② 必须比客户端帧率留余量（这是踩过的坑）**：
节流判据是 `now - 上次接受 < 1/max_fps`，而 `_last_accept` **只在该帧被接受时才更新**。
若 `max_fps` 也写 5，客户端定时器只要有一次微小负抖动（间隔 0.199s < 0.200s），
该帧就被判超速丢弃，下一帧要等到 0.398s 才被接受 ⇒ **丢帧率放大**。
实测（同一客户端 @5 fps 各发 40 帧）：

| `max_fps` | 接受 | 被节流 | 有效帧率 |
| --- | --- | --- | --- |
| `8`（留余量） | **40** | **0** | **100%** |
| `5`（无余量） | 25 | 15 | 62% |

所以配置里取 `8` 而不是刚好等于 App 的最大档位。

### `/ws/camera` 协议（服务端与 iOS App 共用）

| 方向 | 内容 | 说明 |
| --- | --- | --- |
| 下行 | `{"type":"hello", ...}` | 连接即发，带 max_fps / 缩放 / 编码参数 |
| 上行 | 二进制 JPEG 帧 | 唯一的数据帧，不加任何封装 |
| 上行 | `{"type":"hello","device":"iPhone"}` | 客户端自我介绍 |
| 上行 | `{"type":"ping"}` / `{"type":"status"}` | 心跳 / 查询服务端接收统计 |
| 下行 | `{"type":"pong"}` / `{"type":"ack", ...}` | ack 每帧回一次，供 App 显示「服务器确认帧数」 |

## 目录结构

```
BlindSpatialAgent/
├── 启动服务.command             # 一键启动器：双击即起服务并打开浏览器面板（macOS）
├── main.py                     # 启动入口：终端循环 / --serve / --demo / --selftest / --acceptance[-demo]
├── requirements.txt
├── .env.example                # 密钥模板（可提交）；复制成 .env 后填自己的 Key
├── .env                        # 本地密钥（.gitignore 已忽略，不进版本库）
├── config/
│   ├── config.yaml             # 全局配置中心（地图/传感器/阈值/感知/表达/录制，数据驱动）
│   └── loader.py               # 唯一配置读取入口（支持环境变量覆盖 + .env 解析）
├── spatial/                    # 状态契约层
│   ├── spatial_state.py        #   SpatialState 及全部子结构（Pydantic v2）
│   ├── state_manager.py        #   融合入口：调用各 provider → SpatialState
│   └── world_model.py          #   短期空间记忆：轨迹/障碍记忆/已探索区域/打转检测
├── sensors/                    # 传感器适配层（★换硬件的唯一接口面）
│   └── base.py                 #   SensorProvider / CameraProvider / WorldStepper 等 Protocol
├── simulator/                  # 感知前端（未来换成真硬件；★唯一被允许 import 的地方是装配处）
│   ├── map_simulator.py        #   占用栅格 + 分区 + 带安全间隙的 A*
│   ├── navigation_simulator.py #   全局路线 + 局部重规划 + 用户沿路线行走
│   ├── obstacle_simulator.py   #   动态障碍（椅子/行人/箱子/通道变窄）
│   └── sensor_simulator.py     #   UWB/IMU/LiDAR 噪声与丢帧
├── fusion/                     # 状态融合层
│   ├── state_fusion.py         #   多源观测 → 统一状态
│   ├── confidence.py           #   置信度估计（定位/感知）
│   └── freshness.py            #   数据新鲜度（fresh/stale/expired）
├── world_model/                # 空间世界模型（持续记忆，不是每帧重算）
│   ├── world_model.py
│   ├── object_memory.py        #   物体持久记忆（evict_after_s 决定"多久算还在"）
│   ├── semantic_world.py       #   语义物体（门/椅子/绿植…）
│   ├── affordance_world.py     #   可通行性/净空
│   ├── spatial_memory.py       #   已探索区域
│   └── trajectory_memory.py    #   轨迹与打转检测
├── maps/                       # 地图接口（度量层 / 语义层 / 可通行层 / 导航图）
│   ├── map_provider.py
│   ├── metric_map.py
│   ├── navigation_graph.py
│   ├── semantic_map.py
│   └── affordance_map.py
├── events/                     # 事件引擎
│   ├── event_types.py          #   16 类 EventType + 严重度 + 冷却
│   ├── event_detector.py       #   边沿检测（状态迁移才发，不重复发）
│   ├── event_engine.py         #   编排：检测 → 去重/冷却 → 发布
│   ├── event_bus.py            #   订阅/发布
│   └── event_history.py        #   事件历史（供认知循环回看"最近发生了什么"）
├── agent/                      # 智能体层
│   ├── agent_core.py           #   SpatialAgent（决策器）+ SpatialAgentSystem（运行时/主循环）
│   ├── orchestrator.py         #   双循环编排：安全快循环 + 认知慢循环（工作线程）
│   ├── cognitive_agent.py      #   认知决策（大模型）
│   ├── context_builder.py      #   上下文装配（状态 + 事件 + 记忆 + 画面）
│   ├── prompt_template.py      #   系统提示词 + 状态渲染 + 输出解析
│   ├── decision.py             #   Action 模型 + 规则兜底决策引擎
│   ├── action_schema.py        #   ActionType（10 种）+ Urgency
│   ├── interaction_policy.py   #   表达层闸门（防刷屏 / 最小间隔 / 长度上限）
│   ├── active_perception.py    #   主动感知策略（何时该看一眼画面）
│   ├── tools.py                #   工具集 + function calling schema（副作用唯一出口）
│   ├── llm_client.py           #   多模态大模型接口（OpenAI 兼容 + 工具循环 + 熔断）
│   ├── memory.py               #   对话/播报/行动记忆
│   └── safety/                 #   安全层
│       ├── safety_engine.py    #     四级裁决 + 干预
│       └── risk_rules.py       #     默认风险规则集
├── camera/                     # 摄像头层
│   ├── iphone_receiver.py      #   帧接收、缓存、落盘、新鲜度
│   └── image_processor.py      #   解码/缩放/编码/合成测试帧
├── recording/                  # 录制与回放（科研可复现）
│   ├── schemas.py              #   落盘 schema（states/events/actions/llm + SCHEMA_VERSION）
│   ├── session_recorder.py     #   会话录制
│   └── session_player.py       #   回放器（零 import simulator，靠鸭子类型假扮传感器）
├── api/
│   ├── websocket_server.py     #   FastAPI：REST + 双 WebSocket + 后台主循环
│   └── test_page.html          #   实时测试控制台（浏览器摄像头模拟 + 手动驾驶）
├── ios/BSACameraStreamer/      # 原生 iOS 推流 App（SwiftUI，零三方依赖）
├── tests/
│   ├── selftest.py             #   75 项细粒度自检（改代码时用）
│   └── acceptance.py           #   10 项验收场景，逐条对应 Gate 1~10（验收时用）
├── scripts/
│   ├── dev_check.sh            #   一键自验：自检(硬门槛) → 实时运行 → 起服务开浏览器
│   └── package.py              #   交付打包（排除密钥/设备备份/构建产物）
├── docs/                       # 见文末「文档」
├── tools/                      # 联调与测试工具（真机不在手时也能验证链路）
│   ├── acceptance_demo.py      #   ★七拍验收 Demo（任务书第二十九节剧本）
│   ├── iphone_stub.py          #   iPhone 推流替身（与 App 同一协议）
│   ├── fake_llm_server.py      #   本地假大模型（OpenAI 兼容，零成本跑通多模态链路）
│   ├── verify_image_link.py    #   断言"图片确实进了模型输入"
│   ├── ui_smoke_test.py        #   测试页 UI 冒烟测试（Node + DOM 桩，不开浏览器验渲染）
│   ├── set_llm_key.py          #   写入并实测 API Key（自动修断行 / 大小写 / 套餐配对）
│   ├── verify_real_vision.py   #   用**真实大模型**做 A/B 验证：画面是否真的改变了决策
│   ├── verify_live_camera.py   #   用**真机画面**验证：模型说的话是不是来自眼前那一帧
│   ├── render_map.py           #   把 config.yaml 的地图画成 docs/map_check.png（改障碍后先看图）
│   └── e2e_test.py             #   一键端到端回归：起服务 → 推流 → 断言 → 报告
└── logs/                       # 运行日志与 latest.jpg
```

## 服务接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 实时测试控制台（小地图 / 行动流 / 摄像头 / 大模型诊断） |
| GET | `/api/state` | 当前 SpatialState 的**裁剪视图** + 最近一次行动（含规划路径与动态障碍坐标） |
| GET | `/api/stats` | 全系统统计（地图/传感器/世界模型/工具/大模型/熔断状态） |
| GET | `/api/camera` | 摄像头通道状态 |
| GET | `/api/frame` | 摄像头**最新一帧 JPEG**（供网页预览手机推流的画面；`?max_age_s=` 控制可接受的过期阈值） |
| GET | `/api/map` | 地图几何（边界 / 分区 / 路点 / 静态物体），供前端画小地图 |
| GET | `/api/llm_check` | 大模型连通性诊断；`?vision=1` 加测看图能力 |
| GET | `/api/test_frame` | 合成测试图（无需真实摄像头） |
| POST | `/api/query` | 提交用户文本指令（模拟语音） |
| POST | `/api/image` | 上传一帧图片（multipart，字段名 `file`） |
| POST | `/api/control` | `pause` / `resume` / `reset` |
| WS | `/ws/camera` | 图片上行 |
| WS | `/ws/agent` | 状态行动下行 + 用户指令上行 |

> ⚠️ `/api/state` 返回的是 `to_prompt_dict()` **裁剪视图**（为省 token），不是完整 `model_dump()`。
> 例如查不到 `environment.walkable_width` 是正常的，等价信息在 `affordance.passable_width_m`。

### 大模型诊断接口

排查「Key / base_url / 网络」问题时，直接问服务端比看日志快：

```bash
curl -s --noproxy '*' 'http://127.0.0.1:8000/api/llm_check' | python -m json.tool          # 连通性
curl -s --noproxy '*' 'http://127.0.0.1:8000/api/llm_check?vision=1' | python -m json.tool # 连看图能力
```

返回 `category` 会明确区分 `not_configured` / `auth_error` / `bad_endpoint` /
`rate_limited` / `network_error` / `ok`，并附带 **HTTP 状态码、原始错误正文与修复提示**
（`LLMClient` 出于「不打断主循环」的设计会吞掉异常，这个接口故意把它暴露出来）。

> **熔断保护**：认证失败（401/403）或模型名错误（404）不会自愈，因此连续失败后
> `LLMClient` 会熔断（认证类 600s、限流 30s、网络 15s），期间不再发起真实请求，
> 避免 1 Hz 无限重试浪费配额并把日志刷爆。成功一次即自动解除。

## 测试与验收

```bash
python main.py --selftest           # 75 项细粒度自检（改代码时用）
python main.py --acceptance         # 10 项验收场景（逐条对应 Gate 1~10）
python main.py --acceptance-demo    # 七拍验收 Demo（任务书第二十九节剧本）
```

| 分组 | 检查内容 |
| --- | --- |
| 配置与模型 | 配置必填项、SpatialState 校验与 JSON 往返、拒绝非法数据 |
| 地图与寻路 | 栅格构建、路点可通行、A\* 全局寻路、**路径安全间隙、不穿障碍、绕开动态障碍** |
| 传感器与融合 | 噪声统计量、丢帧率、**噪声不会把用户推入墙内**、置信度估计、新鲜度 |
| 动态障碍 | 生成/移动/清除、不会生成在墙里、通道变窄成对生成 |
| 世界模型 | 物体持久记忆与淘汰、语义/可通行层、已探索区域、打转检测 |
| 事件引擎 | 16 类事件的边沿检测、冷却与去重、订阅发布、事件历史 |
| 安全层 | 四级裁决、阈值 `null` 兜底、**安全底线覆盖模型**、紧急必须停 |
| 双循环 | 快循环每轮求值、慢循环工作线程、**LLM 卡 5 秒不拖慢主循环**、结果 TTL |
| 表达层 | 防刷屏（CONTINUE 抑制 / 非 CONTINUE 必插入）、最小间隔、长度上限、答用户问题可突破节流 |
| 工具与动作 | 工具 schema 一致、重规划真实生效、`Action.as_dict()` 冻结格式键集 |
| 录制与回放 | 白名单字段守卫、**回放 100% 一致**、回放器零 `import simulator` |
| 主动感知 | 三条触发条件、节流、无相机不算触发、`on_demand` 省图、一次性握手 |
| 分层铁律 | 全仓扫描 `import simulator`，仅装配处允许（用例 47） |
| 接口 | REST 端点（含地图几何 / 大模型诊断）、双 WebSocket 通道（握手/心跳/统计/错误帧） |
| 端到端 | 入口→出口全程导航、用户提问有回应、系统重置、**600 轮长时间稳定性（无穿墙）** |

七拍验收 Demo 的七拍，逐字对应任务书第二十九节：
用户下指令 → 正常直行保持安静 → 椅子突现（事件/安全/绕行）→ 障碍消失不刷屏
→ 用户提问取图回答 → 摄像头断线继续导航 → LLM 卡 5 秒安全循环照常。

> 自检只覆盖 Python 侧。iOS App 用 `xcodebuild` 单独验证编译：
> ```bash
> cd ios/BSACameraStreamer
> xcodebuild -project BSACameraStreamer.xcodeproj -scheme BSACameraStreamer \
>   -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
>   CODE_SIGNING_ALLOWED=NO build
> ```

## 录制与回放（科研可复现）

录制一个会话，再用完全一致的输入回放，用来做 **Model A / Model B / Rule Baseline 三方对比**，
避免实验只能"现场跑一次"。

```bash
# 录制（默认写到 config.recording.root，可用 --record DIR 指定）
python main.py --record --session-id exp01 --record-notes "baseline 规则模式" --ticks 120

# 回放最近一个会话
python main.py --mode replay

# 回放指定会话，并把结果另存为新会话、限定轮数
python main.py --mode replay --session exp01 --replay-out exp01_replay --replay-ticks 60

# 回放时启用大模型（默认只用规则基线，保证可复现）
python main.py --mode replay --session exp01 --replay-llm
```

会话目录包含 `states.jsonl` / `events.jsonl` / `actions.jsonl` / `llm.jsonl` / `images/` /
`meta.json` / `map.json`。注意 `events.jsonl` 是**一行一条事件**，没有事件的轮次不写行。
细节见 `docs/REPLAY_SYSTEM.md`。

## 摄像头链路联调（不需要真机、不需要 API Key）

```bash
# 1) 端到端回归：自动起服务端 + 假大模型，推 10 帧，逐环断言
python tools/e2e_test.py

# 2) 只验证「图片是否真的进了大模型输入」（用假客户端抓真实请求体）
python tools/verify_image_link.py

# 2b) 用**真实大模型**做 A/B 对照：空旷地面 vs 正前方起火，看决策是否真的改变
#     （需要配好 Key；「火」在 SpatialState 里没有来源，模型只能从画面看出来）
python tools/verify_real_vision.py

# 2c) 用**真机画面**验证最后一环：模型嘴里说的，是不是用户眼前那一帧？
#     需先 `python main.py --serve` 且 iPhone App 正在推流；两条命令即可复现：
python main.py --serve
python tools/verify_live_camera.py --keep
#     原理：结构化状态里只有地图数据（desk/chair/door），模型被要求「只看画面」并
#     以固定前缀「我看到：」作答；它答出的柜子/置物架/包/食盆在地图里毫无来源，
#     因此只能是画面的功劳。--keep 会把证据帧存到 logs/live_camera_proof.jpg。

# 3) 手动推流（真机不在手时的替身，协议与 iOS App 完全一致）
python tools/iphone_stub.py --frames 30 --fps 1 --source synthetic   # 合成画面
python tools/iphone_stub.py --source camera --frames 30              # 用电脑摄像头

# 4) 零成本跑通多模态：本地假大模型替代真实 API
python tools/fake_llm_server.py &
export BSA_LLM_API_KEY=anything \
       BSA_LLM_BASE_URL=http://127.0.0.1:8900/v1 \
       BSA_LLM_MODEL=fake-multimodal
python main.py --serve
```

`e2e_test.py` 覆盖的断言：接收器收到帧 → `latest.jpg` 落盘 → 新鲜帧可用（3s 内）
→ `SpatialState.camera.image_available` → 模型侧确认请求带图且带工具 schema
→ Action 来自大模型且**建议依据了画面**。

## 真机替换点

| 真实能力 | 替换位置 | 需要保持不变的东西 |
| --- | --- | --- |
| UWB 室内定位 | `simulator/sensor_simulator.py` → 未来 `sensors/uwb.py` | 产出 `user.position` |
| IMU 姿态 | 同上 → 未来 `sensors/imu.py` | 产出 `user.heading` / `speed` |
| 3D 地图 / NavMesh / UE5 数字孪生 | `simulator/map_simulator.py` → 未来 `maps/` 的度量层实现 | 提供可通行查询 + 路径规划 |
| iPhone LiDAR 深度避障 | `simulator/obstacle_simulator.py` → 未来 `sensors/lidar.py` | 产出 `environment` 的障碍与净空 |
| 智能眼镜摄像头 | `camera/`（沿用同一 WS 协议） | 产出 JPEG 帧 |
| 语音播报 / 语音输入 | `agent/tools.py` 的 `speak()` / `api` 的 query 通道 | 保持工具签名不变 |

> 具体接入位置与接口约定见 `docs/V04_REAL_SENSOR_INTEGRATION_PLAN.md`（v0.4 预留，**v0.3 不实现**）。

## 开发进度

**Step 阶段（v0.2 及更早）**

| 步骤 | 内容 | 状态 |
| --- | --- | --- |
| Step 1 | 工程骨架：目录 / venv / 配置中心 / 启动自检 | ✅ |
| Step 2 | `SpatialState` 统一空间状态模型 | ✅ |
| Step 3 | 室内环境 + 导航 + 传感器 + 动态障碍模拟 | ✅ |
| Step 4 | Agent Core + Tool Calling + 规则兜底决策 | ✅ |
| Step 5 | 实时多模态大模型接口（图片 + 状态 + 指令） | ✅ |
| Step 6 | WebSocket 接收 iPhone 图片 + 浏览器测试页 | ✅ |
| Step 7 | 端到端自检与真实服务联调 | ✅ |
| Step 8 | 原生 iOS 推流 App + 摄像头链路端到端回归 | ✅ |
| Step 8.5 | 一键自验脚本 `scripts/dev_check.sh` | ✅ |
| Step 8.6 | 实时测试控制台 + 大模型熔断保护 | ✅ |
| Step 9 | 真实多模态链路验证（`verify_real_vision.py` / `verify_live_camera.py`） | ✅ |
| Step 10 | 接入真实 UWB / LiDAR / IMU 硬件 与 UE5 数字孪生 | ⏳ v0.4 |

**v0.3 架构升级阶段（任务书分阶段）**

| Stage | 内容 | 状态 |
| --- | --- | --- |
| 1 | 架构审计（不改代码，只输出报告） | ✅ `docs/V02_ARCHITECTURE_AUDIT.md` |
| 2 | 数据模型升级（Egocentric Spatial State） | ✅ |
| 3 | Sensor Adapter Layer | ✅ |
| 4 | State Fusion（含置信度估计器） | ✅ |
| 5 | Spatial World Model + 地图接口 | ✅ |
| 6 | Event Engine | ✅ |
| 7 | Safety / Cognitive 双循环 | ✅ |
| 8 | Interaction Policy + Action Schema | ✅ |
| 9 | Recording / Replay | ✅ |
| 10 | Active Perception | ✅ |
| 11 | 十项验收场景 + 验收 Demo + 文档 + 版本升级 | ✅ 本版 |

**本版新增（v0.3 收尾之外的直接需求）**

| 内容 | 状态 |
| --- | --- |
| 更大更复杂的室内场景（办公楼-1F，40×46 m，16 个功能区，43 个静态物体） | ✅ |
| WASD 手动驾驶虚拟用户 | ✅ |
| `replans` 计数器虚高修复 | ✅ |

## 文档

| 文档 | 回答什么问题 |
| --- | --- |
| `docs/V02_ARCHITECTURE_AUDIT.md` | **为什么要改**：v0.2 的真实架构、10 个最严重问题、P0/P1/P2 技术债 |
| `docs/V03_MIGRATION_LOG.md` | **改了什么**：Stage 2~10 逐阶段的修改文件 / 原因 / 测试结果 / 踩坑 |
| `docs/ARCHITECTURE_V03.md` | 现行架构：整体图、数据流、快慢双循环、事件流、Agent 流、摄像头流、未来接入点 |
| `docs/SPATIAL_STATE_SCHEMA.md` | `SpatialState` 字段树、三件套契约、裁剪视图，以及"如何安全加字段" |
| `docs/EVENT_SYSTEM.md` | 16 类事件、边沿检测、总线、事件如何触发 Agent 与取图 |
| `docs/SAFETY_ARCHITECTURE.md` | 为什么安全不依赖大模型、风险规则、四级裁决、熔断与并发模型 |
| `docs/REPLAY_SYSTEM.md` | 会话目录结构、字段守卫、回放器如何零依赖模拟器、一致率报告 |
| `docs/TEST_PLAN.md` | 测试分层与命令、Gate 映射、断言设计原则、已知陷阱、发布检查清单 |
| `docs/MAP_DESIGN_GUIDE.md` | **怎么改地图**：zones/门洞/区域型障碍的写法、验证流程（含画图）、8 个已知陷阱 |
| `docs/MIGRATION_V02_TO_V03.md` | v0.2 → v0.3 迁移总览、兼容性策略、回归方法、"最容易改坏的地方" |
| `docs/V04_REAL_SENSOR_INTEGRATION_PLAN.md` | v0.4 预留：UWB / IMU / LiDAR / UE5 各应接在哪一层（不实现） |
| `V0.3_DELIVERY_REPORT.md` | 最终交付报告：问题、改动、新架构、设计决策、兼容性、测试与性能、已知问题、v0.4 方向 |
| `docs/V03_DEMO_TRANSCRIPT.md` | 七拍验收 Demo 的完整运行转写（可复现的证据） |
