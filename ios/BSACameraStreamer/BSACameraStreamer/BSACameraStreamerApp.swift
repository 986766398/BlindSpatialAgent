//
//  BSACameraStreamerApp.swift
//  BSACameraStreamer
//
//  BlindSpatialAgent · iPhone 实时摄像头图片流客户端
//
//  职责（只做这三件事）：
//      1. 打开后置摄像头
//      2. 每秒 1 帧编码成 JPEG
//      3. 通过 WebSocket 推送到电脑上的 Python 服务端，并显示连接状态
//
//  刻意不做的事：
//      - 不做视频理解、不做目标检测、不做推理（全部由 Python 侧 Agent 负责）
//      - 不做本地缓存与补偿，图片流宁缺毋滥：旧帧直接丢弃
//
//  这样保证本 App 是纯粹的「传感器」，未来换成智能眼镜只需替换本层。
//

import SwiftUI

@main
struct BSACameraStreamerApp: App {

    /// 全局唯一的推流控制器。App 生命周期内保持存活，避免切后台重建导致断流。
    @StateObject private var model = StreamViewModel()

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                .onAppear { model.loadSavedSettings() }
        }
    }
}
