//
//  StreamViewModel.swift
//  BSACameraStreamer
//
//  推流控制器：把「摄像头采集」与「WebSocket 上行」两件事串起来，并暴露给 SwiftUI。
//
//  数据流：
//      CameraCaptureService --(每秒 1 帧 JPEG, 后台队列)--> StreamViewModel
//                                                              |
//                                                              +--> FrameStreamClient.send(jpeg:)
//                                                              |
//                                                              +--> @Published 更新 UI（主线程）
//
//  线程约定：所有 @Published 属性只在主线程写。
//  采集回调跑在 com.bsa.camera.capture 队列上，因此入口处必须切主线程。
//

import Foundation
import SwiftUI
import UIKit

final class StreamViewModel: ObservableObject {

    // MARK: - 服务器设置（可在界面上改）

    @Published var host: String = ""
    @Published var portText: String = "8000"
    @Published var path: String = "/ws/camera"
    /// 出帧频率（帧/秒）。默认 5.0。
    /// 最初默认 1.0，实测体感「手机转一下、网页要等一两秒才动」——
    /// 1 fps 下仅传输环节就占掉约 1s 延迟，5 fps 才谈得上实时。
    /// 上界受服务端握手下发的 max_fps 约束（当前配置为 8，留了抗抖动余量）。
    @Published var fps: Double = 5.0

    // MARK: - 链路状态（只读）

    @Published private(set) var connectionLabel: String = "未连接"
    @Published private(set) var connectionActive = false
    @Published private(set) var sentFrames = 0
    @Published private(set) var ackedFrames = 0
    @Published private(set) var bytesSent = 0
    @Published private(set) var lastFrameBytes = 0
    @Published private(set) var lastSentAt: Date?
    @Published private(set) var rttMs: Double?
    @Published private(set) var serverMaxFPS: Int?

    // MARK: - 摄像头状态（只读）

    @Published private(set) var cameraNote: String = "摄像头未启动"
    @Published private(set) var isCapturing = false
    @Published private(set) var previewImage: UIImage?
    @Published private(set) var resolutionText: String = "-"
    /// 实测出帧间隔（秒），用于确认实际出帧节奏与设定帧率是否一致。
    @Published private(set) var measuredInterval: Double?

    // MARK: - 日志与错误

    @Published private(set) var logs: [String] = []
    @Published var errorText: String?

    /// 是否正在推流（摄像头已开）。
    var isStreaming: Bool { isCapturing }

    // MARK: - 私有

    private let camera = CameraCaptureService()
    private let client = FrameStreamClient()
    private var recentFrameTimes: [TimeInterval] = []
    private static let settingsKey = "com.bsa.camera.settings.v1"

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    // MARK: - 设置持久化

    private struct StoredSettings: Codable {
        var host: String
        var port: Int
        var path: String
        var fps: Double
    }

    func loadSavedSettings() {
        guard let data = UserDefaults.standard.data(forKey: Self.settingsKey),
              let saved = try? JSONDecoder().decode(StoredSettings.self, from: data) else {
            appendLog("首次运行：请在「服务器」中填写电脑的局域网 IP")
            return
        }
        host = saved.host
        portText = String(saved.port)
        path = saved.path
        fps = saved.fps
        appendLog("已恢复上次的服务器设置：\(saved.host):\(saved.port)\(saved.path)")
    }

    private func saveSettings() {
        let stored = StoredSettings(host: host, port: parsedPort, path: path, fps: fps)
        guard let data = try? JSONEncoder().encode(stored) else { return }
        UserDefaults.standard.set(data, forKey: Self.settingsKey)
    }

    private var parsedPort: Int {
        Int(portText.trimmingCharacters(in: .whitespaces)) ?? 8000
    }

    /// 供界面显示的目标地址。
    var targetDescription: String {
        let h = host.trimmingCharacters(in: .whitespaces)
        return h.isEmpty ? "未设置" : "ws://\(h):\(parsedPort)\(path)"
    }

    // MARK: - 开始 / 停止推流

    func startStreaming() {
        errorText = nil

        guard let url = FrameStreamClient.makeURL(host: host, port: parsedPort, path: path) else {
            errorText = "服务器地址不合法。请填写电脑的局域网 IP（例如 192.168.1.23）与端口。"
            return
        }

        saveSettings()
        appendLog("目标：\(url.absoluteString)")

        // ---- ① WebSocket ----
        client.onStateChange = { [weak self] state in
            guard let self else { return }
            self.connectionLabel = state.label
            self.connectionActive = state.isActive
            self.appendLog("链路状态：\(state.label)")
        }
        client.onStatsChange = { [weak self] stats in
            guard let self else { return }
            self.sentFrames = stats.sentFrames
            self.ackedFrames = stats.ackedFrames
            self.bytesSent = stats.bytesSent
            self.lastFrameBytes = stats.lastFrameBytes
            self.lastSentAt = stats.lastSentAt
            self.rttMs = stats.rttMs
            self.serverMaxFPS = stats.serverMaxFPS
        }
        client.onLog = { [weak self] text in
            self?.appendLog(text)
        }
        client.connect(to: url)

        // ---- ② 摄像头 ----
        camera.interval = max(0.2, 1.0 / max(0.1, fps))
        camera.jpegQuality = 0.5
        camera.delegate = self

        camera.start { [weak self] result in
            guard let self else { return }
            switch result {
            case .success:
                self.isCapturing = true
                if self.camera.isSynthetic {
                    self.cameraNote = "模拟器无摄像头 → 使用合成帧（仅用于链路测试）"
                    self.appendLog("未检测到摄像头，降级为合成帧源")
                } else {
                    self.cameraNote = "后置摄像头运行中，\(String(format: "%.1f", self.fps)) fps JPEG"
                    self.appendLog("摄像头已启动，目标 \(String(format: "%.1f", self.fps)) fps")
                }
            case .failure(let error):
                self.isCapturing = false
                self.cameraNote = "摄像头启动失败"
                self.errorText = error.localizedDescription
                self.appendLog("摄像头启动失败：\(error.localizedDescription)")
                self.client.disconnect()
            }
        }
    }

    func stopStreaming() {
        camera.stop()
        client.disconnect()
        isCapturing = false
        cameraNote = "摄像头已停止"
        recentFrameTimes.removeAll()
        measuredInterval = nil
        appendLog("已停止推流")
    }

    /// 只重连 WebSocket，不动摄像头（用于网络抖动后手动重试）。
    func reconnectOnly() {
        guard let url = FrameStreamClient.makeURL(host: host, port: parsedPort, path: path) else {
            errorText = "服务器地址不合法"
            return
        }
        client.disconnect()
        client.connect(to: url)
        appendLog("手动重连：\(url.absoluteString)")
    }

    // MARK: - 日志

    private func appendLog(_ text: String) {
        let line = "[\(Self.timeFormatter.string(from: Date()))] \(text)"
        logs.append(line)
        if logs.count > 60 { logs.removeFirst(logs.count - 60) }
    }
}

// MARK: - CameraCaptureServiceDelegate

extension StreamViewModel: CameraCaptureServiceDelegate {

    /// 采集线程回调 → 立刻切主线程，然后上行 + 更新 UI。
    func cameraCapture(
        _ service: CameraCaptureService,
        didProduceJPEG jpeg: Data,
        width: Int,
        height: Int
    ) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }

            // 上行。未连接时 FrameStreamClient 内部直接丢弃 —— 不缓存旧帧。
            self.client.send(jpeg: jpeg)

            self.previewImage = UIImage(data: jpeg)
            self.resolutionText = "\(width)×\(height) · \(jpeg.count / 1024) KB"

            // 实测出帧间隔（最近 8 帧的均值）
            let now = Date().timeIntervalSinceReferenceDate
            self.recentFrameTimes.append(now)
            if self.recentFrameTimes.count > 8 { self.recentFrameTimes.removeFirst() }
            if self.recentFrameTimes.count >= 3,
               let first = self.recentFrameTimes.first,
               let last = self.recentFrameTimes.last,
               last > first {
                self.measuredInterval = (last - first) / Double(self.recentFrameTimes.count - 1)
            }
        }
    }

    func cameraCapture(_ service: CameraCaptureService, didFailWith message: String) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.errorText = message
            self.cameraNote = "采集异常"
            self.appendLog("采集异常：\(message)")
        }
    }
}
