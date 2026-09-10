import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/api_error.dart';
import '../api/providers.dart';
import 'system_notifications.dart';

/// Serializes registration and ignores results from a previous login.
class PushController extends ChangeNotifier {
  PushController(this.dio, this.native, this.prefs) {
    native.addListener(_nativeChanged);
  }
  final Dio dio;
  final SystemNotifications native;
  final SharedPreferences prefs;
  String? userId;
  String? sessionId;
  String? bindingId;
  String? errorCode;
  bool deliveryReady = false;
  bool busy = false;
  bool foreground = true;
  String lifecycle = 'detached';
  static const _sequenceKey = 'openbox:push-presence-sequence';
  int _presenceSequence = 0;
  Future<void> _presenceWrites = Future.value();
  bool _disposed = false;
  int _epoch = 0;
  String? _fingerprint;
  Future<void>? _syncing;
  bool _again = false;
  bool _checking = false;
  Timer? _retry;
  String get _preferenceKey => 'openbox:notifications-enabled:$userId';
  bool get wanted => prefs.getBool(_preferenceKey) ?? true;

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  void _nativeChanged() {
    unawaited(sync());
    _changed();
  }

  void setIdentity(String? user, String? session) {
    if (userId == user && sessionId == session) return;
    final hadIdentity = userId != null;
    _epoch++;
    userId = user;
    sessionId = session;
    bindingId = null;
    deliveryReady = false;
    _fingerprint = null;
    errorCode = null;
    _retry?.cancel();
    if (hadIdentity) unawaited(native.clear());
    if (user != null && session != null) {
      unawaited(reportPresence());
      unawaited(checkSession());
    }
    _changed();
  }

  void setForeground(bool value) {
    foreground = value;
    if (!value) _retry?.cancel();
  }

  void setLifecycle(String state) {
    final changed = lifecycle != state;
    lifecycle = state;
    setForeground(state == 'resumed' || state == 'inactive');
    if (changed) unawaited(reportPresence());
  }

  /// Persist a logical sequence before dispatch. Requests may finish out of
  /// order; the server rejects older sequences, including after app restart.
  Future<void> reportPresence() async {
    if (_disposed || userId == null || sessionId == null) return;
    final epoch = _epoch;
    final state = lifecycle;
    final write = _presenceWrites.then<int?>((_) async {
      if (epoch != _epoch || _disposed) return null;
      final saved = prefs.getInt(_sequenceKey) ?? 0;
      if (saved > _presenceSequence) _presenceSequence = saved;
      final sequence = ++_presenceSequence;
      if (!await prefs.setInt(_sequenceKey, sequence)) return null;
      return sequence;
    });
    _presenceWrites = write.then<void>((_) {}, onError: (Object _) {});
    try {
      final sequence = await write;
      if (sequence == null || epoch != _epoch || _disposed) return;
      await dio.put<dynamic>(
        '/api/push/presence',
        data: {'state': state, 'sequence': sequence},
      );
    } catch (_) {
      // Retry the CURRENT state on the next heartbeat/resume. A network
      // failure must never rewrite local visibility to "offline".
    }
  }

  Future<void> checkSession() async {
    if (_checking || userId == null || sessionId == null || _disposed) return;
    _checking = true;
    final epoch = _epoch;
    try {
      final response = await dio.get<Map<String, dynamic>>('/api/push/status');
      if (epoch != _epoch || _disposed) return;
      bindingId = response.data?['bindingId'] as String?;
      final presence = response.data?['presence'];
      final sequence = presence is Map<String, dynamic>
          ? presence['sequence']
          : null;
      if (sequence is int && sequence > _presenceSequence) {
        _presenceSequence = sequence;
      }
      unawaited(reportPresence());
      await native.refresh();
      if (epoch == _epoch) await sync(force: true);
    } on DioException catch (error) {
      if (epoch == _epoch) {
        errorCode = ApiError.fromDio(error).code;
        deliveryReady = false;
      }
    } finally {
      _checking = false;
      _changed();
      if (epoch != _epoch && !_disposed && userId != null) {
        unawaited(checkSession());
      }
    }
  }

  Future<void> sync({bool force = false}) {
    if (_disposed || userId == null || sessionId == null) return Future.value();
    if (force) _fingerprint = null;
    if (_syncing != null) {
      _again = true;
      return _syncing!;
    }
    return _syncing = _syncLoop().whenComplete(() => _syncing = null);
  }

  Future<void> _syncLoop() async {
    do {
      _again = false;
      await _syncOnce();
    } while (_again && !_disposed && userId != null && sessionId != null);
  }

  Future<void> _syncOnce() async {
    final epoch = _epoch;
    if (native.status == 'unknown') return;
    final token = native.token;
    final enabled = wanted && native.authorized;
    final fingerprint =
        '$sessionId:${native.provider}:${native.environment}:$token:$enabled';
    if (fingerprint == _fingerprint) return;
    try {
      if (token == null || token.isEmpty) {
        deliveryReady = false;
        if (!enabled && bindingId != null) {
          await dio.delete<dynamic>(
            '/api/push/devices/${Uri.encodeComponent(bindingId!)}',
          );
        }
        return;
      }
      final response = await dio.post<Map<String, dynamic>>(
        '/api/push/devices',
        data: {
          'platform': native.platform,
          'provider': native.provider,
          'token': token,
          'bundleId': 'com.bossip.bipmobile',
          'appVersion': native.appVersion,
          'apnsEnvironment': native.environment,
          'locale': PlatformDispatcher.instance.locale.toLanguageTag(),
          'notificationsEnabled': enabled,
        },
      );
      if (epoch != _epoch || _disposed) return;
      bindingId = response.data?['bindingId'] as String?;
      deliveryReady = enabled && response.data?['deliveryEnabled'] == true;
      _fingerprint = fingerprint;
      errorCode = enabled && !deliveryReady ? 'PUSH_NOT_CONFIGURED' : null;
      _retry?.cancel();
    } on DioException catch (error) {
      if (epoch != _epoch || _disposed) return;
      errorCode = ApiError.fromDio(error).code;
      deliveryReady = false;
      _retry?.cancel();
      if (foreground) {
        _retry = Timer(
          const Duration(seconds: 15),
          () => unawaited(sync(force: true)),
        );
      }
    } finally {
      _changed();
    }
  }

  Future<void> setEnabled(bool enabled) async {
    final epoch = _epoch;
    if (userId == null) return;
    busy = true;
    _changed();
    try {
      await prefs.setBool(_preferenceKey, enabled);
      if (enabled && !native.authorized) await native.requestAuthorization();
      if (epoch == _epoch) await sync(force: true);
    } finally {
      busy = false;
      _changed();
    }
  }

  Future<bool> testLocal(String title, String body) => native.showLocal({
    'source': 'openbox',
    'schemaVersion': 1,
    'type': 'system_test',
    'eventId': 'local-${DateTime.now().microsecondsSinceEpoch}',
    'recipientId': userId,
    'bindingId': bindingId,
    'title': title,
    'body': body,
    'delaySeconds': 10,
  });

  Future<String> testRemote() async {
    final epoch = _epoch;
    await sync(force: true);
    if (epoch != _epoch || !deliveryReady) {
      throw StateError('PUSH_DEVICE_NOT_READY');
    }
    final response = await dio.post<Map<String, dynamic>>('/api/push/test');
    final id = response.data?['id'] as String?;
    if (id == null) throw StateError('PUSH_SETUP_FAILED');
    if (epoch != _epoch || _disposed) return 'cancelled';
    return 'pending';
  }

  @override
  void dispose() {
    _disposed = true;
    _epoch++;
    _retry?.cancel();
    native.removeListener(_nativeChanged);
    super.dispose();
  }
}

final pushControllerProvider = ChangeNotifierProvider<PushController>(
  (ref) => PushController(
    ref.read(apiDioProvider),
    ref.read(systemNotificationsProvider),
    ref.read(prefsProvider),
  ),
);
