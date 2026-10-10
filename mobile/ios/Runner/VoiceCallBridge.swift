import AVFoundation
import CallKit
import Flutter
import UIKit

/// Audio-only calls use CallKit's system call indicator/return UI. iOS does
/// not grant a permission for Android-style windows over other applications.
final class VoiceCallBridge: NSObject, CXProviderDelegate {
  private var channel: FlutterMethodChannel?
  private let calls = CXCallController()
  private var provider: CXProvider?
  private var callID: UUID?
  private var startResult: FlutterResult?
  private var startTimeout: DispatchWorkItem?
  private var muted = false
  private var audioActive = false

  func attach(_ messenger: FlutterBinaryMessenger) {
    let channel = FlutterMethodChannel(name: "com.bossip.bipmobile/voice_call", binaryMessenger: messenger)
    self.channel = channel
    channel.setMethodCallHandler { [weak self] call, result in
      guard let self else { return }
      switch call.method {
      case "state":
        result(["active": self.callID != nil, "surface": "callkit", "audioActive": self.audioActive])
      case "start":
        let labels = call.arguments as? [String: String] ?? [:]
        self.start(name: labels["name"] ?? "BossIP", result: result)
      case "connected":
        if let id = self.callID {
          let milliseconds = (call.arguments as? [String: Any])?["at"] as? NSNumber
          let date = milliseconds.map { Date(timeIntervalSince1970: $0.doubleValue / 1000) } ?? Date()
          self.provider?.reportOutgoingCall(with: id, connectedAt: date)
        }
        result(nil)
      case "mute":
        let muted = call.arguments as? Bool ?? false
        if let id = self.callID, self.muted != muted {
          self.muted = muted
          self.calls.request(CXTransaction(action: CXSetMutedCallAction(call: id, muted: muted))) { _ in }
        }
        result(nil)
      case "end":
        self.end(reason: .remoteEnded)
        result(nil)
      case "needsOverlayPermission", "requestOverlayPermission":
        // CallKit supplies its own system UI; there is no overlay setting.
        result(false)
      default:
        result(FlutterMethodNotImplemented)
      }
    }
  }

  private func start(name: String, result: @escaping FlutterResult) {
    guard callID == nil else {
      result(FlutterError(code: "CALL_BUSY", message: "A call is already active", details: nil))
      return
    }
    if provider == nil {
      let configuration = CXProviderConfiguration(localizedName: "BossIP")
      configuration.supportsVideo = false
      configuration.maximumCallGroups = 1
      configuration.maximumCallsPerCallGroup = 1
      configuration.supportedHandleTypes = [.generic]
      configuration.includesCallsInRecents = false
      provider = CXProvider(configuration: configuration)
      provider?.setDelegate(self, queue: .main)
    }
    let id = UUID()
    callID = id
    muted = false
    startResult = result
    let action = CXStartCallAction(call: id, handle: CXHandle(type: .generic, value: name))
    action.isVideo = false
    let timeout = DispatchWorkItem { [weak self] in
      guard let self, self.callID == id, self.startResult != nil else { return }
      self.failStart(code: "CALL_AUDIO_TIMEOUT")
    }
    startTimeout = timeout
    DispatchQueue.main.asyncAfter(deadline: .now() + 15, execute: timeout)
    calls.request(CXTransaction(action: action)) { [weak self] error in
      DispatchQueue.main.async {
        guard let self, self.callID == id else { return }
        if error != nil { self.failStart(code: "CALL_START_FAILED") }
      }
    }
  }

  func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
    guard action.callUUID == callID else { action.fail(); return }
    do {
      // Configure before fulfilling, but let CallKit activate the session.
      try AVAudioSession.sharedInstance().setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetooth])
      let update = CXCallUpdate()
      update.localizedCallerName = action.handle.value
      update.remoteHandle = action.handle
      update.hasVideo = false
      update.supportsHolding = false
      update.supportsGrouping = false
      update.supportsUngrouping = false
      update.supportsDTMF = false
      provider.reportCall(with: action.callUUID, updated: update)
      provider.reportOutgoingCall(with: action.callUUID, startedConnectingAt: Date())
      action.fulfill()
    } catch {
      action.fail()
      failStart(code: "CALL_AUDIO_FAILED")
    }
  }

  func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
    guard callID != nil else { return }
    audioActive = true
    startTimeout?.cancel()
    startTimeout = nil
    if let result = startResult {
      startResult = nil
      result(nil) // Flutter may now start microphone capture and playback.
    } else {
      channel?.invokeMethod("resumed", arguments: nil)
    }
  }

  func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
    audioActive = false
    if callID != nil { channel?.invokeMethod("interrupted", arguments: nil) }
  }

  func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
    guard action.callUUID == callID else { action.fulfill(); return }
    clear()
    channel?.invokeMethod("end", arguments: nil)
    action.fulfill()
  }

  func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
    guard action.callUUID == callID else { action.fail(); return }
    muted = action.isMuted
    channel?.invokeMethod("mute", arguments: muted)
    action.fulfill()
  }

  func providerDidReset(_ provider: CXProvider) {
    let wasActive = callID != nil
    clear()
    if wasActive { channel?.invokeMethod("end", arguments: nil) }
  }

  func didBecomeActive() {
    if callID != nil { channel?.invokeMethod("open", arguments: nil) }
  }

  private func failStart(code: String) {
    let callback = startResult
    startResult = nil
    end(reason: .failed)
    callback?(FlutterError(code: code, message: "The system could not start call audio", details: nil))
  }

  private func end(reason: CXCallEndedReason) {
    let id = callID
    clear()
    if let id { provider?.reportCall(with: id, endedAt: Date(), reason: reason) }
  }

  private func clear() {
    callID = nil
    audioActive = false
    startTimeout?.cancel()
    startTimeout = nil
    let callback = startResult
    startResult = nil
    callback?(FlutterError(code: "CALL_CANCELLED", message: "Call cancelled", details: nil))
  }
}
