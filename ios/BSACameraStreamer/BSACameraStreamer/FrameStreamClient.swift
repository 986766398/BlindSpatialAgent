//
//  FrameStreamClient.swift
//  BSACameraStreamer
//
//  WebSocket 上行客户端：把 JPEG 帧送到电脑上的 Python 服务端。
//
//  协议（对应服务端 api/websocket_server.py 的 /ws/camera）：
//      上行 二进制帧   <JPEG 字节>                     —— 唯一的数据帧，不加任何封装
//      上行 文本帧     {"type":"hello","device":"iPhone"}   连接建立后立即发送
//      上行 文本帧     {"type":"ping"}                       定时心跳
//      下行 文本帧     {"type":"hello", ...}                 服务端握手（含 max_fps 等约束）
//      下行 文本帧     {"type":"ack","frames":N,"accepted":M}  每 5 帧回一次，用于确认链路
//      下行 文本帧     {"type":"pong","t":...}               心跳回应
//      下行 文本帧     {"type":"status", ...}                服务端接收统计
//      下行 文本帧     {"type":"error","note":"..."}         协议错误
//
//  为什么用 URLSessionWebSocketTask 而不是 Starscream 之类的三方库：
//  零依赖，不需要 CocoaPods / SPM 配置，clone 下来就能编译；
//  而本 App 的通信复杂度极低，官方 API 完全够用。
//
//  断线策略：指数退避自动重连（1s → 2s → 4s → 8s 封顶），
//  重连期间不缓存旧帧 —— 导航场景里迟到的图比没有图更危险。
//

import Foundation
import UIKit

final class FrameStreamClient: NSObject {

    // MARK: - 状态

    enum State: Equatable {
        case idle
        case connecting
        case connected
        case reconnecting(attempt: Int)
        case failed(String)

        var label: String {
            switch self {
            case .idle:                     return "未连接"
            case .connecting:               return "连接中…"
            case .connected:                return "已连接"
            case .reconnecting(let n):      return "重连中（第 \(n) 次）"
            case .failed(let msg):          return "连接失败：\(msg)"
            }
        }

        var isActive: Bool {
            switch self {
            case .connected, .connecting, .reconnecting: return true
            case .idle, .failed:                         return false
            }
        }
    }

    /// 链路统计（全部在主线程读写）。
    struct Stats {
        var sentFrames: Int = 0
        var ackedFrames: Int = 0
        var bytesSent: Int = 0
        var lastFrameBytes: Int = 0
        var lastSentAt: Date?
        var rttMs: Double?
        /// 服务端握手返回的上行限速（帧/秒），用于提示客户端不要超发。
        var serverMaxFPS: Int?
        /// 服务端已接受的最新 frame_id。
        var serverFrameID: Int = 0
    }

    // MARK: - 回调（均在主线程触发）

    var onStateChange: ((State) -> Void)?
    var onStatsChange: ((Stats) -> Void)?
    var onLog: ((String) -> Void)?

    private(set) var state: State = .idle {
        didSet {
            guard state != oldValue else { return }
            onStateChange?(state)
        }
    }

    private(set) var stats = Stats() {
        didSet { onStatsChange?(stats) }
    }

    // MARK: - 内部

    private lazy var session: URLSession = {
        // delegateQueue = nil → 回调走 URLSession 自己的串行队列，我们内部再切主线程
        URLSession(configuration: .default, delegate: self, delegateQueue: nil)
    }()

    private var task: URLSessionWebSocketTask?
    private var targetURL: URL?
    private var shouldReconnect = false
    private var reconnectAttempt = 0
    private var reconnectWorkItem: DispatchWorkItem?
    private var pingTimer: DispatchSourceTimer?
    private var pingSentAt: Date?
    private let maxBackoff: TimeInterval = 8.0

    /// 有意的主动断开标记，用来区分"用户点了断开"与"网络掉了"。
    private var closingIntentionally = false

    // MARK: - 对外接口

    /// 建立连接。会自动重连，直到调用 disconnect()。
    func connect(to url: URL) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.targetURL = url
            self.shouldReconnect = true
            self.closingIntentionally = false
            self.reconnectAttempt = 0
            self.onLog?("开始连接 \(url.absoluteString)")
            self.openTask()
        }
    }

    /// 主动断开，并停止自动重连。
    func disconnect() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.shouldReconnect = false
            self.closingIntentionally = true
            self.cancelReconnect()
            self.stopPingTimer()
            self.task?.cancel(with: .normalClosure, reason: nil)
            self.task = nil
            self.state = .idle
            self.onLog?("已主动断开")
        }
    }

    /// 发送一帧 JPEG。未连接时静默丢弃（不排队、不缓存）。
    func send(jpeg: Data) {
        DispatchQueue.main.async { [weak self] in
            guard let self, let task = self.task, self.state == .connected else { return }
            task.send(.data(jpeg)) { [weak self] error in
                guard let self else { return }
                DispatchQueue.main.async {
                    if let error {
                        self.handleFailure(error)
                        return
                    }
                    self.stats.sentFrames += 1
                    self.stats.bytesSent += jpeg.count
                    self.stats.lastFrameBytes = jpeg.count
                    self.stats.lastSentAt = Date()
                }
            }
        }
    }

    /// 发送一个 JSON 控制帧。
    func sendJSON(_ object: [String: Any]) {
        DispatchQueue.main.async { [weak self] in
            guard let self, let task = self.task, self.state == .connected,
                  let data = try? JSONSerialization.data(withJSONObject: object),
                  let text = String(data: data, encoding: .utf8) else { return }
            task.send(.string(text)) { _ in }
        }
    }

    // MARK: - 连接管理

    private func openTask() {
        guard let url = targetURL else { return }

        cancelReconnect()
        stopPingTimer()

        if reconnectAttempt == 0 {
            state = .connecting
        } else {
            state = .reconnecting(attempt: reconnectAttempt)
        }

        let newTask = session.webSocketTask(with: url)
        task = newTask
        newTask.resume()

        startPingTimer()
        receiveLoop(newTask)
    }

    /// 持续接收下行消息。WebSocket 是双向的，不消费下行会导致缓冲区堆积。
    private func receiveLoop(_ task: URLSessionWebSocketTask) {
        Task { [weak self] in
            while true {
                do {
                    let message = try await task.receive()
                    await MainActor.run { [weak self] in
                        guard let self, self.task === task else { return }
                        self.handle(message)
                    }
                } catch {
                    await MainActor.run { [weak self] in
                        guard let self, self.task === task else { return }
                        self.handleFailure(error)
                    }
                    return
                }
            }
        }
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        switch message {
        case .string(let text):
            handleServerText(text)
        case .data(let data):
            onLog?("收到下行二进制 \(data.count) B（非预期，已忽略）")
        @unknown default:
            break
        }
    }

    private func handleServerText(_ text: String) {
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            onLog?("收到非 JSON 下行：\(text.prefix(60))")
            return
        }

        let type = (obj["type"] as? String)?.lowercased() ?? ""

        switch type {
        case "hello":
            let maxFPS = obj["max_fps"] as? Int
            stats.serverMaxFPS = maxFPS
            onLog?("服务端握手成功，限速 \(maxFPS.map(String.init) ?? "?") fps")

        case "ack":
            if let accepted = obj["accepted"] as? Int { stats.ackedFrames = accepted }
            if let frameID = obj["frame_id"] as? Int { stats.serverFrameID = frameID }
            if (obj["ok"] as? Bool) == false, let reason = obj["reason"] as? String {
                onLog?("服务端拒绝该帧：\(reason)")
            }

        case "pong":
            if let sent = pingSentAt {
                stats.rttMs = Date().timeIntervalSince(sent) * 1000.0
                pingSentAt = nil
            }

        case "status":
            let accepted = obj["accepted"] as? Int ?? 0
            onLog?("服务端累计接收 \(obj["received"] as? Int ?? 0) 帧，接受 \(accepted) 帧")

        case "welcome":
            onLog?("服务端确认设备：\(obj["device"] as? String ?? "?")")

        case "error":
            onLog?("服务端协议错误：\(obj["note"] as? String ?? "")")

        default:
            onLog?("未知下行消息：\(text.prefix(60))")
        }
    }

    private func handleFailure(_ error: Error) {
        guard shouldReconnect else { return }
        // 主动断开产生的中断不算失败
        if closingIntentionally { return }

        onLog?("链路异常：\(error.localizedDescription)")
        task?.cancel(with: .abnormalClosure, reason: nil)
        task = nil
        stopPingTimer()
        scheduleReconnect()
    }

    private func scheduleReconnect() {
        guard shouldReconnect else {
            state = .failed("已断开")
            return
        }
        cancelReconnect()
        reconnectAttempt += 1
        let delay = min(maxBackoff, pow(2.0, Double(min(reconnectAttempt - 1, 3))))

        state = .reconnecting(attempt: reconnectAttempt)
        onLog?("\(String(format: "%.0f", delay))s 后重连…")

        let item = DispatchWorkItem { [weak self] in
            guard let self, self.shouldReconnect else { return }
            self.openTask()
        }
        reconnectWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: item)
    }

    private func cancelReconnect() {
        reconnectWorkItem?.cancel()
        reconnectWorkItem = nil
    }

    // MARK: - 心跳

    /// 每 5 秒发一次 ping。既用于测 RTT，也让 NAT / 路由器不回收空闲连接。
    private func startPingTimer() {
        stopPingTimer()
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 5, repeating: 5)
        timer.setEventHandler { [weak self] in
            guard let self, self.state == .connected else { return }
            self.pingSentAt = Date()
            self.sendJSON(["type": "ping"])
        }
        pingTimer = timer
        timer.resume()
    }

    private func stopPingTimer() {
        pingTimer?.cancel()
        pingTimer = nil
        pingSentAt = nil
    }

    private var helloPayload: [String: Any] {
        [
            "type": "hello",
            "device": "iPhone",
            "app": "BSACameraStreamer",
            "app_version": (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "1.0",
            "system": "iOS \(UIDevice.current.systemVersion)",
        ]
    }

    /// 连接建立后主动打招呼：服务端据此在日志里看到真实设备信息。
    private func sendHello() {
        sendJSON(helloPayload)
    }
}

// MARK: - URLSessionWebSocketDelegate

extension FrameStreamClient: URLSessionWebSocketDelegate {

    func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didOpenWithProtocol protocol: String?
    ) {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.task === webSocketTask else { return }
            self.closingIntentionally = false
            self.reconnectAttempt = 0
            self.state = .connected
            self.onLog?("WebSocket 已建立（protocol=\(`protocol` ?? "-")）")
            self.sendHello()
        }
    }

    func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
        reason: Data?
    ) {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.task === webSocketTask else { return }
            self.onLog?("服务端关闭连接（code=\(closeCode.rawValue)）")
            self.task = nil
            self.stopPingTimer()
            if self.shouldReconnect && !self.closingIntentionally {
                self.scheduleReconnect()
            } else {
                self.state = .idle
            }
        }
    }
}

// MARK: - 连接诊断工具

extension FrameStreamClient {

    /// 把用户输入的 host/port/path 拼成 WebSocket URL，并做基本校验。
    /// 返回 nil 表示输入不合法。
    static func makeURL(host: String, port: Int, path: String) -> URL? {
        let trimmedHost = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedHost.isEmpty, (1...65535).contains(port) else { return nil }

        var normalizedPath = path.trimmingCharacters(in: .whitespacesAndNewlines)
        if normalizedPath.isEmpty { normalizedPath = "/ws/camera" }
        if !normalizedPath.hasPrefix("/") { normalizedPath = "/" + normalizedPath }

        var components = URLComponents()
        components.scheme = "ws"
        components.host = trimmedHost
        components.port = port
        components.path = normalizedPath
        return components.url
    }
}
