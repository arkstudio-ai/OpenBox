import 'dart:async';
import 'dart:math' as math;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/models/session.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';
import '../../../shared/ws/ws_client.dart';
import '../api/chat_api.dart';
import '../utils/reasoning.dart';
import 'config_providers.dart';
import 'pending_store.dart';
import 'stream_store.dart';

/// Per-session orchestration (web `ChatRoute` + `useChatEvents` + the
/// polling queries): initial snapshot, 1s polling while busy, WS-reconnect
/// refetch, send/stop/regenerate. Streaming frames land in
/// [chatStreamProvider]; this controller only converges snapshots.
class ChatSessionState {
  const ChatSessionState({this.loading = true, this.session, this.failed = false});

  final bool loading;
  final Session? session;

  /// The last snapshot fetch failed (backend unreachable). The screen shows
  /// an error state with retry when there's nothing cached to render.
  final bool failed;

  ChatSessionState copyWith({bool? loading, Session? session, bool? failed}) =>
      ChatSessionState(
        loading: loading ?? this.loading,
        session: session ?? this.session,
        failed: failed ?? this.failed,
      );
}

String makeClientId() {
  final now = DateTime.now().millisecondsSinceEpoch.toRadixString(36);
  final rand = math.Random().nextInt(0xFFFFFF).toRadixString(36);
  return 'cmid-$now-$rand';
}

class ChatSessionController
    extends FamilyNotifier<ChatSessionState, String> {
  Timer? _poll;
  StreamSubscription<WsEvent>? _wsSub;
  bool _disposed = false;

  String get _sessionId => arg;

  @override
  ChatSessionState build(String sessionId) {
    _disposed = false;
    unawaited(_wsSub?.cancel());
    _wsSub = ref.read(wsClientProvider).events.listen(_onWsEvent);
    _poll?.cancel();
    _poll = Timer.periodic(const Duration(seconds: 1), (_) {
      if (_isBusy) unawaited(_refetch());
    });
    ref.onDispose(() {
      _disposed = true;
      _poll?.cancel();
      unawaited(_wsSub?.cancel());
    });
    unawaited(ref.read(wsClientProvider).connect());
    unawaited(_initialLoad());
    return const ChatSessionState();
  }

  bool get _isBusy {
    final stream = ref.read(chatStreamProvider);
    final live = stream.statusOf(_sessionId);
    if (live != null) return isBusyStatus(live);
    if (isBusyStatus(state.session?.status)) return true;
    // Busy fallback: any tool still pending/running (web ChatRoute:66-73).
    for (final m in stream.messagesOf(_sessionId)) {
      for (final p in m.parts) {
        if (p is ToolPart &&
            (p.status == ToolStatus.running || p.status == ToolStatus.pending)) {
          return true;
        }
      }
    }
    return false;
  }

  void _onWsEvent(WsEvent event) {
    if (event.type == '__connected') {
      unawaited(_refetch());
      return;
    }
    if (event.sessionId != _sessionId) return;
    if (event.type == 'session.status') {
      final status = ref.read(chatStreamProvider).statusOf(_sessionId);
      // Terminal transition → one consistency-barrier refetch.
      if (status == SessionStatus.idle || status == SessionStatus.error) {
        unawaited(_refetch());
      }
    }
  }

  Future<void> _initialLoad() async {
    await _refetch();
    if (_disposed) return;
    state = state.copyWith(loading: false);
    unawaited(_seedPending());
  }

  Future<void> _seedPending() async {
    try {
      final api = ref.read(chatApiProvider);
      final results =
          await Future.wait([api.listPermissions(), api.listQuestions()]);
      ref.read(pendingProvider.notifier).seed(
            (results[0] as List).cast(),
            (results[1] as List).cast(),
          );
    } catch (_) {
      // Pending seeds are best-effort; WS events keep them current.
    }
  }

  Future<void> _refetch() async {
    final api = ref.read(chatApiProvider);
    try {
      final results = await Future.wait<dynamic>([
        api.listMessages(_sessionId),
        api.getSession(_sessionId),
      ]);
      if (_disposed) return;
      final messages = results[0] as List<ChatMessage>;
      final session = results[1] as Session;
      ref.read(chatStreamProvider.notifier).setMessages(_sessionId, messages);
      state = state.copyWith(session: session, loading: false, failed: false);
    } catch (_) {
      if (!_disposed) state = state.copyWith(loading: false, failed: true);
    }
  }

  /// Optimistic send (web `useSendChat`): tmp message + busy + prompt_async.
  /// Model/agent/reasoning come from the per-session picks, falling back to
  /// the session's own values.
  ///
  /// Rethrows on rejection so the composer knows the send never happened and
  /// can keep the draft. Swallowing it here left an empty box that read as
  /// "sent".
  Future<void> send(String text, {List<String> attachments = const []}) async {
    final model = ref.read(pickedModelProvider(_sessionId));
    final agent =
        ref.read(pickedAgentProvider(_sessionId)) ?? state.session?.agent;
    final variant = _reasoningValue(model);
    final cmid = makeClientId();
    final stream = ref.read(chatStreamProvider.notifier);
    stream.addMessage(
      _sessionId,
      ChatMessage(
        id: 'tmp-$cmid',
        sessionId: _sessionId,
        role: 'user',
        parts: [TextPart(id: 'tmp-part-$cmid', text: text)],
        createdAt: DateTime.now(),
        clientMessageId: cmid,
      ),
    );
    stream.setStatus(_sessionId, SessionStatus.busy);
    stream.clearRunError(_sessionId);
    try {
      await ref.read(chatApiProvider).promptAsync(
            _sessionId,
            text: text,
            clientMessageId: cmid,
            agent: agent,
            model: model,
            variant: variant,
            attachments: attachments,
          );
    } catch (error) {
      stream.setStatus(_sessionId, SessionStatus.idle);
      // Take the optimistic echo back down. Leaving it there showed the
      // message sitting in the transcript as though it had been sent, which
      // is the opposite of what happened.
      stream.dropOptimistic(_sessionId, cmid);
      ref
          .read(toastProvider.notifier)
          .error(errorText(ref.read(i18nProvider), error));
      rethrow;
    }
  }

  /// The reasoning field for the next prompt: the unsent pick for this
  /// conversation/model pair, resolved against what the session stores.
  Variant? _reasoningValue(String? pickedModel) {
    final config = ref.read(appConfigProvider).valueOrNull;
    final session = state.session;
    final modelId = activeModelId(
      picked: pickedModel,
      sessionModel: session?.model,
      defaultModel: config?.defaultModel,
    );
    return resolveReasoning(
      model: config?.byId(modelId),
      sessionModel: session?.model,
      sessionVariant: session?.variant,
      pick: ref.read(pickedVariantProvider(reasoningKey(_sessionId, modelId))),
    ).value;
  }

  /// Manual retry from the error state.
  Future<void> reload() async {
    state = state.copyWith(loading: true, failed: false);
    await _refetch();
  }

  /// Stop generation (web `stop()`): abort + optimistic idle.
  Future<void> stop() async {
    ref.read(chatStreamProvider.notifier).setStatus(_sessionId, SessionStatus.idle);
    try {
      await ref.read(chatApiProvider).abort(_sessionId);
    } catch (_) {
      // Already idle server-side is fine.
    }
  }

  Future<void> regenerate(String messageId, {String? model}) async {
    await ref.read(chatApiProvider).regenerate(_sessionId, messageId, model: model);
    ref.read(chatStreamProvider.notifier).clearMessages(_sessionId);
    await _refetch();
  }

  Future<void> dismiss(String messageId) async {
    await ref.read(chatApiProvider).dismissMessage(_sessionId, messageId);
    ref.read(chatStreamProvider.notifier).clearMessages(_sessionId);
    await _refetch();
  }
}

final chatSessionProvider = NotifierProvider.family<ChatSessionController,
    ChatSessionState, String>(ChatSessionController.new);

/// Unsent per-session model pick (web `stores/model-choice.ts`); null means
/// "keep the session's model".
final pickedModelProvider =
    StateProvider.family<String?, String>((ref, sessionId) => null);

/// Unsent reasoning pick, keyed by [reasoningKey] — a conversation *and* a
/// model. A null state means nothing was picked for that pair, which is not
/// the same as picking "default" (see [Variant]).
final pickedVariantProvider =
    StateProvider.family<Variant?, String>((ref, key) => null);
