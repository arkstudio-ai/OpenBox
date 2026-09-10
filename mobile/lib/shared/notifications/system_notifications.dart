import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

class NativeNotificationEvent {
  const NativeNotificationEvent(this.opened, this.payload);
  final bool opened;
  final Map<String, dynamic> payload;
}

/// Shared MethodChannel contract, implemented by APNs and JPush adapters.
class SystemNotifications extends ChangeNotifier {
  SystemNotifications({MethodChannel? channel})
    : _channel = channel ?? const MethodChannel('bossip/system_notifications');

  final MethodChannel _channel;
  final _events = StreamController<NativeNotificationEvent>.broadcast();
  Stream<NativeNotificationEvent> get events => _events.stream;
  Future<void>? _refreshing;
  bool _disposed = false;
  String status = 'unknown';
  String? token;
  String environment = 'production';
  String appVersion = '';
  String? registrationError;
  bool get authorized => status == 'granted';
  String get platform =>
      defaultTargetPlatform == TargetPlatform.iOS ? 'ios' : 'android';
  String get provider => platform == 'ios' ? 'apns' : 'jpush';

  Future<T?> _invoke<T>(String method, [dynamic arguments]) async {
    try {
      return await _channel
          .invokeMethod<T>(method, arguments)
          .timeout(const Duration(seconds: 3));
    } on Exception {
      return null;
    }
  }

  Future<void> refresh() =>
      _refreshing ??= _refresh().whenComplete(() => _refreshing = null);

  Future<void> _refresh() async {
    _channel.setMethodCallHandler(_handle);
    final nextStatus = await _invoke<String>('getAuthorizationStatus');
    final nextToken = await _invoke<String>('getDeviceToken');
    final nextEnvironment = await _invoke<String>('getApnsEnvironment');
    final version = await _invoke<String>('getAppVersion');
    if (_disposed) return;
    status = nextStatus ?? status;
    token = nextToken?.isNotEmpty == true ? nextToken : token;
    environment = nextEnvironment ?? environment;
    appVersion = version ?? appVersion;
    final initial = await _invoke<Map<dynamic, dynamic>>(
      'getInitialNotification',
    );
    if (initial != null) _publish(true, initial);
    final events = await _invoke<List<dynamic>>('flushNotificationEvents');
    for (final event in events ?? const []) {
      if (event is Map) {
        await _handle(
          MethodCall(event['method']?.toString() ?? '', event['payload']),
        );
      }
    }
    if (!_disposed) notifyListeners();
  }

  Future<dynamic> _handle(MethodCall call) async {
    if (_disposed) return null;
    final data = call.arguments is Map
        ? Map<String, dynamic>.from(call.arguments as Map)
        : <String, dynamic>{};
    switch (call.method) {
      case 'authorizationChanged':
        status = data['status']?.toString() ?? status;
      case 'deviceTokenUpdated':
        token = data['token']?.toString();
        environment = data['apnsEnvironment']?.toString() ?? environment;
        registrationError = null;
      case 'registrationFailed':
        registrationError = 'PUSH_SETUP_FAILED';
      case 'notificationOpened':
        _publish(true, data);
      case 'notificationReceived':
        _publish(false, data);
    }
    notifyListeners();
    return null;
  }

  void _publish(bool opened, Map<dynamic, dynamic> payload) {
    if (!_disposed) {
      _events.add(
        NativeNotificationEvent(opened, Map<String, dynamic>.from(payload)),
      );
    }
  }

  Future<bool> requestAuthorization() async {
    try {
      final granted = await _channel
          .invokeMethod<bool>('requestAuthorization')
          .timeout(const Duration(minutes: 2));
      await refresh();
      return granted == true;
    } on Exception {
      registrationError = 'PUSH_SETUP_FAILED';
      if (!_disposed) notifyListeners();
      return false;
    }
  }

  Future<bool> showLocal(Map<String, dynamic> payload) async =>
      await _invoke<bool>('showLocalNotification', payload) ?? false;
  Future<void> clear() async {
    await _invoke<bool>('clearNotifications');
  }

  Future<void> openSettings() async {
    await _invoke<bool>('openSettings');
  }

  Future<void> setContext(Map<String, dynamic> context) async {
    await _invoke<bool>('setPresentationContext', context);
  }

  @override
  void dispose() {
    _disposed = true;
    _channel.setMethodCallHandler(null);
    unawaited(_events.close());
    super.dispose();
  }
}

final systemNotificationsProvider = ChangeNotifierProvider<SystemNotifications>(
  (ref) => SystemNotifications(),
);
