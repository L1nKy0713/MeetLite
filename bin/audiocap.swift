// audiocap — 用 ScreenCaptureKit 抓系统音频，输出 16k/mono/s16le 原始 PCM 到 stdout。
// 编译：swiftc -O -o bin/audiocap bin/audiocap.swift -framework ScreenCaptureKit -framework CoreMedia
import Foundation
import ScreenCaptureKit
import CoreMedia
import CoreGraphics

func die(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

func argValue(_ name: String, _ def: Int) -> Int {
    let a = CommandLine.arguments
    if let i = a.firstIndex(of: name), i + 1 < a.count, let v = Int(a[i + 1]) { return v }
    return def
}

let out = FileHandle.standardOutput
let debug = ProcessInfo.processInfo.environment["AUDIOCAP_DEBUG"] != nil
func dbg(_ m: String) {
    if debug { FileHandle.standardError.write((m + "\n").data(using: .utf8)!) }
}

// 必须全局持有，否则 Task 结束后 SCStream 被释放，音频回调随即停止
var activeStream: SCStream?

final class Sink: NSObject, SCStreamOutput, SCStreamDelegate {
    var frames: UInt64 = 0

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid, CMSampleBufferGetNumSamples(sb) > 0 else { return }
        guard let fmt = CMSampleBufferGetFormatDescription(sb),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fmt)?.pointee
        else { return }

        var abl = AudioBufferList()
        var block: CMBlockBuffer?
        let st = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb,
            bufferListSizeNeededOut: nil,
            bufferListOut: &abl,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &block
        )
        guard st == noErr else { return }

        let isFloat = (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        let bufs = UnsafeMutableAudioBufferListPointer(&abl)
        var pcm = Data()
        for b in bufs {
            guard let data = b.mData else { continue }
            let n = Int(b.mDataByteSize)
            if isFloat {
                let f = data.assumingMemoryBound(to: Float.self)
                let count = n / MemoryLayout<Float>.size
                pcm.reserveCapacity(pcm.count + count * 2)
                for i in 0..<count {
                    var v = f[i]
                    if v.isNaN { v = 0 }
                    v = max(-1.0, min(1.0, v))
                    var s = Int16(v * 32767.0)
                    withUnsafeBytes(of: &s) { pcm.append(contentsOf: $0) }
                }
            } else {
                pcm.append(Data(bytes: data, count: n - (n % 2)))
            }
        }
        if debug {
            dbg("cb type=\(type.rawValue) fmt=\(isFloat ? "f32" : "s16") ch=\(asbd.mChannelsPerFrame) rate=\(asbd.mSampleRate) bytes=\(pcm.count)")
        }
        if pcm.isEmpty { return }
        do {
            try out.write(contentsOf: pcm)
            frames += 1
        } catch {
            exit(0)   // 父进程关闭了管道
        }
    }
}

let rate = argValue("--rate", 16000)

// --check：只报告权限状态，不采集
if CommandLine.arguments.contains("--check") {
    print(CGPreflightScreenCaptureAccess() ? "granted" : "denied")
    exit(0)
}

// --request：主动向系统申请屏幕录制权限。
// 这条必须存在：应用每次重新构建后 ad-hoc 签名（CDHash）都会变，
// 系统设置里那条旧记录虽然还显示「已勾选」，但 TCC 的 csreq 已经对不上新二进制，
// 于是 preflight 返回 false 而用户完全看不出问题。只有真正调用一次
// CGRequestScreenCaptureAccess()，系统才会为当前二进制登记权限并弹出对话框。
if CommandLine.arguments.contains("--request") {
    let ok = CGPreflightScreenCaptureAccess()
    if ok {
        print("granted")
        exit(0)
    }
    let granted = CGRequestScreenCaptureAccess()
    print(granted ? "granted" : "requested")
    exit(0)
}

guard CGPreflightScreenCaptureAccess() else {
    _ = CGRequestScreenCaptureAccess()
    die("NEED_SCREEN_PERMISSION")
}

// 父进程关闭 stdin 即退出
DispatchQueue.global().async {
    while true {
        let b = FileHandle.standardInput.availableData
        if b.isEmpty { exit(0) }
    }
}

let sink = Sink()
let queue = DispatchQueue(label: "audiocap.audio", qos: .userInitiated)
let sem = DispatchSemaphore(value: 0)

Task {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { die("没有可捕获的显示器") }
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true
        cfg.sampleRate = rate
        cfg.channelCount = 1
        cfg.queueDepth = 5
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        cfg.showsCursor = false

        let stream = SCStream(filter: filter, configuration: cfg, delegate: sink)
        try stream.addStreamOutput(sink, type: .audio, sampleHandlerQueue: queue)
        activeStream = stream
        try await stream.startCapture()
        FileHandle.standardError.write("audiocap: 开始录音 \(rate)Hz mono\n".data(using: .utf8)!)
        sem.signal()
    } catch {
        die("启动系统音频采集失败: \(error)")
    }
}

sem.wait()
RunLoop.main.run()