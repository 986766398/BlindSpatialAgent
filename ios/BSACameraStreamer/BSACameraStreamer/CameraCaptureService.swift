//
//  CameraCaptureService.swift
//  BSACameraStreamer
//
//  摄像头采集层：负责「打开后置摄像头 → 按固定频率产出 JPEG 字节」。
//
//  设计要点：
//  1. 使用 AVCaptureVideoDataOutput 而不是 AVCapturePhotoOutput。
//     PhotoOutput 每次拍照有快门音与明显延迟（0.2~0.5s），做不到稳定高帧率；
//     VideoDataOutput 是连续帧回调，我们只在回调里做节流，最稳。
//  2. 节流在「编码之前」：目标 5 fps 就是每 200ms 只做一次 JPEG 编码，
//     而不是编 30 次丢 29 次 —— 省电，且不会因为编码积压导致帧越来越旧。
//  3. alwaysDiscardsLateVideoFrames = true：积压的帧直接丢，绝不排队。
//     对导航场景来说，一张 2 秒前的图比没有图更危险。
//  4. 输出分辨率 640x480 + JPEG 质量 0.5，单帧约 30~60 KB，
//     5 fps 下带宽约 1.5~2.5 Mbps，WiFi 上依然宽裕。
//  5. 模拟器没有摄像头：自动降级为合成帧源，保证整条链路仍可联调。
//

import AVFoundation
import CoreImage
import UIKit

/// 采集层回调协议。所有回调都在内部串行队列上触发，调用方需自行切主线程。
protocol CameraCaptureServiceDelegate: AnyObject {
    /// 产出一帧 JPEG（已按 interval 节流）。
    func cameraCapture(_ service: CameraCaptureService, didProduceJPEG jpeg: Data, width: Int, height: Int)
    /// 采集层致命错误：权限被拒、无法配置会话等。
    func cameraCapture(_ service: CameraCaptureService, didFailWith message: String)
}

enum CameraCaptureError: LocalizedError {
    case permissionDenied
    case cannotAddInput(String)
    case cannotAddOutput

    var errorDescription: String? {
        switch self {
        case .permissionDenied:
            return "摄像头权限被拒绝。请到「设置 → 隐私与安全性 → 摄像头」中允许 BSACameraStreamer。"
        case .cannotAddInput(let name):
            return "无法添加摄像头输入：\(name)"
        case .cannotAddOutput:
            return "无法添加视频数据输出（会话配置冲突）"
        }
    }
}

final class CameraCaptureService: NSObject {

    /// 出帧间隔（秒）。1.0 = 每秒 1 帧。修改后需重新 start() 才生效。
    var interval: TimeInterval = 1.0
    /// JPEG 压缩质量（0~1）。
    var jpegQuality: CGFloat = 0.5

    weak var delegate: CameraCaptureServiceDelegate?

    /// 采集 / 编码所在串行队列，避免占用主线程。
    private let queue = DispatchQueue(label: "com.bsa.camera.capture", qos: .userInitiated)
    private let session = AVCaptureSession()
    private let output = AVCaptureVideoDataOutput()
    /// CIContext 创建代价高（涉及 Metal 初始化），必须复用。
    private let ciContext = CIContext(options: [.useSoftwareRenderer: false])

    private var configured = false
    private var running = false
    private var lastEmitTime: CFTimeInterval = 0

    /// 模拟器降级用的合成帧定时器。
    private var syntheticTimer: DispatchSourceTimer?
    private var syntheticTick = 0

    // MARK: - 对外状态

    var isRunning: Bool { running }
    var authorizationStatus: AVAuthorizationStatus { AVCaptureDevice.authorizationStatus(for: .video) }
    /// 是否运行在「无摄像头」的降级模式（模拟器）。
    private(set) var isSynthetic = false

    // MARK: - 启动 / 停止

    /// 请求权限并启动采集。completion 在主线程回调。
    func start(completion: ((Result<Void, Error>) -> Void)? = nil) {
        switch authorizationStatus {
        case .authorized:
            queue.async { [weak self] in self?.startOnQueue(completion: completion) }

        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
                guard let self else { return }
                guard granted else {
                    DispatchQueue.main.async { completion?(.failure(CameraCaptureError.permissionDenied)) }
                    return
                }
                self.queue.async { self.startOnQueue(completion: completion) }
            }

        default:
            completion?(.failure(CameraCaptureError.permissionDenied))
        }
    }

    func stop() {
        queue.async { [weak self] in
            guard let self else { return }
            self.stopSynthetic()
            if self.session.isRunning { self.session.stopRunning() }
            self.running = false
        }
    }

    // MARK: - 内部：会话配置

    private func startOnQueue(completion: ((Result<Void, Error>) -> Void)?) {
        if !configured {
            do {
                try configureSession()
            } catch {
                DispatchQueue.main.async { completion?(.failure(error)) }
                return
            }
        }

        // 无可用摄像头（模拟器 / 未授权设备）→ 合成帧降级
        if session.inputs.isEmpty {
            isSynthetic = true
            startSynthetic()
            running = true
            DispatchQueue.main.async { completion?(.success(())) }
            return
        }

        isSynthetic = false
        if !session.isRunning { session.startRunning() }
        running = true
        DispatchQueue.main.async { completion?(.success(())) }
    }

    private func configureSession() throws {
        session.beginConfiguration()
        defer { session.commitConfiguration() }

        // 640x480：分辨率够看清地面障碍与门框，同时控制带宽与编码耗时
        session.sessionPreset = .vga640x480

        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else {
            // 模拟器没有摄像头。不抛错，交给合成帧路径处理。
            configured = true
            return
        }

        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else {
            throw CameraCaptureError.cannotAddInput(device.localizedName)
        }
        session.addInput(input)

        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ]
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)
        guard session.canAddOutput(output) else {
            throw CameraCaptureError.cannotAddOutput
        }
        session.addOutput(output)

        // 竖屏持机 + 后置摄像头：把输出旋转 90°，保证送出去的 JPEG 是正的。
        // （Agent 侧不做方向判断，方向必须在这里定死。）
        if let connection = output.connection(with: .video),
           connection.isVideoRotationAngleSupported(90) {
            connection.videoRotationAngle = 90
        }

        // 连续自动对焦 + 连续自动曝光：室内走动时画面不会忽明忽暗
        if let videoConnection = output.connection(with: .video), videoConnection.isEnabled {
            try? device.lockForConfiguration()
            if device.isFocusModeSupported(.continuousAutoFocus) {
                device.focusMode = .continuousAutoFocus
            }
            if device.isExposureModeSupported(.continuousAutoExposure) {
                device.exposureMode = .continuousAutoExposure
            }
            if device.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
                device.whiteBalanceMode = .continuousAutoWhiteBalance
            }
            device.unlockForConfiguration()
        }

        configured = true
    }

    // MARK: - 内部：模拟器合成帧

    private func startSynthetic() {
        stopSynthetic()
        syntheticTick = 0
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: max(0.2, interval))
        timer.setEventHandler { [weak self] in self?.emitSyntheticFrame() }
        syntheticTimer = timer
        timer.resume()
    }

    private func stopSynthetic() {
        syntheticTimer?.cancel()
        syntheticTimer = nil
    }

    /// 生成一张带时间戳与移动方块的合成图，行为特征与真实画面一致（会变化、有"障碍物"）。
    private func emitSyntheticFrame() {
        syntheticTick += 1
        let size = CGSize(width: 640, height: 480)
        let format = UIGraphicsImageRendererFormat.default()
        format.scale = 1                      // 关键：不要按屏幕倍率放大到 1920x1440
        format.opaque = true

        let tick = syntheticTick
        let clock = Self.clockFormatter.string(from: Date())

        let image = UIGraphicsImageRenderer(size: size, format: format).image { ctx in
            UIColor(white: 0.10, alpha: 1).setFill()
            ctx.fill(CGRect(origin: .zero, size: size))

            // 地板参考线
            UIColor(white: 0.22, alpha: 1).setFill()
            ctx.fill(CGRect(x: 0, y: 360, width: size.width, height: 3))

            // 来回移动的"障碍物"
            let x = 30 + CGFloat((tick * 43) % 520)
            UIColor(red: 0.24, green: 0.52, blue: 0.88, alpha: 1).setFill()
            ctx.fill(CGRect(x: x, y: 280, width: 90, height: 84))

            let text = "BSA SIMULATOR\nframe #\(tick)\n\(clock)"
            let attributes: [NSAttributedString.Key: Any] = [
                .font: UIFont.monospacedSystemFont(ofSize: 26, weight: .semibold),
                .foregroundColor: UIColor(white: 0.92, alpha: 1),
            ]
            text.draw(at: CGPoint(x: 28, y: 32), withAttributes: attributes)
        }

        guard let jpeg = image.jpegData(compressionQuality: jpegQuality) else { return }
        delegate?.cameraCapture(
            self,
            didProduceJPEG: jpeg,
            width: Int(size.width),
            height: Int(size.height)
        )
    }

    private static let clockFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()
}

// MARK: - 视频帧回调

extension CameraCaptureService: AVCaptureVideoDataOutputSampleBufferDelegate {

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        // ① 节流：1 fps 就是每 1000ms 处理一帧，多余的帧在这里直接返回
        let now = CACurrentMediaTime()
        guard now - lastEmitTime >= interval else { return }
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        lastEmitTime = now

        // ② CVPixelBuffer → CIImage → CGImage → UIImage
        let ciImage = CIImage(cvPixelBuffer: pixelBuffer)
        guard let cgImage = ciContext.createCGImage(ciImage, from: ciImage.extent) else { return }
        let uiImage = UIImage(cgImage: cgImage)

        // ③ JPEG 编码（单帧 640x480 约 1~3 ms，放在采集队列上不会掉帧）
        guard let jpeg = uiImage.jpegData(compressionQuality: jpegQuality) else { return }

        delegate?.cameraCapture(
            self,
            didProduceJPEG: jpeg,
            width: Int(uiImage.size.width),
            height: Int(uiImage.size.height)
        )
    }
}
