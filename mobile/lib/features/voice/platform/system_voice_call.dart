import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/assistant_profile.dart';
import '../../../shared/i18n/i18n.dart';

enum SystemVoiceAction { open, end, mute, unmute, interrupted, resumed }

/// Native call lifetime, separate from the call page and overlay visibility.
abstract class SystemVoiceCall {
  Stream<SystemVoiceAction> get actions;
  Future<void> start();
  Future<void> connected(DateTime at);
  Future<void> setMuted(bool muted);
  Future<void> end();
  Future<bool> needsOverlayPermission();
  Future<bool> requestOverlayPermission();
}

/// Desktop/widget tests do not have an OS call surface.
class NoopSystemVoiceCall implements SystemVoiceCall {
  const NoopSystemVoiceCall();
  @override
  Stream<SystemVoiceAction> get actions => const Stream.empty();
  @override
  Future<void> start() async {}
  @override
  Future<void> connected(DateTime at) async {}
  @override
  Future<void> setMuted(bool muted) async {}
  @override
  Future<void> end() async {}
  @override
  Future<bool> needsOverlayPermission() async => false;
  @override
  Future<bool> requestOverlayPermission() async => false;
}

final systemVoiceCallProvider = Provider<SystemVoiceCall>((ref) {
  if (kIsWeb ||
      !const {
        TargetPlatform.android,
        TargetPlatform.iOS,
      }.contains(defaultTargetPlatform)) {
    return const NoopSystemVoiceCall();
  }
  final bridge = DeviceSystemVoiceCall(() {
    final i18n = ref.read(i18nProvider);
    final name = ref.read(assistantProfileProvider).valueOrNull?.name ?? '';
    return {
      'name': name.isEmpty ? i18n.t('common:assistantName.title') : name,
      'connecting': i18n.t('voice:state.connecting'),
      'ongoing': i18n.t('voice:banner.inCall'),
      'returnLabel': i18n.t('voice:banner.tapToReturn'),
      'endLabel': i18n.t('voice:controls.hangUp'),
    };
  });
  ref.onDispose(bridge.dispose);
  return bridge;
});

class DeviceSystemVoiceCall implements SystemVoiceCall {
  DeviceSystemVoiceCall(this.labels) {
    channel.setMethodCallHandler((call) async {
      final action = switch (call.method) {
        'open' => SystemVoiceAction.open,
        'end' => SystemVoiceAction.end,
        'interrupted' => SystemVoiceAction.interrupted,
        'resumed' => SystemVoiceAction.resumed,
        'mute' =>
          call.arguments == true
              ? SystemVoiceAction.mute
              : SystemVoiceAction.unmute,
        _ => null,
      };
      if (action != null) _actions.add(action);
    });
  }

  static const channel = MethodChannel('com.bossip.bipmobile/voice_call');
  final Map<String, String> Function() labels;
  final _actions = StreamController<SystemVoiceAction>.broadcast();
  @override
  Stream<SystemVoiceAction> get actions => _actions.stream;
  @override
  Future<void> start() => channel.invokeMethod<void>('start', labels());
  @override
  Future<void> connected(DateTime at) => channel.invokeMethod<void>(
    'connected',
    {'at': at.millisecondsSinceEpoch},
  );
  @override
  Future<void> setMuted(bool muted) =>
      channel.invokeMethod<void>('mute', muted);
  @override
  Future<void> end() => channel.invokeMethod<void>('end');
  @override
  Future<bool> needsOverlayPermission() async =>
      await channel.invokeMethod<bool>('needsOverlayPermission') ?? false;
  @override
  Future<bool> requestOverlayPermission() async =>
      await channel.invokeMethod<bool>('requestOverlayPermission') ?? false;

  void dispose() {
    channel.setMethodCallHandler(null);
    unawaited(_actions.close());
  }
}
