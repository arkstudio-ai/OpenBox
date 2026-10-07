import 'dart:async';

import 'package:bossip_mobile/features/voice/audio/call_audio.dart';
import 'package:bossip_mobile/features/voice/audio/mic_permission.dart';
import 'package:bossip_mobile/features/voice/audio/tones.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'voice_fakes.dart';

/// One controller in its own container, driven by fake time.
class _Call {
  _Call(this.rig) : container = ProviderContainer(overrides: rig.overrides);

  final VoiceRig rig;
  final ProviderContainer container;

  VoiceCallController get controller =>
      container.read(voiceCallControllerProvider.notifier);

  VoiceCallState get state => container.read(voiceCallControllerProvider);

  FakeVoiceChannel get socket => rig.connector.last;

  Future<void> dial(WidgetTester tester) async {
    unawaited(controller.start());
    await tester.pump();
  }

  Future<void> connect(WidgetTester tester) async {
    await dial(tester);
    socket.sendReady();
    await tester.pump();
  }

  Future<void> wait(WidgetTester tester, Duration by) async {
    rig.clock.advance(by);
    await tester.pump(by);
  }

  /// Every timer cancelled, every socket let go.
  void dispose() => container.dispose();
}

void main() {
  testWidgets('dials in order and sends no audio before ready', (tester) async {
    final call = _Call(VoiceRig());
    await call.dial(tester);
    expect(call.state.status, VoiceCallStatus.connecting);
    expect(call.rig.audio.opened, isTrue);
    expect(call.rig.audio.tones, [CallTone.connecting]);
    // Speaker unless a headset is on.
    expect(call.rig.audio.speaker, [true]);
    expect(call.state.speakerOn, isTrue);
    final uri = call.socket.uri;
    expect(uri.path, '/ws/assistant/voice');
    expect(uri.queryParameters['ticket'], 'ticket-1');
    expect(call.rig.connector.scopes, [testScope]);

    call.rig.audio.packet();
    expect(call.socket.audioSent, isEmpty);

    call.socket.sendReady();
    await tester.pump();
    expect(call.state.status, VoiceCallStatus.connected);
    expect(call.state.phase, VoicePhase.greeting);
    expect(call.rig.audio.tones, [CallTone.connecting, CallTone.connected]);
    expect(call.rig.haptics, [false]);

    final packet = call.rig.audio.packet();
    expect(call.socket.audioSent.single, packet);
    expect(call.controller.micLevel.value, greaterThan(0));
    call.dispose();
  });

  testWidgets('server audio plays, playback.clear clears, phases follow', (
    tester,
  ) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    call.socket
      ..audio([1, 0, 2, 0])
      ..event({'type': 'playback.clear'})
      ..event({'type': 'phase', 'value': 'listening', 'working': true});
    await tester.pump();
    expect(call.rig.audio.played.single, [1, 0, 2, 0]);
    expect(call.rig.audio.clears, 1);
    expect(call.state.phase, VoicePhase.listening);
    expect(call.state.working, isTrue);
    call.dispose();
  });

  testWidgets('muted, the microphone sends silence', (tester) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    call.controller.toggleMute();
    call.rig.audio.packet();
    final sent = call.socket.audioSent.single;
    expect(sent, hasLength(3200));
    expect(sent.every((byte) => byte == 0), isTrue);
    expect(call.controller.micLevel.value, 0);
    call.controller.toggleMute();
    expect(call.state.muted, isFalse);
    call.dispose();
  });

  testWidgets('hang-up stops at once; ended brings the numbers', (
    tester,
  ) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    await call.controller.hangUp();
    expect(call.state.status, VoiceCallStatus.ending);
    expect(call.socket.jsonSent, [
      {'type': 'stop'},
    ]);
    expect(call.rig.audio.closed, isTrue);
    expect(call.rig.audio.closeTone, CallTone.ended);
    expect(call.rig.haptics, [false, false]);

    // Audio still in flight is not played after the hang-up.
    call.socket
      ..audio([9, 9])
      ..event({'type': 'cost', 'total_yuan': 0.0123, 'final': true})
      ..event({
        'type': 'ended',
        'reason': 'hangup',
        'duration_seconds': 75,
        'pending_turns': 1,
        'cost': {'total_yuan': 0.0123, 'final': true},
      })
      ..serverClose(1000);
    await tester.pump();
    expect(call.rig.audio.played, isEmpty);
    final end = call.state.end!;
    expect(call.state.status, VoiceCallStatus.ended);
    expect(
      (end.reason, end.durationSeconds, end.pendingTurns),
      (VoiceEndReason.hangup, 75, 1),
    );
    expect(end.cost!.totalYuan, 0.0123);
    // No second tone or buzz for the server's confirmation.
    expect(call.rig.haptics, [false, false]);
    call.dispose();
  });

  testWidgets('hang-up without an answer ends after four seconds', (
    tester,
  ) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    await call.wait(tester, const Duration(seconds: 20));
    await call.controller.hangUp();
    await call.wait(tester, const Duration(seconds: 3));
    expect(call.state.status, VoiceCallStatus.ending);
    await call.wait(tester, const Duration(seconds: 1));
    expect(call.state.status, VoiceCallStatus.ended);
    expect(call.state.end!.reason, VoiceEndReason.hangup);
    // Counted to the button press, not the wait after it.
    expect(call.state.end!.durationSeconds, 20);
    await call.wait(tester, VoiceCallController.closeGrace);
    expect(call.socket.sinkClosed, isTrue);
    call.dispose();
  });

  testWidgets('cancelling while dialling leaves no call behind', (
    tester,
  ) async {
    final call = _Call(VoiceRig());
    await call.dial(tester);
    await call.controller.hangUp();
    expect(call.state.status, VoiceCallStatus.idle);
    expect(call.rig.audio.closed, isTrue);
    expect(call.rig.audio.closeTone, isNull);
    expect(call.socket.sinkClosed, isTrue);
    call.dispose();
  });

  for (final (code, reason, detail) in [
    (4009, VoiceEndReason.concurrent, null),
    (4029, VoiceEndReason.quota, null),
    (4503, VoiceEndReason.error, 'voice:errors.disabled'),
    (4001, VoiceEndReason.error, 'voice:errors.connectFailed'),
  ]) {
    testWidgets('close $code ends the call as ${reason.wire}', (tester) async {
      final call = _Call(VoiceRig());
      await call.dial(tester);
      call.socket.serverClose(code);
      await tester.pump();
      expect(call.state.status, VoiceCallStatus.ended);
      expect(call.state.end!.reason, reason);
      expect(call.state.end!.detailKey, detail);
      expect(call.rig.audio.closeTone, CallTone.error);
      expect(call.rig.haptics, [true]);
      call.dispose();
    });
  }

  testWidgets('4404 creates the main conversation and dials once more', (
    tester,
  ) async {
    final call = _Call(VoiceRig());
    await call.dial(tester);
    call.socket.serverClose(4404);
    await tester.pump();
    expect(call.rig.ensured, 1);
    expect(call.rig.connector.channels, hasLength(2));
    expect(call.state.status, VoiceCallStatus.connecting);
    call.socket.sendReady();
    await tester.pump();
    expect(call.state.status, VoiceCallStatus.connected);

    // Only once: a second 4404 is the end.
    final again = _Call(VoiceRig());
    await again.dial(tester);
    again.socket.serverClose(4404);
    await tester.pump();
    again.socket.serverClose(4404);
    await tester.pump();
    expect(again.rig.ensured, 1);
    expect(again.state.end!.detailKey, 'voice:errors.assistantUnavailable');
    call.dispose();
    again.dispose();
  });

  testWidgets('a ticket or server failure while dialling ends the call', (
    tester,
  ) async {
    final rig = VoiceRig();
    rig.connector.failWith = StateError('offline');
    final call = _Call(rig);
    await call.dial(tester);
    expect(call.state.end!.reason, VoiceEndReason.error);
    expect(call.state.end!.detailKey, 'voice:errors.connectFailed');
    call.dispose();
  });

  testWidgets('no ready within 25 s gives up', (tester) async {
    final call = _Call(VoiceRig());
    await call.dial(tester);
    await call.wait(tester, const Duration(seconds: 24));
    expect(call.state.status, VoiceCallStatus.connecting);
    await call.wait(tester, const Duration(seconds: 1));
    expect(call.state.end!.reason, VoiceEndReason.error);
    expect(call.state.end!.detailKey, 'voice:errors.connectFailed');
    call.dispose();
  });

  testWidgets('30 s without a frame is a dropped network', (tester) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    // Heartbeats keep a quiet call alive.
    for (var i = 0; i < 3; i++) {
      await call.wait(tester, const Duration(seconds: 25));
      call.socket.event({'type': 'heartbeat', 'elapsed_seconds': 25 * i});
      await tester.pump();
    }
    expect(call.state.status, VoiceCallStatus.connected);
    await call.wait(tester, const Duration(seconds: 30));
    expect(call.state.end!.reason, VoiceEndReason.network);
    call.dispose();
  });

  testWidgets('an interruption pauses with zero frames, then resumes', (
    tester,
  ) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    call.rig.audio.emit(const CallAudioInterruption(begin: true));
    await tester.pump();
    expect(call.state.status, VoiceCallStatus.paused);
    expect(call.state.pauseCause, VoicePauseCause.interruption);
    expect(call.rig.audio.paused, isTrue);

    await call.wait(tester, const Duration(milliseconds: 350));
    final zeros = call.socket.audioSent;
    expect(zeros, hasLength(3));
    expect(
      zeros.every((p) => p.length == 3200 && p.every((b) => b == 0)),
      isTrue,
    );
    // Server audio is not played into a phone call.
    call.socket.audio([5, 5]);
    await tester.pump();
    expect(call.rig.audio.played, isEmpty);

    call.rig.audio.emit(const CallAudioInterruption(begin: false));
    await tester.pump();
    expect(call.state.status, VoiceCallStatus.connected);
    expect(call.rig.audio.paused, isFalse);
    await call.wait(tester, const Duration(milliseconds: 500));
    expect(call.socket.audioSent, hasLength(3));
    call.dispose();
  });

  testWidgets('a pause longer than a minute ends the call', (tester) async {
    final rig = VoiceRig();
    final call = _Call(rig);
    await call.connect(tester);
    rig.audio
      ..resumes = false
      ..emit(const CallAudioInterruption(begin: true));
    await tester.pump();
    // The system keeps the audio: resuming fails, the call stays paused.
    rig.audio.emit(const CallAudioInterruption(begin: false));
    await tester.pump();
    expect(call.state.status, VoiceCallStatus.paused);
    // The server's heartbeats keep the socket's own watchdog quiet.
    for (var i = 0; i < 2; i++) {
      await call.wait(tester, const Duration(seconds: 25));
      call.socket.event({'type': 'heartbeat', 'elapsed_seconds': i});
      await tester.pump();
    }
    await call.wait(tester, const Duration(seconds: 10));
    expect(call.state.end!.reason, VoiceEndReason.error);
    call.dispose();
  });

  testWidgets('Android: a minute in the background pauses the call', (
    tester,
  ) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    final call = _Call(VoiceRig());
    await call.connect(tester);
    tester.binding
      ..handleAppLifecycleStateChanged(AppLifecycleState.inactive)
      ..handleAppLifecycleStateChanged(AppLifecycleState.hidden)
      ..handleAppLifecycleStateChanged(AppLifecycleState.paused);
    for (final step in const [25, 25, 9]) {
      await call.wait(tester, Duration(seconds: step));
      call.socket.event({'type': 'heartbeat', 'elapsed_seconds': step});
      await tester.pump();
    }
    expect(call.state.status, VoiceCallStatus.connected);
    await call.wait(tester, const Duration(seconds: 1));
    expect(call.state.status, VoiceCallStatus.paused);
    expect(call.state.pauseCause, VoicePauseCause.background);
    tester.binding
      ..handleAppLifecycleStateChanged(AppLifecycleState.hidden)
      ..handleAppLifecycleStateChanged(AppLifecycleState.inactive)
      ..handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pump();
    expect(call.state.status, VoiceCallStatus.connected);
    call.dispose();
    debugDefaultTargetPlatformOverride = null;
  });

  testWidgets('time limit: the closing phrase plays, then ended(limit)', (
    tester,
  ) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    call.socket
      ..event({'type': 'limit', 'reason': 'max_duration'})
      ..audio([3, 0]);
    await tester.pump();
    expect(call.state.status, VoiceCallStatus.ending);
    expect(call.rig.audio.played, hasLength(1));
    call.rig.audio.packet();
    expect(call.socket.audioSent, isEmpty);
    call.socket.event({'type': 'ended', 'reason': 'limit'});
    await tester.pump();
    expect(call.state.end!.reason, VoiceEndReason.limit);
    expect(call.rig.audio.closeTone, CallTone.ended);
    call.dispose();
  });

  testWidgets('a server error ends with its own words', (tester) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    call.socket.event({
      'type': 'error',
      'code': 'provider_unavailable',
      'message': '语音服务暂时不可用。',
    });
    await tester.pump();
    expect(call.state.end!.reason, VoiceEndReason.error);
    expect(call.state.end!.message, '语音服务暂时不可用。');
    expect(call.rig.audio.closeTone, CallTone.error);
    // The trailing `ended` still fills in the duration.
    call.socket.event({
      'type': 'ended',
      'reason': 'error',
      'duration_seconds': 12,
    });
    await tester.pump();
    expect(call.state.end!.durationSeconds, 12);
    call.dispose();
  });

  testWidgets('microphone: refused ends before any socket; asked once', (
    tester,
  ) async {
    final denied = _Call(VoiceRig(access: MicAccess.denied));
    // The system answers a standing refusal at once, without a dialog.
    denied.rig.permission.grant = false;
    await denied.dial(tester);
    expect(denied.rig.permission.requests, 1);
    expect(denied.state.end!.reason, VoiceEndReason.micDenied);
    expect(denied.rig.audios, isEmpty);
    expect(denied.rig.connector.channels, isEmpty);

    final declined = _Call(VoiceRig(access: MicAccess.undetermined));
    declined.rig.permission.grant = false;
    await declined.dial(tester);
    expect(declined.rig.permission.requests, 1);
    expect(declined.state.end!.reason, VoiceEndReason.micDenied);

    final granted = _Call(VoiceRig(access: MicAccess.undetermined));
    await granted.dial(tester);
    expect(granted.rig.permission.requests, 1);
    expect(granted.state.status, VoiceCallStatus.connecting);
    denied.dispose();
    declined.dispose();
    granted.dispose();
  });

  testWidgets('a busy microphone fails before the network', (tester) async {
    final rig = VoiceRig();
    rig.nextAudio.openError = const CallAudioFailure(VoiceEndReason.micBusy);
    final call = _Call(rig);
    await call.dial(tester);
    expect(call.state.end!.reason, VoiceEndReason.micBusy);
    expect(rig.connector.channels, isEmpty);
    call.dispose();
  });

  testWidgets('headsets move the route until the speaker is chosen', (
    tester,
  ) async {
    final rig = VoiceRig();
    rig.nextAudio.external = true;
    final call = _Call(rig);
    await call.connect(tester);
    expect(call.state.speakerOn, isFalse);
    rig.audio.emit(const CallAudioRouteChange(external: false));
    await tester.pump();
    expect(call.state.speakerOn, isTrue);
    await call.controller.toggleSpeaker();
    expect(call.state.speakerOn, isFalse);
    rig.audio.emit(const CallAudioRouteChange(external: false));
    await tester.pump();
    expect(call.state.speakerOn, isFalse);
    expect(rig.audio.speaker, [false, true, false]);
    call.dispose();
  });

  testWidgets('a lost microphone ends the call', (tester) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    call.rig.audio.emit(const CallAudioMicLost());
    await tester.pump();
    expect(call.state.end!.reason, VoiceEndReason.micLost);
    call.dispose();
  });

  testWidgets('signing out or switching workspace hangs up', (tester) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    call.container.read(testScopeProvider.notifier).state = (
      userId: 'user-1',
      workspaceId: 'ws-2',
    );
    // Riverpod refreshes the scope on a zero-length timer.
    await tester.pump(Duration.zero);
    expect(call.state.status, VoiceCallStatus.ending);
    expect(call.socket.jsonSent, [
      {'type': 'stop'},
    ]);
    call.dispose();
  });

  testWidgets('closing the page on an ended call clears it; redial dials', (
    tester,
  ) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    call.controller.setExpanded(true);
    call.socket.serverClose(1011);
    await tester.pump();
    expect(call.state.status, VoiceCallStatus.ended);
    expect(call.state.expanded, isTrue);

    unawaited(call.controller.redial());
    await tester.pump();
    expect(call.state.status, VoiceCallStatus.connecting);
    expect(call.rig.connector.channels, hasLength(2));
    expect(call.state.expanded, isTrue);
    call.socket.serverClose(4009);
    await tester.pump();
    call.controller.setExpanded(false);
    expect(call.state.status, VoiceCallStatus.idle);
    call.dispose();
  });

  testWidgets('odd frames from the server still reach the player', (
    tester,
  ) async {
    final call = _Call(VoiceRig());
    await call.connect(tester);
    call.socket.audio(Uint8List(3));
    await tester.pump();
    expect(call.rig.audio.played.single, hasLength(3));
    call.dispose();
  });
}
