import Flutter
import UIKit
import UserNotifications

/// APNs transport and OS presentation. Business navigation stays in Flutter.
final class SystemNotificationBridge: NSObject, UNUserNotificationCenterDelegate {
  private var channel: FlutterMethodChannel?
  private var pending: [[String: Any]] = []
  private var initial: [String: Any]?
  private var ready = false
  private var token: String?
  private var context: [String: Any] = [:]

  func start(_ options: [UIApplication.LaunchOptionsKey: Any]?) {
    UNUserNotificationCenter.current().delegate = self
    if let payload = options?[.remoteNotification] as? [AnyHashable: Any] {
      opened(payload)
    }
    refreshRegistration()
  }

  func attach(_ messenger: FlutterBinaryMessenger) {
    guard channel == nil else { return }
    let channel = FlutterMethodChannel(name: "bossip/system_notifications", binaryMessenger: messenger)
    self.channel = channel
    channel.setMethodCallHandler { [weak self] call, result in
      guard let self else { return result(nil) }
      switch call.method {
      case "requestAuthorization":
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound]) { granted, _ in
          DispatchQueue.main.async {
            if granted { UIApplication.shared.registerForRemoteNotifications() }
            self.emit("authorizationChanged", ["status": granted ? "granted" : "denied"])
            result(granted)
          }
        }
      case "getAuthorizationStatus":
        UNUserNotificationCenter.current().getNotificationSettings { settings in
          DispatchQueue.main.async {
            let status = self.status(settings.authorizationStatus)
            if status == "granted" { UIApplication.shared.registerForRemoteNotifications() }
            result(status)
          }
        }
      case "getDeviceToken": result(self.token)
      case "getApnsEnvironment": result(self.environment)
      case "getAppVersion":
        result("\(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") ?? "")+\(Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") ?? "")")
      case "getInitialNotification":
        result(self.initial)
        self.initial = nil
      case "flushNotificationEvents":
        self.ready = true
        let events = self.pending
        self.pending.removeAll()
        result(events)
      case "setPresentationContext":
        self.context = call.arguments as? [String: Any] ?? [:]
        result(true)
      case "clearNotifications":
        self.initial = nil
        self.pending.removeAll { ($0["method"] as? String)?.hasPrefix("notification") == true }
        UNUserNotificationCenter.current().removeAllDeliveredNotifications()
        UNUserNotificationCenter.current().removeAllPendingNotificationRequests()
        result(true)
      case "openSettings":
        guard let url = URL(string: UIApplication.openSettingsURLString) else { return result(false) }
        UIApplication.shared.open(url) { result($0) }
      case "showLocalNotification":
        self.showLocal(call.arguments as? [String: Any] ?? [:], result)
      default: result(FlutterMethodNotImplemented)
      }
    }
  }

  func refreshRegistration() {
    UNUserNotificationCenter.current().getNotificationSettings { settings in
      DispatchQueue.main.async {
        let status = self.status(settings.authorizationStatus)
        self.emit("authorizationChanged", ["status": status])
        if status == "granted" { UIApplication.shared.registerForRemoteNotifications() }
      }
    }
  }

  func registered(_ data: Data) {
    token = data.map { String(format: "%02x", $0) }.joined()
    emit("deviceTokenUpdated", ["platform": "ios", "provider": "apns", "token": token ?? "",
                                 "apnsEnvironment": environment])
  }

  func registrationFailed() {
    emit("registrationFailed", ["error": "registration_failed"])
  }

  private func status(_ value: UNAuthorizationStatus) -> String {
    switch value {
    case .authorized, .provisional, .ephemeral: return "granted"
    case .denied: return "denied"
    default: return "notDetermined"
    }
  }

  private var environment: String {
    // Distribution and Development may both use Release; inspect the profile.
    if let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"),
       let data = try? Data(contentsOf: url),
       let start = data.range(of: Data("<?xml".utf8)),
       let end = data.range(of: Data("</plist>".utf8), in: start.lowerBound..<data.endIndex),
       let profile = try? PropertyListSerialization.propertyList(
          from: data.subdata(in: start.lowerBound..<end.upperBound), options: [], format: nil) as? [String: Any],
       let entitlements = profile["Entitlements"] as? [String: Any],
       let value = entitlements["aps-environment"] as? String {
      return value == "development" ? "sandbox" : "production"
    }
    #if targetEnvironment(simulator) || DEBUG
    return "sandbox"
    #else
    return "production"
    #endif
  }

  private func emit(_ method: String, _ payload: [String: Any]) {
    DispatchQueue.main.async {
      if self.ready {
        self.channel?.invokeMethod(method, arguments: payload)
      } else {
        self.pending.append(["method": method, "payload": payload])
        if self.pending.count > 64 { self.pending.removeFirst() }
      }
    }
  }

  private func normalized(_ info: [AnyHashable: Any]) -> [String: Any] {
    var payload = Dictionary(uniqueKeysWithValues: info.map { (String(describing: $0.key), $0.value) })
    if let aps = payload["aps"] as? [String: Any], let alert = aps["alert"] as? [String: Any] {
      payload["title"] = alert["title"]
      payload["body"] = alert["body"]
    }
    payload["provider"] = "apns"
    payload["platform"] = "ios"
    return payload
  }

  func opened(_ info: [AnyHashable: Any]) {
    let payload = normalized(info)
    initial = payload
    emit("notificationOpened", payload)
  }

  private func suppress(_ payload: [String: Any]) -> Bool {
    if let recipient = payload["recipientId"] as? String,
       recipient != context["userId"] as? String { return true }
    if let binding = payload["bindingId"] as? String,
       let current = context["bindingId"] as? String, !current.isEmpty, binding != current { return true }
    // Native visibility wins even before Flutter reports a lifecycle change.
    return UIApplication.shared.applicationState != .background
  }

  func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                              withCompletionHandler completion: @escaping (UNNotificationPresentationOptions) -> Void) {
    let payload = normalized(notification.request.content.userInfo)
    // Local mirrors must not re-enter the Dart remote-notification loop.
    if payload["localMirror"] as? Bool != true { emit("notificationReceived", payload) }
    // willPresent is the foreground delegate. Never produce an OS banner,
    // sound, or list entry while any app page (including inactive UI) is visible.
    completion([])
  }

  func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                              withCompletionHandler completion: @escaping () -> Void) {
    opened(response.notification.request.content.userInfo)
    completion()
  }

  private func showLocal(_ payload: [String: Any], _ result: @escaping FlutterResult) {
    let content = UNMutableNotificationContent()
    content.title = payload["title"] as? String ?? "BossIP"
    content.body = payload["body"] as? String ?? ""
    content.sound = .default
    var info = payload
    info["localMirror"] = true
    content.userInfo = info
    let delay = (payload["delaySeconds"] as? NSNumber)?.doubleValue ?? 0
    if delay <= 0 && suppress(payload) { return result(false) }
    let trigger = delay > 0 ? UNTimeIntervalNotificationTrigger(timeInterval: max(1, delay), repeats: false) : nil
    let request = UNNotificationRequest(identifier: payload["eventId"] as? String ?? UUID().uuidString,
                                         content: content, trigger: trigger)
    UNUserNotificationCenter.current().add(request) { error in
      DispatchQueue.main.async { result(error == nil) }
    }
  }
}
