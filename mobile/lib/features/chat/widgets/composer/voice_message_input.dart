import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';
import 'package:record/record.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../../../../shared/api/api_error.dart';
import '../../../../shared/api/providers.dart';
import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/utils/error_text.dart';
import '../../../voice/state/voice_call_controller.dart';
import '../../api/assistant_api.dart';
import '../../state/assistant_controller.dart';

/// Small injectable recorder boundary; microphone and gesture races are tested
/// without needing native audio in a widget test.
abstract class MessageRecorder {
  Future<bool> permitted();
  Future<void> start(String path);
  Future<void> stop();
  Future<void> dispose();
}

class DeviceMessageRecorder implements MessageRecorder {
  final _record = AudioRecorder();
  @override
  Future<bool> permitted() => _record.hasPermission();
  @override
  Future<void> start(String path) => _record.start(
    const RecordConfig(
      encoder: AudioEncoder.aacLc,
      sampleRate: 16000,
      numChannels: 1,
      bitRate: 64000,
      audioInterruption: AudioInterruptionMode.pause,
    ),
    path: path,
  );
  @override
  Future<void> stop() async {
    await _record.stop();
  }

  @override
  Future<void> dispose() => _record.dispose();
}

final messageRecorderFactoryProvider = Provider<MessageRecorder Function()>(
  (ref) => DeviceMessageRecorder.new,
);
final voiceDraftDirectoryProvider = Provider<Future<String> Function()>(
  (ref) =>
      () async => (await getApplicationSupportDirectory()).path,
);

/// Owns one durable draft. Upload / ASR / send retries keep its asset identity;
/// leaving this page or restarting the app never invents a new assistant turn.
class VoiceMessageInput extends ConsumerStatefulWidget {
  const VoiceMessageInput({
    super.key,
    required this.scope,
    required this.sessionId,
    this.conversation = false,
    this.prepareSession,
    this.onSend,
    this.onSent,
    this.onTranscript,
    this.disabled = false,
  });
  final AssistantScope scope;
  final String sessionId;
  final bool conversation, disabled;
  final Future<String> Function()? prepareSession;
  final Future<void> Function(
    String sessionId,
    String text,
    String clientId,
    Map<String, dynamic> voice,
  )?
  onSend;
  final ValueChanged<String>? onSent;
  final ValueChanged<String>? onTranscript;
  @override
  ConsumerState<VoiceMessageInput> createState() => _VoiceMessageInputState();
}

class _VoiceMessageInputState extends ConsumerState<VoiceMessageInput>
    with WidgetsBindingObserver {
  late final MessageRecorder _recorder;
  late final SharedPreferences _prefs;
  final _clock = Stopwatch();
  Timer? _timer;
  bool _pressing = false,
      _recording = false,
      _starting = false,
      _busy = false,
      _cancel = false;
  bool _ready = false;
  String? _path, _error, _phase;
  Map<String, dynamic>? _draft;
  String get _draftKey =>
      '${widget.conversation ? 'conversation-voice' : 'voice-message'}:${widget.scope.userId}:${widget.scope.workspaceId}:${widget.sessionId}';
  AssistantApi get _api => ref.read(assistantApiProvider(widget.scope));

  @override
  void initState() {
    super.initState();
    _recorder = ref.read(messageRecorderFactoryProvider)();
    _prefs = ref.read(prefsProvider);
    WidgetsBinding.instance.addObserver(this);
    final saved = _prefs.getString(_draftKey);
    if (saved != null) {
      try {
        _draft = Map<String, dynamic>.from(jsonDecode(saved) as Map);
      } catch (_) {
        _error = 'chat:voiceMessage.restoreFailed';
      }
    }
    _ready = true;
  }

  Future<void> _save() async {
    final prefs = _prefs;
    final ok = _draft == null
        ? await prefs.remove(_draftKey)
        : await prefs.setString(_draftKey, jsonEncode(_draft));
    if (!ok) throw StateError('Could not save voice draft');
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed && (_recording || _starting)) {
      _pressing = false;
      unawaited(_finish(cancel: true));
    }
  }

  Future<void> _start() async {
    if (widget.disabled ||
        !_ready ||
        _busy ||
        _starting ||
        _recording ||
        _draft != null) {
      return;
    }
    if (ref.read(voiceCallControllerProvider).active) {
      setState(() => _error = 'chat:voiceMessage.callActive');
      return;
    }
    _pressing = true;
    setState(() {
      _starting = true;
      _error = null;
      _cancel = false;
    });
    try {
      if (!await _recorder.permitted()) {
        if (mounted) setState(() => _error = 'chat:voiceMessage.permission');
        return;
      }
      if (!mounted || !_pressing) return;
      final directory = await ref.read(voiceDraftDirectoryProvider)();
      if (!mounted || !_pressing) return;
      _path = '$directory/voice-${DateTime.now().microsecondsSinceEpoch}.m4a';
      await _recorder.start(_path!);
      if (!mounted || !_pressing) {
        await _recorder.stop();
        return;
      }
      _clock
        ..reset()
        ..start();
      setState(() => _recording = true);
      unawaited(HapticFeedback.lightImpact());
      _timer = Timer.periodic(const Duration(milliseconds: 200), (_) {
        if (_clock.elapsed >= const Duration(seconds: 60)) {
          unawaited(_finish(cancel: _cancel));
        } else if (mounted) {
          setState(() {});
        }
      });
    } catch (_) {
      if (mounted) setState(() => _error = 'chat:voiceMessage.micFailed');
    } finally {
      if (mounted) setState(() => _starting = false);
    }
  }

  Future<void> _finish({required bool cancel}) async {
    _pressing = false;
    if (!_recording) return;
    _timer?.cancel();
    _clock.stop();
    final ms = _clock.elapsedMilliseconds;
    setState(() {
      _recording = false;
      _busy = true;
    });
    try {
      await _recorder.stop();
      if (!mounted) return;
      if (cancel || ms < 600) {
        if (!cancel) setState(() => _error = 'chat:voiceMessage.tooShort');
        return;
      }
      _draft = {
        'path': _path,
        'duration_ms': ms,
        'client_id': 'voice-${DateTime.now().microsecondsSinceEpoch}',
      };
      await _save();
      if (!mounted) return;
    } catch (_) {
      if (mounted) setState(() => _error = 'chat:voiceMessage.micFailed');
      return;
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    if (!cancel && _draft != null && mounted) await _send();
  }

  Future<void> _send() async {
    if (_busy || _draft == null) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final draft = _draft!;
      if (draft['asset_id'] == null) {
        setState(() => _phase = 'uploading');
        final bytes = await File(draft['path'] as String).readAsBytes();
        if (!mounted) return;
        draft['asset_id'] = await _api.uploadVoice(
          bytes,
          widget.conversation ? null : widget.sessionId,
        );
        await _save();
      }
      if (!mounted) return;
      if (draft['text'] == null) {
        setState(() => _phase = 'transcribing');
        final result = widget.conversation
            ? await _api.transcribeRecording(draft['asset_id'] as String)
            : await _api.transcribeVoice(
                draft['asset_id'] as String,
                widget.sessionId,
              );
        draft.addAll(result);
        await _save();
      }
      if (!mounted) return;
      if (widget.onTranscript != null) {
        widget.onTranscript!(draft['text'] as String);
        _draft = null;
        await _save();
        return;
      }
      if (widget.conversation && draft['target_session_id'] == null) {
        draft['target_session_id'] =
            await widget.prepareSession?.call() ?? widget.sessionId;
        await _save();
      }
      if (!mounted) return;
      setState(() => _phase = 'sending');
      // Persist before submission: on restart only a retry may resolve an
      // uncertain receipt. Discarding it could strand the controller's key.
      draft['submitted'] = true;
      await _save();
      if (!mounted) return;
      if (widget.onSend != null) {
        await widget.onSend!(
          draft['target_session_id'] as String,
          draft['text'] as String,
          draft['client_id'] as String,
          {'asset_id': draft['asset_id'], 'duration_ms': draft['duration_ms']},
        );
      } else {
        await ref
            .read(assistantControllerProvider(widget.scope).notifier)
            .send(
              draft['text'] as String,
              const [],
              clientId: draft['client_id'] as String,
              voiceMessage: {
                'asset_id': draft['asset_id'],
                'duration_ms': draft['duration_ms'],
              },
            );
      }
      _draft = null;
      await _save();
      if (mounted) {
        widget.onSent?.call(
          draft['target_session_id'] as String? ?? widget.sessionId,
        );
      }
    } catch (error) {
      if (mounted) {
        setState(() => _error = errorText(ref.read(i18nProvider), error));
      }
      final apiError = apiErrorOf(error);
      if (_draft != null &&
          apiError != null &&
          apiError.status >= 400 &&
          apiError.status < 500 &&
          apiError.status != 408 &&
          apiError.status != 409 &&
          apiError.status != 429) {
        _draft!['submitted'] = false;
        if (mounted) await _save();
      }
    } finally {
      if (mounted) {
        setState(() {
          _busy = false;
          _phase = null;
        });
      }
    }
  }

  Future<void> _discard() async {
    if (_busy || _draft?['submitted'] == true) return;
    _draft = null;
    await _save();
    if (mounted) setState(() => _error = null);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _pressing = false;
    _timer?.cancel();
    unawaited(_recorder.dispose());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final draft = _draft;
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (draft != null) ...[
          Align(
            alignment: Alignment.centerRight,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
              decoration: BoxDecoration(
                color: t.n200,
                borderRadius: BorderRadius.circular(Radii.xl),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.graphic_eq, size: 22),
                  const SizedBox(width: 12),
                  Text('${((draft['duration_ms'] as num) / 1000).ceil()}″'),
                  if (_busy) ...[
                    const SizedBox(width: 12),
                    const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                  ],
                ],
              ),
            ),
          ),
          if (_phase != null)
            Text(
              i18n.t('chat:voiceMessage.$_phase'),
              textAlign: TextAlign.right,
            ),
          if (!_busy)
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                if (draft['submitted'] != true)
                  TextButton(
                    onPressed: _discard,
                    child: Text(i18n.t('chat:voiceMessage.discard')),
                  ),
                TextButton(
                  key: const Key('voice-message-retry'),
                  onPressed: widget.disabled ? null : _send,
                  child: Text(
                    i18n.t(
                      widget.onTranscript == null
                          ? 'chat:voiceMessage.retry'
                          : 'chat:voiceMessage.retryFill',
                    ),
                  ),
                ),
              ],
            ),
        ] else
          GestureDetector(
            key: const Key('voice-message-hold'),
            onLongPressStart: widget.disabled
                ? null
                : (_) => unawaited(_start()),
            onLongPressMoveUpdate: (event) {
              if (_recording || _starting) {
                setState(() => _cancel = event.offsetFromOrigin.dy < -60);
              }
            },
            onLongPressEnd: (_) => unawaited(_finish(cancel: _cancel)),
            onLongPressCancel: () {
              _pressing = false;
              unawaited(_finish(cancel: true));
            },
            child: Semantics(
              button: true,
              label: i18n.t('chat:voiceMessage.hold'),
              child: Container(
                constraints: const BoxConstraints(minHeight: 48),
                padding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 12,
                ),
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: _recording ? t.n300 : t.n100,
                  borderRadius: BorderRadius.circular(Radii.lg),
                ),
                child: Text(
                  _recording
                      ? '${i18n.t(_cancel ? 'chat:voiceMessage.releaseCancel' : (widget.onTranscript != null ? 'chat:voiceMessage.releaseFill' : 'chat:voiceMessage.releaseSend'))} ${_clock.elapsed.inSeconds}″'
                      : i18n.t(
                          _starting
                              ? 'chat:voiceMessage.starting'
                              : 'chat:voiceMessage.hold',
                        ),
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    color: t.ink,
                    fontSize: FontSizes.lg,
                    height: 1.4,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ),
          ),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(
              _error!.startsWith('chat:') ? i18n.t(_error!) : _error!,
              style: TextStyle(color: t.n700, fontSize: 12),
            ),
          ),
        if (_recording)
          Text(
            i18n.t('chat:voiceMessage.slideCancel'),
            textAlign: TextAlign.center,
          ),
      ],
    );
  }
}
