//
//  ContentView.swift
//  BSACameraStreamer
//
//  界面：服务器地址配置 + 连接状态 + 推流统计 + 最近一帧预览 + 运行日志。
//
//  这块界面就是"联调仪表盘"：现场遇到问题时，看一眼连接状态、
//  已发送帧数与服务器确认帧数，就能判断是摄像头没出帧、网络不通，还是服务端在拒绝。
//

import SwiftUI

struct ContentView: View {

    @ObservedObject var model: StreamViewModel

    private let fpsOptions: [Double] = [0.5, 1.0, 2.0, 3.0, 5.0]

    var body: some View {
        NavigationView {
            ScrollView {
                VStack(spacing: 16) {
                    statusCard
                    serverCard
                    actionButtons
                    statsCard
                    previewCard
                    logCard
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            }
            .background(Color(.systemGroupedBackground).ignoresSafeArea())
            .navigationTitle("BSA 之眼")
            .navigationBarTitleDisplayMode(.inline)
        }
        .navigationViewStyle(.stack)
    }

    // MARK: - 连接状态

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Circle()
                    .fill(model.connectionActive ? Color.green : Color.orange)
                    .frame(width: 12, height: 12)
                Text(model.connectionLabel)
                    .font(.headline)
                Spacer()
                if model.isStreaming {
                    Text("推流中")
                        .font(.caption).bold()
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Color.blue.opacity(0.15))
                        .foregroundColor(.blue)
                        .clipShape(Capsule())
                }
            }

            Text(model.targetDescription)
                .font(.system(.footnote, design: .monospaced))
                .foregroundColor(.secondary)

            Text(model.cameraNote)
                .font(.caption)
                .foregroundColor(.secondary)

            if let error = model.errorText {
                Text(error)
                    .font(.caption)
                    .foregroundColor(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    // MARK: - 服务器设置

    private var serverCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("服务器")
                .font(.headline)

            HStack {
                Text("IP").frame(width: 42, alignment: .leading).foregroundColor(.secondary)
                TextField("例如 192.168.1.23", text: $model.host)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled(true)
                    .keyboardType(.numbersAndPunctuation)
                    .font(.system(.body, design: .monospaced))
            }

            HStack {
                Text("端口").frame(width: 42, alignment: .leading).foregroundColor(.secondary)
                TextField("8000", text: $model.portText)
                    .keyboardType(.numberPad)
                    .font(.system(.body, design: .monospaced))
            }

            HStack {
                Text("路径").frame(width: 42, alignment: .leading).foregroundColor(.secondary)
                TextField("/ws/camera", text: $model.path)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled(true)
                    .font(.system(.body, design: .monospaced))
            }

            HStack {
                Text("帧率").frame(width: 42, alignment: .leading).foregroundColor(.secondary)
                Picker("", selection: $model.fps) {
                    ForEach(fpsOptions, id: \.self) { value in
                        Text(value == 5.0 ? "5.0 fps（默认）" : String(format: "%.1f fps", value))
                            .tag(value)
                    }
                }
                .pickerStyle(.menu)
                .disabled(model.isStreaming)
                Spacer()
            }

            Text("在电脑上执行  python main.py --serve ，然后把启动时打印的「局域网」IP 填到这里。手机与电脑必须在同一个 WiFi 下。")
                .font(.caption2)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    // MARK: - 操作按钮

    private var actionButtons: some View {
        HStack(spacing: 12) {
            Button {
                if model.isStreaming {
                    model.stopStreaming()
                } else {
                    model.startStreaming()
                }
            } label: {
                Text(model.isStreaming ? "停止推流" : "开始推流")
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
            }
            .background(model.isStreaming ? Color.red : Color.blue)
            .foregroundColor(.white)
            .clipShape(RoundedRectangle(cornerRadius: 10))

            Button {
                model.reconnectOnly()
            } label: {
                Text("重连")
                    .font(.headline)
                    .frame(width: 88)
                    .padding(.vertical, 12)
            }
            .background(Color(.tertiarySystemGroupedBackground))
            .foregroundColor(.primary)
            .clipShape(RoundedRectangle(cornerRadius: 10))
        }
    }

    // MARK: - 统计

    private var statsCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("链路统计")
                .font(.headline)

            statRow("已发送帧", "\(model.sentFrames)")
            statRow("服务器确认", "\(model.ackedFrames) 帧")
            statRow("累计流量", byteText(model.bytesSent))
            statRow("最近帧大小", model.lastFrameBytes == 0 ? "-" : "\(model.lastFrameBytes / 1024) KB")
            statRow("分辨率", model.resolutionText)
            statRow("实测间隔", model.measuredInterval.map { String(format: "%.2f s", $0) } ?? "-")
            statRow("往返时延", model.rttMs.map { String(format: "%.0f ms", $0) } ?? "-")
            statRow("最近发送", model.lastSentAt.map { Self.clock.string(from: $0) } ?? "-")
            if let maxFPS = model.serverMaxFPS {
                statRow("服务端限速", "\(maxFPS) fps")
            }

            if model.sentFrames > 0 && model.ackedFrames == 0 {
                Text("已发帧但服务端未确认：检查 IP / 端口是否正确，以及电脑防火墙是否放行。")
                    .font(.caption2)
                    .foregroundColor(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private func statRow(_ key: String, _ value: String) -> some View {
        HStack {
            Text(key).font(.subheadline).foregroundColor(.secondary)
            Spacer()
            Text(value)
                .font(.system(.subheadline, design: .monospaced))
                .foregroundColor(.primary)
        }
    }

    private func byteText(_ bytes: Int) -> String {
        if bytes < 1024 { return "\(bytes) B" }
        if bytes < 1024 * 1024 { return String(format: "%.1f KB", Double(bytes) / 1024) }
        return String(format: "%.2f MB", Double(bytes) / 1024 / 1024)
    }

    // MARK: - 预览

    private var previewCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("最近一帧（即实际上行内容）")
                .font(.headline)

            if let image = model.previewImage {
                Image(uiImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .frame(maxWidth: .infinity)
                    .background(Color.black)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            } else {
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color(.tertiarySystemGroupedBackground))
                    .frame(height: 160)
                    .overlay(Text("等待第一帧").foregroundColor(.secondary))
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    // MARK: - 日志

    private var logCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("运行日志").font(.headline)
                Spacer()
                Text("最近 \(model.logs.count) 条").font(.caption).foregroundColor(.secondary)
            }

            if model.logs.isEmpty {
                Text("暂无日志").font(.caption).foregroundColor(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(model.logs.suffix(14).enumerated()), id: \.offset) { _, line in
                        Text(line)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundColor(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private static let clock: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()
}

// 说明：这里刻意不写 #Preview 宏。
// 本机 Xcode 的 swift-plugin-server 无法启动（宏展开失败），一旦写上宏，
// 整个工程在命令行与 Xcode 中都会编译失败。需要预览时，在 ContentView
// 末尾手动加上 #Preview { ContentView(model: StreamViewModel()) } 即可。
