import Flutter
import UIKit
import UserNotifications
#if canImport(AlipaySDK)
import AlipaySDK
#endif

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate, UIDocumentPickerDelegate {
  let notifications = SystemNotificationBridge()
  private var alipayResult: FlutterResult?
  private var downloadResult: FlutterResult?

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    let result = super.application(application, didFinishLaunchingWithOptions: launchOptions)
    notifications.start(launchOptions)
    return result
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
    notifications.attach(engineBridge.applicationRegistrar.messenger())
    let channel = FlutterMethodChannel(
      name: "com.bossip.bipmobile/alipay",
      binaryMessenger: engineBridge.applicationRegistrar.messenger()
    )
    channel.setMethodCallHandler { [weak self] call, result in
      guard call.method == "pay" else {
        result(FlutterMethodNotImplemented)
        return
      }
      guard
        let arguments = call.arguments as? [String: Any],
        let orderString = arguments["orderString"] as? String,
        !orderString.isEmpty
      else {
        result(FlutterError(code: "INVALID_ORDER", message: "Missing Alipay order string", details: nil))
        return
      }
      self?.startAlipay(orderString: orderString, result: result)
    }

    let downloadChannel = FlutterMethodChannel(
      name: "com.bossip.bipmobile/download",
      binaryMessenger: engineBridge.applicationRegistrar.messenger()
    )
    downloadChannel.setMethodCallHandler { [weak self] call, result in
      guard call.method == "saveFile" else {
        result(FlutterMethodNotImplemented)
        return
      }
      guard let arguments = call.arguments as? [String: Any] else {
        result(FlutterError(code: "INVALID_FILE", message: "Missing file arguments", details: nil))
        return
      }
      self?.startFileExport(arguments: arguments, result: result)
    }
  }

  private func startFileExport(arguments: [String: Any], result: @escaping FlutterResult) {
    guard downloadResult == nil else {
      result(FlutterError(code: "SAVE_BUSY", message: "Another file save is active", details: nil))
      return
    }
    guard
      let path = arguments["path"] as? String,
      !path.isEmpty,
      FileManager.default.fileExists(atPath: path)
    else {
      result(FlutterError(code: "INVALID_FILE", message: "The downloaded file is unavailable", details: nil))
      return
    }
    guard let presenter = activePresenter() else {
      result(FlutterError(code: "SAVE_UNAVAILABLE", message: "No active window can present the save dialog", details: nil))
      return
    }

    downloadResult = result
    let picker = UIDocumentPickerViewController(
      forExporting: [URL(fileURLWithPath: path)],
      asCopy: true
    )
    picker.delegate = self
    picker.modalPresentationStyle = .formSheet
    presenter.present(picker, animated: true)
  }

  private func activePresenter() -> UIViewController? {
    let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
    let window = scenes
      .flatMap(\.windows)
      .first(where: \.isKeyWindow) ?? scenes.flatMap(\.windows).first
    var controller = window?.rootViewController
    while true {
      if let presented = controller?.presentedViewController {
        controller = presented
      } else if let navigation = controller as? UINavigationController {
        controller = navigation.visibleViewController
      } else if let tabs = controller as? UITabBarController {
        controller = tabs.selectedViewController
      } else {
        return controller
      }
    }
  }

  func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
    finishFileExport(saved: !urls.isEmpty)
  }

  func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
    finishFileExport(saved: false)
  }

  private func finishFileExport(saved: Bool) {
    guard let callback = downloadResult else { return }
    downloadResult = nil
    callback(saved)
  }

  private func startAlipay(orderString: String, result: @escaping FlutterResult) {
    #if canImport(AlipaySDK)
    guard alipayResult == nil else {
      result(FlutterError(code: "PAYMENT_BUSY", message: "Another payment is active", details: nil))
      return
    }
    alipayResult = result
    AlipaySDK.defaultService().payOrder(
      orderString,
      fromScheme: "com.bossip.bipmobile.alipay"
    ) { [weak self] response in
      self?.finishAlipay(response)
    }
    #else
    result(FlutterError(
      code: "SDK_UNAVAILABLE",
      message: "Alipay SDK is unavailable in this simulator build",
      details: nil
    ))
    #endif
  }

  #if canImport(AlipaySDK)
  private func finishAlipay(_ response: [AnyHashable: Any]?) {
    guard let callback = alipayResult else { return }
    alipayResult = nil
    let normalized = Dictionary(uniqueKeysWithValues: (response ?? [:]).map {
      (String(describing: $0.key), $0.value)
    })
    callback(normalized)
  }
  #endif

  func handleAlipay(url: URL) -> Bool {
    guard url.scheme == "com.bossip.bipmobile.alipay" else { return false }
    #if canImport(AlipaySDK)
    AlipaySDK.defaultService().processOrder(withPaymentResult: url) { [weak self] response in
      self?.finishAlipay(response)
    }
    return true
    #else
    return false
    #endif
  }

  override func application(
    _ app: UIApplication,
    open url: URL,
    options: [UIApplication.OpenURLOptionsKey: Any] = [:]
  ) -> Bool {
    if handleAlipay(url: url) { return true }
    return super.application(app, open: url, options: options)
  }
  override func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
    super.application(application, didRegisterForRemoteNotificationsWithDeviceToken: deviceToken)
    notifications.registered(deviceToken)
  }

  override func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
    super.application(application, didFailToRegisterForRemoteNotificationsWithError: error)
    notifications.registrationFailed()
  }

}
