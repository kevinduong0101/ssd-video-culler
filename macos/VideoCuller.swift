import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var serverProcess: Process?
    private var activeURL: URL = URL(string: "http://127.0.0.1:8789/")!
    private let defaultPort = 8789

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMainMenu()
        setupWindow()
        NSApp.activate(ignoringOtherApps: true)
        connectOrLaunchServer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            window.makeKeyAndOrderFront(nil)
        }
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let proc = serverProcess, proc.isRunning {
            proc.terminate()
            proc.waitUntilExit()
        }
    }

    private func setupMainMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Video Culler", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Hide Video Culler", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Video Culler", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu

        let viewMenuItem = NSMenuItem()
        mainMenu.addItem(viewMenuItem)
        let viewMenu = NSMenu(title: "View")
        let reloadItem = NSMenuItem(title: "Reload", action: #selector(reloadPage), keyEquivalent: "r")
        reloadItem.target = self
        viewMenu.addItem(reloadItem)
        let fsItem = NSMenuItem(title: "Toggle Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        fsItem.keyEquivalentModifierMask = [.command, .control]
        viewMenu.addItem(fsItem)
        viewMenuItem.submenu = viewMenu

        let winMenuItem = NSMenuItem()
        mainMenu.addItem(winMenuItem)
        let winMenu = NSMenu(title: "Window")
        winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winMenuItem.submenu = winMenu

        NSApp.mainMenu = mainMenu
    }

    private func setupWindow() {
        let screenFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let w = min(1480, max(1080, screenFrame.width * 0.9))
        let h = min(920, max(700, screenFrame.height * 0.88))
        let rect = NSRect(
            x: screenFrame.origin.x + (screenFrame.width - w) / 2,
            y: screenFrame.origin.y + (screenFrame.height - h) / 2,
            width: w,
            height: h
        )

        window = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Video Culler"
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(calibratedRed: 0.043, green: 0.047, blue: 0.055, alpha: 1.0)
        window.minSize = NSSize(width: 760, height: 520)
        window.setFrameAutosaveName("VideoCullerMainWindow")

        let cfg = WKWebViewConfiguration()
        cfg.mediaTypesRequiringUserActionForPlayback = []
        webView = WKWebView(frame: window.contentView!.bounds, configuration: cfg)
        webView.autoresizingMask = [.width, .height]
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")

        let splash = """
        <!doctype html><html lang="vi"><head><meta charset="utf-8">
        <style>body{margin:0;height:100vh;display:grid;place-items:center;background:#0b0c0e;color:#8a93a2;font:500 13.5px -apple-system,system-ui,sans-serif;}</style>
        </head><body><div>Đang mở kho video…</div></body></html>
        """
        webView.loadHTMLString(splash, baseURL: nil)

        window.contentView?.addSubview(webView)
        window.makeKeyAndOrderFront(nil)
    }

    @objc private func reloadPage() {
        webView.load(URLRequest(url: activeURL))
    }

    static func resolveScriptPath() -> String? {
        if let resPath = Bundle.main.resourcePath {
            let bundled = (resPath as NSString).appendingPathComponent("app/culler.py")
            if FileManager.default.isReadableFile(atPath: bundled) {
                return bundled
            }
        }
        let execDir = ((CommandLine.arguments[0] as NSString).deletingLastPathComponent as NSString)
        let devFallback = execDir.appendingPathComponent("../culler.py")
        if FileManager.default.isReadableFile(atPath: devFallback) {
            return devFallback
        }
        return nil
    }

    static func probeServer(port: Int, timeout: TimeInterval = 0.35) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/copy-status") else { return false }
        var req = URLRequest(url: url)
        req.timeoutInterval = timeout
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            if let http = resp as? HTTPURLResponse, http.statusCode == 200,
               let data = data,
               let txt = String(data: data, encoding: .utf8),
               txt.contains("\"copiedCount\"") {
                ok = true
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 0.1)
        return ok
    }

    static func cleanEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let home = env["HOME"]?.isEmpty == false ? env["HOME"]! : NSHomeDirectory()
        env["HOME"] = home
        env["LANG"] = "en_US.UTF-8"
        env["LC_ALL"] = "en_US.UTF-8"
        env["PYTHONIOENCODING"] = "utf-8"
        let basePaths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        let existing = (env["PATH"] ?? "").split(separator: ":").map(String.init)
        var merged = basePaths
        for p in existing where !merged.contains(p) && !p.isEmpty {
            merged.append(p)
        }
        env["PATH"] = merged.joined(separator: ":")
        return env
    }

    private func connectOrLaunchServer() {
        DispatchQueue.global(qos: .userInitiated).async {
            if Self.probeServer(port: self.defaultPort) {
                DispatchQueue.main.async {
                    self.activeURL = URL(string: "http://127.0.0.1:\(self.defaultPort)/")!
                    self.webView.load(URLRequest(url: self.activeURL))
                }
                return
            }

            guard let script = Self.resolveScriptPath() else {
                DispatchQueue.main.async {
                    self.showError("Không tìm thấy culler.py trong gói ứng dụng.")
                }
                return
            }

            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            proc.arguments = [script, "--port", "\(self.defaultPort)", "--lazy-scan"]
            proc.environment = Self.cleanEnvironment()
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice

            do {
                try proc.run()
                self.serverProcess = proc
            } catch {
                DispatchQueue.main.async {
                    self.showError("Không thể khởi chạy máy chủ Python: \(error.localizedDescription)")
                }
                return
            }

            var detectedPort = self.defaultPort
            while proc.isRunning {
                if Self.probeServer(port: detectedPort, timeout: 0.25) {
                    let target = URL(string: "http://127.0.0.1:\(detectedPort)/")!
                    DispatchQueue.main.async {
                        self.activeURL = target
                        self.webView.load(URLRequest(url: target))
                    }
                    return
                }
                for alt in (self.defaultPort + 1)...(self.defaultPort + 5) {
                    if Self.probeServer(port: alt, timeout: 0.15) {
                        detectedPort = alt
                        break
                    }
                }
                Thread.sleep(forTimeInterval: 0.1)
            }

            DispatchQueue.main.async {
                self.showError("Máy chủ Python đã dừng bất ngờ.")
            }
        }
    }

    private func showError(_ msg: String) {
        let html = """
        <!doctype html><html lang="vi"><head><meta charset="utf-8">
        <style>body{margin:0;height:100vh;display:grid;place-items:center;background:#0b0c0e;color:#f1f3f5;font:14px -apple-system,system-ui,sans-serif;padding:24px;text-align:center;}</style>
        </head><body><div><p>\(msg)</p></div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }
}

// Headless CLI verification mode (`VideoCuller --self-test`) for empty `env -i` validation
if CommandLine.arguments.contains("--self-test") {
    let env = AppDelegate.cleanEnvironment()
    guard let script = AppDelegate.resolveScriptPath() else {
        fputs("SELF_TEST_FAIL: missing culler.py\n", stderr)
        exit(1)
    }
    let testPort = 18799
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    proc.arguments = [script, "--port", "\(testPort)", "--lazy-scan"]
    proc.environment = env
    proc.standardOutput = FileHandle.nullDevice
    proc.standardError = FileHandle.nullDevice
    try proc.run()
    defer {
        if proc.isRunning {
            proc.terminate()
            proc.waitUntilExit()
        }
    }
    var ready = false
    for _ in 0..<40 {
        if AppDelegate.probeServer(port: testPort, timeout: 0.2) {
            ready = true
            break
        }
        Thread.sleep(forTimeInterval: 0.1)
    }
    guard ready else {
        fputs("SELF_TEST_FAIL: server did not start under env -i\n", stderr)
        exit(2)
    }
    print("SELF_TEST_OK: script=\(script) home=\(env["HOME"] ?? "") lang=\(env["LANG"] ?? "") — Tiếng Việt UTF-8 chuẩn")
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
