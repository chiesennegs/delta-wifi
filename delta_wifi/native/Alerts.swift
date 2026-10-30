import AppKit
import UserNotifications

final class AlertsDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    var output: String?
    var done = false
    let center = UNUserNotificationCenter.current()

    func finish(_ result: [String: Any]) {
        guard !done else { return }
        done = true
        if let path = output, let data = try? JSONSerialization.data(withJSONObject: result) {
            try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
        }
        NSApp.terminate(nil)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--output"), i + 1 < args.count { output = args[i + 1] }
        center.delegate = self
        DispatchQueue.main.asyncAfter(deadline: .now() + 45) { self.finish(["state": "timeout"]) }
        if args.contains("--authorize") {
            NSApp.activate(ignoringOtherApps: true)
            center.requestAuthorization(options: [.alert, .sound]) { allowed, error in
                DispatchQueue.main.async { self.finish(["state": allowed ? "authorized" : "denied"]) }
            }
        } else {
            center.getNotificationSettings { settings in
                guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                    DispatchQueue.main.async { self.finish(["state": "not-authorized"]) }
                    return
                }
                let content = UNMutableNotificationContent()
                content.title = "Delta Wi-Fi"
                content.sound = .default
                if args.contains("--restored") {
                    content.body = "Internet access is working again after a sustained outage."
                } else if args.contains("--attention") {
                    content.body = "Automatic login is paused. Check delta-wifi for the next step."
                } else {
                    DispatchQueue.main.async { self.finish(["state": "invalid-event"]) }
                    return
                }
                let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
                self.center.add(request) { error in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                        self.finish(["state": error == nil ? "queued" : "failed"])
                    }
                }
            }
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}

let app = NSApplication.shared
let delegate = AlertsDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
