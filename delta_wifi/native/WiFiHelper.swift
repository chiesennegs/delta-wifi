import AppKit
import CoreLocation
import CoreWLAN

final class Delegate: NSObject, NSApplicationDelegate, CLLocationManagerDelegate {
    let manager = CLLocationManager()
    var finished = false
    var output: String?
    var requesting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        let args = CommandLine.arguments
        if let index = args.firstIndex(of: "--output"), index + 1 < args.count {
            output = args[index + 1]
        }
        requesting = args.contains("--request-permission")
        manager.delegate = self
        if requesting && manager.authorizationStatus == .notDetermined && CLLocationManager.locationServicesEnabled() {
            NSApp.activate(ignoringOtherApps: true)
            manager.requestWhenInUseAuthorization()
            DispatchQueue.main.asyncAfter(deadline: .now() + 45) { self.finish() }
        } else {
            finish()
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        if output != nil && manager.authorizationStatus != .notDetermined {
            // Allow the Wi-Fi service a moment to observe newly granted authorization.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.finish() }
        }
    }

    func finish() {
        guard !finished, let path = output else { return }
        finished = true
        let interface = CWWiFiClient.shared().interface()
        let status: String
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse: status = "authorized"
        case .denied: status = "denied"
        case .restricted: status = "restricted"
        case .notDetermined: status = "not-determined"
        @unknown default: status = "unknown"
        }
        let result: [String: Any] = [
            "ssid": interface?.ssid() as Any? ?? NSNull(),
            "interface": interface?.interfaceName as Any? ?? NSNull(),
            "power_on": interface?.powerOn() ?? false,
            "location_services": CLLocationManager.locationServicesEnabled(),
            "authorization": status
        ]
        do {
            let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
            try data.write(to: URL(fileURLWithPath: path), options: .atomic)
        } catch { }
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
