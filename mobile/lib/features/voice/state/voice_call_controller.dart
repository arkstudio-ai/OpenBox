import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:wakelock_plus/wakelock_plus.dart';

import '../../../shared/api/providers.dart';
import '../../chat/api/assistant_api.dart';
import '../api/voice_events.dart';
import '../api/voice_socket.dart';
import '../audio/call_audio.dart';
import '../audio/mic_permission.dart';
import '../audio/pcm_codec.dart';
import '../audio/tones.dart';
import 'voice_call_reducer.dart';
import 'voice_call_state.dart';

/// What the call touches outside Dart, so tests can hand in fakes.
class VoiceCallDeps {
  const VoiceCallDeps({
    required this.connector,
    required this.audio,
    required this.permission,
    required this.ensureAssistant,
    this.now = DateTime.now,
    this.haptic = _haptic,
    this.keepScreenOn = _keepScreenOn,
  });

  final VoiceConnector connector;
  final CallAudio Function() audio;
  final MicPermission permission;

  /// `POST /api/assistant/ensure`, for a socket closed with 4404.
  final Future<void> Function(AssistantScope scope) ensureAssistant;
  final DateTime Function() now;

  /// `lightImpact` when connected or ended, `mediumImpact` on an error.
  final void Function({required bool strong}) haptic;
  final Future<void> Function(bool on) keepScreenOn;
}

void _haptic({required bool strong}) => unawaited(
  strong ? HapticFeedback.mediumImpact() : HapticFeedback.lightImpact(),
);

Future<void> _keepScreenOn(bool on) async {
  try {
    await WakelockPlus.toggle(enable: on);
  } catch (_) {
    // Not every platform can hold the screen on; the call does not care.
  }
}

final voiceCallDepsProvider = Provider<VoiceCallDeps>((ref) {
  final dio = ref.watch(apiDioProvider);
  return VoiceCallDeps(
    connector: VoiceConnector(dio),
    audio: DeviceCallAudio.new,
    permission: const DeviceMicPermission(),
    ensureAssistant: (scope) => AssistantApi(dio, scope).ensure(),
  );
});

/// The one place a call lives (docs/VOICE_CALL_MOBILE.md §6). The page and
/// the call bar are two views of it; closing the page never ends the call.
/// Not auto-disposed, so a call outlives every screen.
final voiceCallControllerProvider =
    NotifierProvider<VoiceCallController, VoiceCallState>(
      VoiceCallController.new,
    );

/// End reasons that are not failures: the falling two-note tone and a light
/// tap. Everything else gets the error tone and a firmer one.
const _calmEnds = {VoiceEndReason.hangup, VoiceEndReason.limit};

class VoiceCallController extends Notifier<VoiceCallState> {
  // Timings from docs/VOICE_CALL_SPEC.md §5.6 and the mobile document §4.
  static const connectTimeout = Duration(seconds: 25);
  static const silenceLimit = Duration(seconds: 30);
  static const pauseLimit = Duration(seconds: 60);
  static const backgroundGrace = Duration(seconds: 60);
  static const hangUpGrace = Duration(seconds: 4);

  /// The closing phrase after `limit`, then `ended`, should take seconds.
  static const limitGrace = Duration(seconds: 15);

  /// After an end, how long a last `ended`/`cost` may still arrive.
  static const closeGrace = Duration(seconds: 2);

  static final _silence = Uint8List(uplinkPacketBytes);

  /// Microphone loudness for the orb; zero while muted or paused.
  final micLevel = ValueNotifier<double>(0);

  /// Loudness of what the assistant is saying.
  final outputLevel = ValueNotifier<double>(0);

  VoiceCallDeps? _depsForCall;
  int _generation = 0;
  AssistantScope? _scope;
  CallAudio? _audio;
  VoiceSocket? _socket;
  StreamSubscription<CallAudioEvent>? _audioEvents;
  AppLifecycleListener? _lifecycle;
  Timer? _connectTimer;
  Timer? _silenceTimer;
  Timer? _pauseTimer;
  Timer? _zeroTimer;
  Timer? _closingTimer;
  Timer? _graceTimer;
  VoiceSocket? _graceSocket;
  Timer? _backgroundTimer;
  DateTime? _hungUpAt;
  bool _speakerChosen = false;
  bool _ensured = false;

  /// Read when needed, not at build: the call bar builds this controller on
  /// every screen, and most of them never dial.
  VoiceCallDeps get _deps =>
      _depsForCall ??= ref.read<VoiceCallDeps>(voiceCallDepsProvider);

  @override
  VoiceCallState build() {
    ref.onDispose(() {
      _generation++;
      _release();
      micLevel.dispose();
      outputLevel.dispose();
    });
    // Signing out or switching workspace ends the call: it belongs to the
    // user and workspace it was dialled in.
    ref.listen<AssistantScope?>(assistantScopeProvider, (_, next) {
      if (state.active && next != _scope) unawaited(hangUp());
    });
    return const VoiceCallState();
  }

  bool _current(int generation) => generation == _generation;

  /// Dials. The call page calls this when it opens on no call.
  Future<void> start() async {
    if (state.active) return;
    final generation = ++_generation;
    _depsForCall = ref.read(voiceCallDepsProvider);
    final scope = _scope = ref.read(assistantScopeProvider);
    _speakerChosen = false;
    _ensured = false;
    _hungUpAt = null;
    state = VoiceCallState(
      status: VoiceCallStatus.requestingMic,
      expanded: state.expanded,
    );
    if (scope == null) {
      _finish(
        VoiceEndReason.error,
        detailKey: 'voice:errors.assistantUnavailable',
      );
      return;
    }

    // 1. Microphone permission. The pre-permission page has explained it;
    //    this shows the system dialog when the answer is still open.
    var access = await _deps.permission.status().catchError(
      (Object _) => MicAccess.denied,
    );
    if (!_current(generation)) return;
    // A refusal answers at once without a dialog, so asking again costs
    // nothing — and a permission reset since then gets its dialog back.
    if (access != MicAccess.granted) {
      final granted = await _deps.permission.request().catchError(
        (Object _) => false,
      );
      if (!_current(generation)) return;
      access = granted ? MicAccess.granted : MicAccess.denied;
    }
    if (access != MicAccess.granted) {
      _finish(VoiceEndReason.micDenied);
      return;
    }

    // 2. Audio: session, player, microphone — before any network, so a
    //    busy microphone fails fast and the beeps come after the dialog.
    final audio = _audio = _deps.audio();
    _audioEvents = audio.events.listen(_onAudioEvent);
    audio.outputLevel.addListener(_copyOutputLevel);
    try {
      await audio.open(onPacket: _onPacket);
    } catch (error) {
      if (_current(generation)) {
        _finish(
          error is CallAudioFailure ? error.reason : VoiceEndReason.micBusy,
        );
      }
      return;
    }
    if (!_current(generation)) return;
    final external = await audio.externalOutput().catchError(
      (Object _) => false,
    );
    if (!_current(generation)) return;
    // Speaker unless a headset is on (mobile §4).
    await _route(speaker: !external);
    if (!_current(generation)) return;

    // 3. The socket.
    audio.playTone(CallTone.connecting);
    state = state.copyWith(status: VoiceCallStatus.connecting);
    _connectTimer = Timer(
      connectTimeout,
      () => _finish(
        VoiceEndReason.error,
        detailKey: 'voice:errors.connectFailed',
      ),
    );
    await _connect(generation, scope);
  }

  Future<void> _connect(int generation, AssistantScope scope) async {
    final VoiceSocket socket;
    try {
      socket = await _deps.connector.open(scope);
    } catch (_) {
      if (_current(generation)) {
        _finish(VoiceEndReason.error, detailKey: 'voice:errors.connectFailed');
      }
      return;
    }
    if (!_current(generation) || state.status != VoiceCallStatus.connecting) {
      socket.close();
      return;
    }
    _socket = socket;
    _watchSilence();
    socket.listen(
      onEvent: (event) => _onServerEvent(generation, event),
      onAudio: (audio) => _onServerAudio(generation, audio),
      onClosed: (code) => unawaited(_onClosed(generation, socket, code)),
    );
  }

  /// The red button, or cancel while dialling. The microphone and playback
  /// stop at once; the server's `ended` brings the duration and the cost.
  Future<void> hangUp() async {
    switch (state.status) {
      case VoiceCallStatus.idle:
      case VoiceCallStatus.ending:
      case VoiceCallStatus.ended:
        return;
      case VoiceCallStatus.requestingMic:
      case VoiceCallStatus.connecting:
        // Nothing was said or billed: drop the attempt without a summary.
        _generation++;
        _release();
        state = VoiceCallState(expanded: state.expanded);
      case VoiceCallStatus.connected:
      case VoiceCallStatus.paused:
        _hungUpAt = _deps.now();
        _cancelLiveTimers();
        micLevel.value = 0;
        state = state.copyWith(
          status: VoiceCallStatus.ending,
          endingReason: VoiceEndReason.hangup,
          pauseCause: null,
        );
        _socket?.sendStop();
        _deps.haptic(strong: false);
        _closeAudio(CallTone.ended);
        _closingTimer = Timer(
          hangUpGrace,
          () => _finish(VoiceEndReason.hangup),
        );
    }
  }

  void toggleMute() {
    if (!state.live) return;
    state = state.copyWith(muted: !state.muted);
    if (state.muted) micLevel.value = 0;
  }

  /// A manual choice sticks: headsets coming and going no longer flip it.
  Future<void> toggleSpeaker() async {
    if (!state.live) return;
    _speakerChosen = true;
    await _route(speaker: !state.speakerOn);
  }

  /// The page opened ([expanded]) or closed. Closing it on a finished call
  /// clears the call: the person has read the summary.
  void setExpanded(bool expanded) {
    if (!expanded && state.status == VoiceCallStatus.ended) {
      state = const VoiceCallState();
    } else if (state.expanded != expanded) {
      state = state.copyWith(expanded: expanded);
    }
  }

  /// The page has gone. If it handed the call to the bar on its way out
  /// ([setExpanded] from a button), there is nothing left to do — and a
  /// call that ended meanwhile keeps its summary in the bar.
  void pageGone() {
    if (state.expanded) setExpanded(false);
  }

  /// The call bar's summary has been shown; back to no call.
  void dismissEnded() {
    if (state.status == VoiceCallStatus.ended) {
      state = VoiceCallState(expanded: state.expanded);
    }
  }

  /// "重新拨打" on the end panel.
  Future<void> redial() async {
    dismissEnded();
    await start();
  }

  Future<void> openMicSettings() => _deps.permission.openSettings();

  /// Whether the entry button should explain the microphone first.
  Future<MicAccess> micAccess() => _deps.permission.status().catchError(
    (Object _) => MicAccess.undetermined,
  );

  /// The call's clock, for the timer on screen (tests bring their own).
  DateTime now() => _deps.now();

  /// The screen stays on while the call page is up (mobile §4).
  Future<void> keepScreenOn(bool on) => _deps.keepScreenOn(on);

  // ---------------------------------------------------------------- socket

  void _onServerEvent(int generation, VoiceServerEvent event) {
    if (!_current(generation)) return;
    if (state.active) _watchSilence();
    final before = state;
    state = reduceVoiceEvent(before, event, now: _deps.now());
    switch (event) {
      case VoiceReadyEvent():
        if (before.status == VoiceCallStatus.connecting && state.live) {
          _connectTimer?.cancel();
          _audio?.playTone(CallTone.connected);
          _deps.haptic(strong: false);
          _watchLifecycle();
        }
      case VoicePlaybackClearEvent():
        _audio?.clearPlayback();
      case VoiceLimitEvent():
        // The closing phrase still plays; nothing more is sent up.
        _cancelLiveTimers();
        micLevel.value = 0;
        _closingTimer?.cancel();
        _closingTimer = Timer(limitGrace, () => _finish(VoiceEndReason.limit));
      default:
        break;
    }
    if (before.status != VoiceCallStatus.ended &&
        state.status == VoiceCallStatus.ended) {
      _settle();
    }
  }

  void _onServerAudio(int generation, Uint8List audio) {
    if (!_current(generation) || !state.active) return;
    _watchSilence();
    final closingPhrase =
        state.status == VoiceCallStatus.ending &&
        state.endingReason == VoiceEndReason.limit;
    if (state.status == VoiceCallStatus.connected || closingPhrase) {
      _audio?.play(audio);
    }
  }

  Future<void> _onClosed(int generation, VoiceSocket socket, int? code) async {
    if (!_current(generation) || !identical(_socket, socket)) return;
    _socket = null;
    if (!state.active) return;
    // No main conversation yet: create it and dial once more (§5.1).
    if (code == 4404 &&
        state.status == VoiceCallStatus.connecting &&
        !_ensured) {
      _ensured = true;
      final scope = _scope!;
      try {
        await _deps.ensureAssistant(scope);
      } catch (_) {
        // The second attempt says whether it worked.
      }
      if (!_current(generation) || state.status != VoiceCallStatus.connecting) {
        return;
      }
      await _connect(generation, scope);
      return;
    }
    final outcome = voiceCloseOutcome(code, state);
    _finish(outcome.reason, detailKey: outcome.detailKey);
  }

  void _watchSilence() {
    _silenceTimer?.cancel();
    _silenceTimer = Timer(silenceLimit, () => _finish(VoiceEndReason.network));
  }

  // ----------------------------------------------------------------- audio

  void _onPacket(Uint8List packet) {
    final socket = _socket;
    // Nothing goes up before `ready`, while paused, or once closing.
    if (socket == null || state.status != VoiceCallStatus.connected) return;
    if (state.muted) {
      socket.sendAudio(_silence);
      return;
    }
    micLevel.value = pcm16Level(packet);
    socket.sendAudio(packet);
  }

  void _copyOutputLevel() {
    final audio = _audio;
    if (audio != null) outputLevel.value = audio.outputLevel.value;
  }

  void _onAudioEvent(CallAudioEvent event) {
    switch (event) {
      case CallAudioInterruption(begin: true):
        _pause(VoicePauseCause.interruption);
      case CallAudioInterruption(begin: false):
        unawaited(_resume(VoicePauseCause.interruption));
      case CallAudioRouteChange(:final external):
        // Headset in: speaker off; out: speaker on. Unless chosen by hand.
        if (state.live && !_speakerChosen) {
          unawaited(_route(speaker: !external));
        }
      case CallAudioMicLost():
        _finish(VoiceEndReason.micLost);
    }
  }

  Future<void> _route({required bool speaker}) async {
    state = state.copyWith(speakerOn: speaker);
    try {
      await _audio?.setSpeaker(speaker);
    } catch (_) {
      // The route stays where the system put it.
    }
  }

  /// Phone call, Siri, another app's audio — or (Android) a minute in the
  /// background. Zero frames keep the socket alive; a minute later the call
  /// ends.
  void _pause(VoicePauseCause cause) {
    if (state.status != VoiceCallStatus.connected) return;
    state = state.copyWith(status: VoiceCallStatus.paused, pauseCause: cause);
    micLevel.value = 0;
    unawaited(_audio?.pause());
    _zeroTimer?.cancel();
    _zeroTimer = Timer.periodic(
      const Duration(milliseconds: 100),
      (_) => _socket?.sendAudio(_silence),
    );
    _pauseTimer?.cancel();
    _pauseTimer = Timer(pauseLimit, () => _finish(VoiceEndReason.error));
  }

  Future<void> _resume(VoicePauseCause cause) async {
    if (state.status != VoiceCallStatus.paused || state.pauseCause != cause) {
      return;
    }
    final generation = _generation;
    final resumed = await (_audio?.resume() ?? Future.value(false)).catchError(
      (Object _) => false,
    );
    if (!_current(generation) ||
        state.status != VoiceCallStatus.paused ||
        !resumed) {
      return; // Still held elsewhere: the pause limit decides.
    }
    _zeroTimer?.cancel();
    _pauseTimer?.cancel();
    state = state.copyWith(status: VoiceCallStatus.connected, pauseCause: null);
    // The interruption may have moved the route.
    await _route(speaker: state.speakerOn);
  }

  /// Android takes the microphone from apps in the background (iOS keeps
  /// it, with the audio background mode). After a minute away the call
  /// pauses; coming back resumes it.
  void _watchLifecycle() {
    _lifecycle?.dispose();
    _lifecycle = AppLifecycleListener(onStateChange: _onLifecycle);
  }

  void _onLifecycle(AppLifecycleState lifecycle) {
    if (defaultTargetPlatform != TargetPlatform.android) return;
    if (lifecycle == AppLifecycleState.resumed) {
      _backgroundTimer?.cancel();
      _backgroundTimer = null;
      unawaited(_resume(VoicePauseCause.background));
    } else if (lifecycle == AppLifecycleState.hidden ||
        lifecycle == AppLifecycleState.paused) {
      _backgroundTimer ??= Timer(backgroundGrace, () {
        _backgroundTimer = null;
        _pause(VoicePauseCause.background);
      });
    }
  }

  // ------------------------------------------------------------------- end

  /// Ends the call from this side (timeouts, close codes, local failures).
  void _finish(VoiceEndReason reason, {String? detailKey, String? message}) {
    if (!state.active) return;
    state = endVoiceCall(
      state,
      // A closing already under way (hang-up, time limit) keeps its reason.
      state.endingReason ?? reason,
      now: _hungUpAt ?? _deps.now(),
      detailKey: detailKey,
      message: message,
    );
    _settle();
  }

  /// The call has just ended, whichever side ended it.
  void _settle() {
    _cancelLiveTimers();
    _closingTimer?.cancel();
    _lifecycle?.dispose();
    _lifecycle = null;
    micLevel.value = 0;
    final reason = state.end?.reason;
    if (_audio != null) {
      final calm = _calmEnds.contains(reason);
      _deps.haptic(strong: !calm);
      _closeAudio(calm ? CallTone.ended : CallTone.error);
    }
    // A last `ended` or `cost` may follow an error; then let go.
    final socket = _socket;
    if (socket != null) {
      _closeGraceSocket();
      _graceSocket = socket;
      _graceTimer = Timer(closeGrace, _closeGraceSocket);
    }
  }

  void _closeGraceSocket() {
    _graceTimer?.cancel();
    _graceTimer = null;
    final socket = _graceSocket;
    _graceSocket = null;
    if (socket == null) return;
    socket.close();
    if (identical(_socket, socket)) _socket = null;
  }

  void _closeAudio(CallTone tone) {
    final audio = _audio;
    _audio = null;
    if (audio == null) return;
    audio.outputLevel.removeListener(_copyOutputLevel);
    outputLevel.value = 0;
    unawaited(_audioEvents?.cancel());
    _audioEvents = null;
    unawaited(audio.close(tone: tone).catchError((Object _) {}));
  }

  void _cancelLiveTimers() {
    _connectTimer?.cancel();
    _silenceTimer?.cancel();
    _pauseTimer?.cancel();
    _zeroTimer?.cancel();
    _backgroundTimer?.cancel();
    _backgroundTimer = null;
  }

  /// Lets go of everything at once, without tones (a cancelled dial, a
  /// disposed container).
  void _release() {
    _cancelLiveTimers();
    _closingTimer?.cancel();
    _closeGraceSocket();
    _lifecycle?.dispose();
    _lifecycle = null;
    _socket?.close();
    _socket = null;
    final audio = _audio;
    _audio = null;
    if (audio != null) {
      audio.outputLevel.removeListener(_copyOutputLevel);
      unawaited(audio.close().catchError((Object _) {}));
    }
    unawaited(_audioEvents?.cancel());
    _audioEvents = null;
  }
}
