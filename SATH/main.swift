import Cocoa
import WebKit
import CoreLocation

class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKScriptMessageHandler, CLLocationManagerDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var bridgeProcess: Process?
    var locationManager: CLLocationManager?

    // 语雀登录弹窗（NSPanel 不触发 applicationShouldTerminateAfterLastWindowClosed）
    var yuqueWindow: NSPanel?
    var yuqueWebView: WKWebView?
    var shimoWindow: NSPanel?
    var shimoWebView: WKWebView?
    var feishuWindow: NSPanel?
    var feishuWebView: WKWebView?
    var feishuAppId: String = ""
    var feishuAppSecret: String = ""

    func applicationDidFinishLaunching(_ notification: Notification) {
        startBridge()
        startLocationUpdates()

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 960, height: 700),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.backgroundColor = NSColor(red: 0.949, green: 0.941, blue: 0.922, alpha: 1.0)
        window.minSize = NSSize(width: 800, height: 600)
        window.center()
        window.title = "SATH"

        let toolbar = NSToolbar(identifier: "main")
        toolbar.showsBaselineSeparator = false
        window.toolbar = toolbar
        window.toolbarStyle = .unified

        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        config.setValue(true, forKey: "allowUniversalAccessFromFileURLs")
        // 注册 JS → Swift 消息通道
        config.userContentController.add(self, name: "sath")

        webView = WKWebView(frame: window.contentView!.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.setValue(false, forKey: "drawsBackground")
        webView.uiDelegate = self
        window.contentView?.addSubview(webView)

        let htmlURL: URL
        if let bundled = Bundle.main.url(forResource: "index", withExtension: "html") {
            htmlURL = bundled
        } else {
            let dir = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
            htmlURL = dir.appendingPathComponent("index.html")
        }
        webView.loadFileURL(htmlURL, allowingReadAccessTo: htmlURL.deletingLastPathComponent())

        NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown]) { [weak self] event in
            guard let self = self, let contentView = self.window.contentView else { return event }
            let location = event.locationInWindow
            let contentHeight = contentView.frame.height
            if location.y > contentHeight - 52 {
                if location.x < 80 { return event }
                self.window.performDrag(with: event)
                return nil
            }
            return event
        }

        let mainMenu = NSMenu()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 SATH", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 SATH", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let appMenuItem = NSMenuItem(); appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)

        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let editMenuItem = NSMenuItem(); editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)

        NSApplication.shared.mainMenu = mainMenu
        window.makeKeyAndOrderFront(nil)
    }

    // ── JS → Swift 消息处理 ──
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "sath",
              let body = message.body as? [String: Any],
              let action = body["action"] as? String else { return }

        DispatchQueue.main.async {
            if action == "yuque_login" {
                self.openYuqueLogin()
            } else if action == "shimo_login" {
                self.openShimoLogin()
            } else if action == "feishu_login" {
                let appId     = body["appId"]     as? String ?? ""
                let appSecret = body["appSecret"] as? String ?? ""
                self.feishuAppSecret = appSecret
                self.openFeishuLogin(appId: appId)
            }
        }
    }

    // ── 语雀登录弹窗 ──
    func openYuqueLogin() {
        // 已有弹窗则置前
        if let w = yuqueWindow { w.makeKeyAndOrderFront(nil); return }

        let yuqueCfg = WKWebViewConfiguration()
        let yv = WKWebView(frame: NSRect(x: 0, y: 0, width: 480, height: 660), configuration: yuqueCfg)
        yv.navigationDelegate = self

        // 用 NSPanel 而非 NSWindow，关闭时不触发 applicationShouldTerminateAfterLastWindowClosed
        let win = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 660),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        win.title = "登录语雀"
        win.contentView = yv
        win.center()
        win.makeKeyAndOrderFront(nil)

        yv.load(URLRequest(url: URL(string: "https://www.yuque.com/login")!))

        yuqueWindow = win
        yuqueWebView = yv
    }

    // ── 飞书 OAuth 登录弹窗 ──
    func openFeishuLogin(appId: String) {
        feishuAppId = appId
        if let w = feishuWindow { w.makeKeyAndOrderFront(nil); return }

        let cfg = WKWebViewConfiguration()
        let fv = WKWebView(frame: NSRect(x: 0, y: 0, width: 480, height: 680), configuration: cfg)
        fv.navigationDelegate = self

        let win = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 680),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        win.title = "登录飞书"
        win.contentView = fv
        win.center()
        win.makeKeyAndOrderFront(nil)

        let redirectURI = "http://127.0.0.1:4800/api/feishu_callback"
        let scope = "wiki:wiki:readonly drive:drive:readonly offline_access"
        var comps = URLComponents(string: "https://open.feishu.cn/open-apis/authen/v1/authorize")!
        comps.queryItems = [
            URLQueryItem(name: "app_id", value: appId),
            URLQueryItem(name: "redirect_uri", value: redirectURI),
            URLQueryItem(name: "scope", value: scope),
            URLQueryItem(name: "state", value: "sath")
        ]
        if let url = comps.url {
            fv.load(URLRequest(url: url))
        }

        feishuWindow = win
        feishuWebView = fv
    }

    // ── 石墨文档登录弹窗 ──
    func openShimoLogin() {
        if let w = shimoWindow { w.makeKeyAndOrderFront(nil); return }

        let shimoCfg = WKWebViewConfiguration()
        let sv = WKWebView(frame: NSRect(x: 0, y: 0, width: 480, height: 660), configuration: shimoCfg)
        sv.navigationDelegate = self

        let win = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 660),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        win.title = "登录石墨文档"
        win.contentView = sv
        win.center()
        win.makeKeyAndOrderFront(nil)

        sv.load(URLRequest(url: URL(string: "https://shimo.im/login")!))

        shimoWindow = win
        shimoWebView = sv
    }

    // ── CoreLocation：获取用户位置并推送给 bridge ──
    func startLocationUpdates() {
        let mgr = CLLocationManager()
        mgr.delegate = self
        mgr.desiredAccuracy = kCLLocationAccuracyKilometer
        locationManager = mgr
        switch mgr.authorizationStatus {
        case .notDetermined:
            mgr.requestWhenInUseAuthorization()
        case .authorized, .authorizedAlways:
            mgr.requestLocation()
        default:
            print("[SATH] location permission denied, skipping")
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        if manager.authorizationStatus == .authorized ||
           manager.authorizationStatus == .authorizedAlways {
            manager.requestLocation()
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let loc = locations.first else { return }
        // 反地理编码：坐标 → 城市名
        let geocoder = CLGeocoder()
        geocoder.reverseGeocodeLocation(loc) { placemarks, error in
            guard let pm = placemarks?.first, error == nil else { return }
            let city    = pm.locality ?? pm.administrativeArea ?? ""
            let region  = pm.administrativeArea ?? ""
            let country = pm.country ?? ""
            let lat     = loc.coordinate.latitude
            let lng     = loc.coordinate.longitude
            print("[SATH] location: \(city), \(region), \(country) (\(lat), \(lng))")
            // POST 到 bridge 存库
            guard let url = URL(string: "http://127.0.0.1:4800/api/context/location") else { return }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            let payload: [String: Any] = [
                "city": city, "region": region, "country": country,
                "lat": lat, "lng": lng
            ]
            req.httpBody = try? JSONSerialization.data(withJSONObject: payload)
            URLSession.shared.dataTask(with: req) { _, _, _ in }.resume()
        }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        print("[SATH] location error: \(error.localizedDescription)")
    }

    func startBridge() {
        let resourcePath: String
        if let bundlePath = Bundle.main.resourcePath {
            resourcePath = bundlePath
        } else {
            resourcePath = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent().path
        }

        let bridgePath = (resourcePath as NSString).appendingPathComponent("bridge.py")
        guard FileManager.default.fileExists(atPath: bridgePath) else {
            print("[SATH] bridge.py not found at \(bridgePath)")
            return
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [bridgePath]
        process.environment = ProcessInfo.processInfo.environment
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice

        do {
            try process.run()
            bridgeProcess = process
            print("[SATH] bridge started (PID: \(process.processIdentifier))")
            Thread.sleep(forTimeInterval: 1.5)
        } catch {
            print("[SATH] bridge start failed: \(error)")
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let process = bridgeProcess, process.isRunning {
            process.terminate()
            print("[SATH] bridge terminated")
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ application: NSApplication) -> Bool {
        return true
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.runModal()
        completionHandler()
    }
}

// ── 语雀登录检测：导航完成后检查 cookie ──
extension AppDelegate: WKNavigationDelegate {
    // ── 飞书 OAuth：拦截 redirect 到 127.0.0.1:4800 的导航，提取 code ──
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard webView == feishuWebView,
              let url = navigationAction.request.url,
              url.host == "127.0.0.1", url.port == 4800 else {
            decisionHandler(.allow); return
        }
        // 拦截，不让 WKWebView 真的发请求
        decisionHandler(.cancel)
        guard let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let code = comps.queryItems?.first(where: { $0.name == "code" })?.value else { return }

        // 用 bridge 交换 token
        let appId     = self.feishuAppId
        let appSecret = self.feishuAppSecret
        guard let bridgeURL = URL(string: "http://127.0.0.1:4800/api/feishu_exchange") else { return }
        var req = URLRequest(url: bridgeURL)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let payload = ["code": code, "app_id": appId, "app_secret": appSecret]
        req.httpBody = try? JSONSerialization.data(withJSONObject: payload)

        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self = self, let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let token = json["token"] as? String else { return }
            let escaped = token.replacingOccurrences(of: "\\", with: "\\\\")
                               .replacingOccurrences(of: "'", with: "\\'")
            let js = "feishuLoginCallback('\(escaped)')"
            DispatchQueue.main.async {
                self.webView.evaluateJavaScript(js, completionHandler: nil)
                self.feishuWindow?.close()
                self.feishuWindow = nil
                self.feishuWebView = nil
            }
        }.resume()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // ── 语雀登录检测 ──
        if webView == yuqueWebView,
           let urlStr = webView.url?.absoluteString,
           urlStr.contains("yuque.com"),
           !urlStr.contains("/login") {
            webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { [weak self] cookies in
                guard let self = self else { return }
                guard let session = cookies.first(where: { $0.name == "_yuque_session" }) else { return }
                let cookieStr = "_yuque_session=\(session.value)"
                let escaped = cookieStr.replacingOccurrences(of: "\\", with: "\\\\")
                                       .replacingOccurrences(of: "'", with: "\\'")
                let js = "yuqueLoginCallback('\(escaped)')"
                DispatchQueue.main.async {
                    self.webView.evaluateJavaScript(js, completionHandler: nil)
                    self.yuqueWindow?.close()
                    self.yuqueWindow = nil
                    self.yuqueWebView = nil
                }
            }
        }

        // ── 石墨登录检测 ──
        if webView == shimoWebView,
           let urlStr = webView.url?.absoluteString,
           urlStr.contains("shimo.im"),
           !urlStr.contains("/login") {
            webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { [weak self] cookies in
                guard let self = self else { return }
                // 收集所有 shimo.im 相关 cookie
                let shimoCookies = cookies.filter { $0.domain.contains("shimo.im") }
                guard !shimoCookies.isEmpty else { return }
                let cookieStr = shimoCookies.map { "\($0.name)=\($0.value)" }.joined(separator: "; ")
                let escaped = cookieStr.replacingOccurrences(of: "\\", with: "\\\\")
                                       .replacingOccurrences(of: "'", with: "\\'")
                let js = "shimoLoginCallback('\(escaped)')"
                DispatchQueue.main.async {
                    self.webView.evaluateJavaScript(js, completionHandler: nil)
                    self.shimoWindow?.close()
                    self.shimoWindow = nil
                    self.shimoWebView = nil
                }
            }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
