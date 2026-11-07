import AppKit
import WebKit

// 页面主题 → 原生窗口外观（标题栏、底色的明暗）
final class ThemeHandler: NSObject, WKScriptMessageHandler {
    weak var app: AppDelegate?
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let mode = m.body as? String else { return }
        DispatchQueue.main.async { self.app?.applyChromeTheme(mode) }
    }
}

// MeetLite 原生壳：拉起内置 Python 服务 → 等就绪 → WKWebView 打开界面
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var proc: Process?
    var port: Int = 0
    var ready = false
    var themeHandler: ThemeHandler?
    var themeMode = "auto"

    // MARK: 生命周期
    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        buildWindow()
        showLoading("正在启动本地引擎…")
        DispatchQueue.global().async { self.boot() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ note: Notification) {
        if let p = proc, p.isRunning {
            kill(-p.processIdentifier, SIGTERM)   // 结束整个进程组（python + whisper-server + llama-helper）
            let deadline = Date().addingTimeInterval(3)
            while p.isRunning && Date() < deadline { usleep(50_000) }
            if p.isRunning { kill(-p.processIdentifier, SIGKILL) }
        }
    }

    // MARK: 界面
    func buildWindow() {
        let rect = NSRect(x: 0, y: 0, width: 1180, height: 760)
        window = NSWindow(contentRect: rect,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "MeetLite"
        window.titlebarAppearsTransparent = false
        themeMode = Self.savedTheme()
        window.minSize = NSSize(width: 900, height: 600)
        window.center()
        window.setFrameAutosaveName("MeetLiteMain")

        let cfg = WKWebViewConfiguration()
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        themeHandler = ThemeHandler()
        themeHandler?.app = self
        cfg.userContentController.add(themeHandler!, name: "theme")
        web = WKWebView(frame: rect, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        window.contentView = web
        applyChromeTheme(themeMode)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    static func savedTheme() -> String {
        let p = ("~/Library/Application Support/MeetLite/config.json" as NSString).expandingTildeInPath
        guard let d = FileManager.default.contents(atPath: p),
              let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let t = o["theme"] as? String else { return "auto" }
        return t
    }

    static func systemIsDark() -> Bool {
        NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
    }

    /// 页面选白天/黑夜时同步标题栏与窗口底色；auto 跟随系统
    func applyChromeTheme(_ mode: String) {
        themeMode = mode
        let dark: Bool
        switch mode {
        case "dark": dark = true
        case "light": dark = false
        default: dark = Self.systemIsDark()
        }
        if let w = window {
            w.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
            w.backgroundColor = dark
                ? NSColor(calibratedRed: 0.086, green: 0.090, blue: 0.094, alpha: 1)
                : NSColor(calibratedRed: 0.969, green: 0.965, blue: 0.953, alpha: 1)
        }
    }

    func showLoading(_ text: String) {
        let dark: Bool = themeMode == "dark" || (themeMode == "auto" && Self.systemIsDark())
        let bg = dark ? "#161718" : "#f7f6f3"
        let fg = dark ? "#e9e9ec" : "#3a3a38"
        let sub = dark ? "#989aa0" : "#8a8880"
        let html = """
        <html><head><meta charset="utf-8"><style>
        body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
        background:\(bg);color:\(fg);font:14px/1.7 -apple-system,"PingFang SC",sans-serif}
        .box{text-align:center}
        .dot{width:8px;height:8px;border-radius:50%;background:#3b5bdb;display:inline-block;
        animation:b 1.1s infinite ease-in-out}
        .dot:nth-child(2){animation-delay:.15s}.dot:nth-child(3){animation-delay:.3s}
        @keyframes b{0%,80%,100%{opacity:.15;transform:translateY(0)}40%{opacity:1;transform:translateY(-4px)}}
        h1{font-size:15px;font-weight:600;letter-spacing:.3px;margin:0 0 14px}
        p{margin:0;color:\(sub);font-size:12.5px}
        </style></head><body><div class="box">
        <h1>MeetLite</h1>
        <div><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
        <p style="margin-top:14px">\(text)</p></div></body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 MeetLite", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        let open = appMenu.addItem(withTitle: "打开数据目录", action: #selector(openDataDir), keyEquivalent: "")
        open.target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 MeetLite", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出 MeetLite", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let viewMenu = NSMenu(title: "显示")
        let reload = viewMenu.addItem(withTitle: "重新载入界面", action: #selector(reloadUI), keyEquivalent: "r")
        reload.target = self
        viewItem.submenu = viewMenu
        NSApp.mainMenu = main
    }

    @objc func openDataDir() {
        let p = ("~/Library/Application Support/MeetLite" as NSString).expandingTildeInPath
        try? FileManager.default.createDirectory(atPath: p, withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: p))
    }

    @objc func reloadUI() { if ready { web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!)) } }

    // MARK: 启动 Python 服务
    func resources() -> String {
        Bundle.main.bundlePath + "/Contents/Resources/app"
    }

    func pythonPath() -> String {
        for c in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: c) { return c }
        return "/usr/bin/python3"
    }

    func freePort() -> Int {
        // 让内核挑一个空闲端口再释放给 python 用
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        if fd < 0 { return 8787 }
        defer { Darwin.close(fd) }
        var a = sockaddr_in()
        a.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        a.sin_family = sa_family_t(AF_INET)
        a.sin_addr.s_addr = inet_addr("127.0.0.1")
        a.sin_port = 0
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafePointer(to: &a) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(fd, $0, len) }
        }
        if bound != 0 { return 8787 }
        let ok = withUnsafeMutablePointer(to: &a) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.getsockname(fd, $0, &len) }
        }
        if ok != 0 { return 8787 }
        let p = Int(UInt16(bigEndian: a.sin_port))
        return p == 0 ? 8787 : p
    }

    func boot() {
        let dir = resources()
        guard FileManager.default.fileExists(atPath: dir + "/server.py") else {
            fail("应用包缺少 Resources/app/server.py")
            return
        }
        port = freePort()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath())
        p.arguments = ["-u", "server.py", "--port", "\(port)"]
        p.currentDirectoryURL = URL(fileURLWithPath: dir)
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env

        let logDir = ("~/Library/Logs/MeetLite" as NSString).expandingTildeInPath
        try? FileManager.default.createDirectory(atPath: logDir, withIntermediateDirectories: true)
        let logURL = URL(fileURLWithPath: logDir + "/server.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let fh = try? FileHandle(forWritingTo: logURL) {
            fh.seekToEndOfFile()
            p.standardOutput = fh
            p.standardError = fh
        }
        p.terminationHandler = { proc in
            if self.ready { DispatchQueue.main.async { self.fail("本地服务已退出（code \(proc.terminationStatus)），详见 ~/Library/Logs/MeetLite/server.log") } }
        }
        do { try p.run() } catch { fail("无法启动 Python 服务: \(error.localizedDescription)"); return }
        proc = p

        // 等服务就绪
        let url = URL(string: "http://127.0.0.1:\(port)/api/system")!
        let deadline = Date().addingTimeInterval(90)
        while Date() < deadline {
            var ok = false
            let sem = DispatchSemaphore(value: 0)
            var req = URLRequest(url: url); req.timeoutInterval = 4
            URLSession.shared.dataTask(with: req) { data, resp, _ in
                if let h = resp as? HTTPURLResponse, h.statusCode == 200, let d = data, d.count > 2 { ok = true }
                sem.signal()
            }.resume()
            _ = sem.wait(timeout: .now() + 3)
            if ok { break }
            usleep(250_000)
        }
        DispatchQueue.main.async {
            self.ready = true
            self.web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(self.port)/")!))
        }
    }

    // MARK: 导航诊断
    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        NSLog("[ui] 开始加载 %@", webView.url?.absoluteString ?? "-")
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        NSLog("[ui] 加载完成 %@", webView.url?.absoluteString ?? "-")
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        NSLog("[ui] 加载失败 %@", error.localizedDescription)
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        NSLog("[ui] 预加载失败 %@", error.localizedDescription)
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        NSLog("[ui] 渲染进程终止，重新加载")
        webView.reload()
    }

    func fail(_ msg: String) {
        DispatchQueue.main.async {
            let a = NSAlert()
            a.messageText = "MeetLite 启动失败"
            a.informativeText = msg
            a.alertStyle = .warning
            a.addButton(withTitle: "好")
            a.runModal()
        }
    }

    // MARK: WKWebView 细节
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = "MeetLite"; a.informativeText = message
        a.addButton(withTitle: "好"); a.runModal(); completionHandler()
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.audio, .movie, .mpeg4Movie, .mp3, .wav]
        completionHandler(panel.runModal() == .OK ? panel.urls : nil)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
