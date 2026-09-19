# BSACameraStreamer —— iPhone 实时摄像头图片流

BlindSpatialAgent 的**唯一真实传感器**：把 iPhone 后置摄像头画面以每秒 1 帧 JPEG 的形式，
通过 WebSocket 送到电脑上的 Python 服务端。

> 本 App 只做三件事：**打开摄像头 → 编码 JPEG → WebSocket 上行**。
> 不做视频理解、不做目标检测、不做推理 —— 这些全部由 Python 侧 Agent 负责。

---

## 1. 工程结构

```
ios/BSACameraStreamer/
├── BSACameraStreamer.xcodeproj/          # 已生成好的 Xcode 工程，直接双击打开
└── BSACameraStreamer/
    ├── BSACameraStreamerApp.swift        # App 入口（SwiftUI）
    ├── ContentView.swift                  # 界面：服务器配置 + 连接状态 + 统计 + 预览 + 日志
    ├── StreamViewModel.swift              # 推流控制器：串联采集与上行，驱动 UI
    ├── CameraCaptureService.swift         # 摄像头采集：AVCaptureSession + 1 fps 节流 + JPEG 编码
    ├── FrameStreamClient.swift            # WebSocket 上行：URLSessionWebSocketTask + 自动重连
    └── Info.plist                         # 权限与 ATS 配置
```

依赖：**零第三方库**。只用 AVFoundation / SwiftUI / Foundation，打开即可编译，不需要 CocoaPods 或 SPM。

要求：iOS 17.0+、Xcode 15+（本工程用 Xcode 26.6 验证）。

---

## 2. 快速开始

### 2.1 先在电脑上启动服务端

```bash
cd BlindSpatialAgent
source .venv/bin/activate
python main.py --serve
```

启动后会打印：

```
Blind Spatial Agent Simulator v0.2.0 —— 服务模式
  本机访问 : http://127.0.0.1:8000/
  局域网   : http://192.168.1.23:8000/   （iPhone 用 Safari 打开）
  图片上行 : ws://<host>:8000/ws/camera
```

⚠️ `192.168.1.23` 就是你待会儿要填进 App 的地址。如果打印出的是 `198.18.x.x` 之类的地址，
说明本机有 VPN / 虚拟网卡，请改用真实的 WiFi 网卡地址：

```bash
ifconfig | grep "inet " | grep -v 127.0.0.1
```

### 2.2 打开并运行 iOS 工程

1. 双击 `ios/BSACameraStreamer/BSACameraStreamer.xcodeproj`
2. 选中左侧项目 → **Signing & Capabilities** → 勾选 **Automatically manage signing**，
   `Team` 选你自己的 Apple ID（免费个人账号即可）
3. 如果 `Bundle Identifier` 报冲突，改成唯一值，例如 `com.yourname.bsacamera`
4. 用数据线连上 iPhone，在顶部设备选择器里选中它，按 **⌘R** 运行
5. 手机上首次启动会弹两个权限：**摄像头**、**本地网络** —— 都必须允许

> 模拟器跑不了这个 App 的真实用途（模拟器没有摄像头）。工程内置了合成帧降级：
> 在模拟器上运行会推送带时间戳的合成画面，可以用来验证 WebSocket 链路是否通。

> 也可以用命令行一键构建安装（省去在 Xcode 里点选）：
> ```bash
> bash build_and_install.sh --wait
> ```
> 构建/签名/连接失败的排查见 **第 6 节**。

### 2.3 在 App 里配置并开始推流

| 字段 | 填什么 |
| --- | --- |
| IP | 电脑的局域网 IP，例如 `192.168.1.23` |
| 端口 | `8000`（与服务端 `--port` 一致） |
| 路径 | `/ws/camera`（一般不用改） |
| 帧率 | `1 fps`（默认） |

点 **开始推流**。状态区会依次变成「连接中…」→「已连接」，
「服务器确认」计数开始增长，说明链路正常。

### 2.4 确认服务端收到了图

浏览器打开 `http://127.0.0.1:8000/api/camera`，或直接看：

```bash
ls -l logs/latest.jpg          # 每秒被刷新
```

---

## 3. 通信协议

对应服务端 `api/websocket_server.py` 的 `/ws/camera`：

| 方向 | 内容 | 说明 |
| --- | --- | --- |
| 上行 | `<JPEG 字节>` | 二进制帧，**就是裸 JPEG，不加任何封装** |
| 上行 | `{"type":"hello","device":"iPhone",...}` | 连接建立后立即发送 |
| 上行 | `{"type":"ping"}` | 每 5 秒一次心跳，同时测 RTT |
| 下行 | `{"type":"hello","max_fps":5,...}` | 服务端握手，下发上行限速 |
| 下行 | `{"type":"ack","ok":true,"frames":N,"accepted":M}` | 每帧回执，用于「服务器确认」计数 |
| 下行 | `{"type":"pong","t":...}` | 心跳回应 |
| 下行 | `{"type":"error","note":"..."}` | 协议错误 |

---

## 4. 关键设计决策

| 决策 | 原因 |
| --- | --- |
| 用 `AVCaptureVideoDataOutput` 而不是 `AVCapturePhotoOutput` | PhotoOutput 每次拍照有快门音和 0.2~0.5s 延迟，做不到稳定 1 fps |
| 节流在**编码之前** | 每秒只编码 1 次，而不是编 30 次丢 29 次 —— 省电且不会编码积压 |
| `alwaysDiscardsLateVideoFrames = true` | 积压帧直接丢。对导航场景，一张 2 秒前的图比没有图更危险 |
| 640×480 / JPEG 质量 0.5 | 单帧 30~60 KB，1 fps 约 0.3~0.5 Mbps，WiFi 上很宽裕 |
| 采集后立即设定 `videoRotationAngle = 90` | 把方向在源头定死，Agent 侧不需要做方向判断 |
| 断线指数退避重连（1→2→4→8s 封顶） | 网络抖动后自动恢复；重连期间**不缓存旧帧** |
| 用 `URLSessionWebSocketTask` 而非三方库 | 零依赖，clone 下来就能编译 |
| 界面显示「最近一帧」缩略图 | 显示的就是**实际发送的内容**，便于现场排查 |

---

## 5. 常见问题

**连不上，状态一直停在「连接中…」**
1. 手机和电脑是否在同一个 WiFi 下（手机别开热点、别连 5G 蜂窝）
2. IP 填对了吗 —— 必须是电脑的**局域网**地址，不是 `127.0.0.1`
3. 电脑防火墙是否放行了 8000 端口：
   `系统设置 → 网络 → 防火墙 → 选项`，允许 Python 接受传入连接
4. macOS 上先用这条命令验证服务端可达：
   `curl -s http://<电脑IP>:8000/api/camera`

**连上了，但「服务器确认」一直是 0**
在 App 的「运行日志」区看有没有 `服务端拒绝该帧：decode_failed`。
若是，说明发出去的不是合法 JPEG —— 请重装 App 确认 `CameraCaptureService` 未被改动。

**没有弹出「本地网络」权限弹窗，且无法连接**
iOS 14+ 访问局域网设备必须获得该权限。到
`设置 → 隐私与安全性 → 本地网络` 里手动打开本 App 的开关。

**App 里显示「模拟器无摄像头 → 使用合成帧」**
说明当前没有可用的后置摄像头（模拟器或权限被拒）。请在真机上运行，
并确认 `设置 → 隐私与安全性 → 摄像头` 已允许本 App。

**「连接失败：A TLS/SSL error」或类似提示**
`Info.plist` 里的 ATS 例外被改动了。服务端是明文 `ws://`，必须保留：

```xml
<key>NSAppTransportSecurity</key>
<dict>
  <key>NSAllowsArbitraryLoads</key><true/>
  <key>NSAllowsLocalNetworking</key><true/>
</dict>
```

> 这是开发原型的配置。正式产品应改为 `wss://` 并使用有效证书，
> 同时把 `NSAllowsArbitraryLoads` 换成精确的 `NSExceptionDomains`。

**想改帧率 / 画质**
帧率在 App 界面里选（0.5 / 1 / 2 / 3 / 5 fps）。
分辨率与 JPEG 质量在 `CameraCaptureService.swift` 的 `jpegQuality` 与服务端
`config/config.yaml` 的 `camera.max_image_side` / `jpeg_quality` 里调整
（服务端还会再做一次缩放与重编码，那是送模型前的统一处理）。

---

## 6. 真机构建与安装（一键脚本）

```bash
cd ios/BSACameraStreamer
bash build_and_install.sh --wait     # 会等你解锁手机，然后构建 + 安装
```

脚本会自动完成：探测真机 UDID → 自检签名账号 → 构建 → `devicectl` 安装
→ 打印要填进 App 的服务器地址。可用 `DEVICE_ID=` / `TEAM_ID=` / `BUNDLE_ID=` 覆盖默认值。

### 三个最容易卡住的点（本项目都实际踩过）

**① 设备标识有两个，千万别用错**

| 用途 | 该填什么 | 形如 |
| --- | --- | --- |
| `xcodebuild -destination` | **UDID** | `00008150-000829A611F0401C` |
| `devicectl --device` | 两种都收；它自己报的 Identifier 是 | `8B7EEE49-70DD-534B-B3B0-1AF6C67231A5` |

拿内部标识当 `-destination`，xcodebuild 会「找不到目标」，`build/` 里**不产出 `.app`
也不报明确错误**，非常难查。脚本已改为自动探测 UDID：

```bash
xcodebuild -project BSACameraStreamer.xcodeproj -scheme BSACameraStreamer -showdestinations
# ↑ 从这里取 id= 后面的值（platform:iOS 且不含 Simulator 的那一行）
```

**② Xcode 必须登录 Apple ID**

典型报错：

```
error: No Accounts: Add a new account in Accounts settings.
error: No profiles for 'com.bsa.spatialeye' were found
```

注意：**钥匙串里有开发证书 ≠ Xcode 里有账号**。本项目遇到的就是这种情况——
证书 `Apple Development: xxx@qq.com` 存在，但 Xcode ▸ Settings ▸ Accounts 是空的，
于是无法自动生成描述文件，`-allowProvisioningUpdates` 也无能为力。

修复：Xcode ▸ Settings… ▸ Accounts ▸ 左下角 `+` ▸ 用 Apple ID 登录（免费个人账号即可），
然后在 Signing & Capabilities 里选中该 Team。
若 `com.bsa.spatialeye` 被占用，改一个唯一值：`BUNDLE_ID=com.<你的名字>.bsacamera bash build_and_install.sh`。

自检命令：

```bash
security find-identity -v -p codesigning                                  # 看证书
defaults read com.apple.dt.Xcode DVTDeveloperAccountManagerAppleIDLists    # 看账号（空 = 没登录）
```

**③ 首次安装必须用 USB 数据线**

纯 Wi-Fi 连接（`devicectl` 里显示 `Transport Type: localNetwork`）会挂不上开发者磁盘映像：

```
The developer disk image could not be mounted on this device.
```

先用 USB-C 数据线连一次 iPhone（解锁屏幕并点「信任」），之后 Xcode 才会允许 Wi-Fi 调试。
另外手机**锁屏时**构建会一直等到 60 秒超时才报
`needs to be unlocked to enable development services` —— 所以脚本提供 `--wait`
先探测解锁状态，避免白白等待。

> 想自己看设备连接方式：
> `xcrun devicectl device info details --device <UDID> | grep -E "Transport|Device State"`

---

## 7. 无真机时怎么联调

用项目自带的推流脚本，走**完全相同的协议**：

```bash
# 合成画面，10 帧 @ 2 fps
python tools/iphone_stub.py --frames 10 --fps 2 --source synthetic

# 用电脑摄像头当"手机摄像头"
python tools/iphone_stub.py --source camera --frames 30
```

或者一条命令跑完整条链路的回归测试：

```bash
python tools/e2e_test.py            # 会自动起服务端与假大模型，全程不需要 API Key
```
