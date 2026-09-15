import 'dart:async';
import 'dart:math' as math;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/events/app_lifecycle.dart';
import '../../../shared/events/bus.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
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

/// Turns in each history read: the window a chat opens on and re-reads at
/// every consistency barrier, and each older page scrolling up brings in.
const chatHistoryTurns = 8;

/// Per-session orchestration (web `ChatRoute` + `useChatEvents` + the
/// polling queries): the newest history window on open, a 1s catch-up while
/// busy, window refreshes on reconnect/terminal status/questions/stop, older
/// pages on demand, send/stop/regenerate. Streaming frames land in
/// [chatStreamProvider]; this controller only converges history reads.
class ChatSessionState {
  const ChatSessionState({
    this.loading = true,
    this.session,
    this.failed = false,
    this.hasMore = false,
    this.loadingOlder = false,
  });

  final bool loading;
  final Session? session;

  /// The last snapshot fetch failed (backend unreachable). The screen shows
  /// an error state with retry when there's nothing cached to render.
  final bool failed;

  /// The server holds messages older than the oldest one loaded here.
  final bool hasMore;

  /// The page before the oldest loaded message is being fetched.
  final bool loadingOlder;

  ChatSessionState copyWith({
    bool? loading,
    Session? session,
    bool? failed,
    bool? hasMore,
    bool? loadingOlder,
  }) => ChatSessionState(
    loading: loading ?? this.loading,
    session: session ?? this.session,
    failed: failed ?? this.failed,
    hasMore: hasMore ?? this.hasMore,
    loadingOlder: loadingOlder ?? this.loadingOlder,
  );
}

String makeClientId() {
  final now = DateTime.now().millisecondsSinceEpoch.toRadixString(36);
  final rand = math.Random().nextInt(0xFFFFFF).toRadixString(36);
  return 'cmid-$now-$rand';
}

class ChatSessionController extends FamilyNotifier<ChatSessionState, String> {
  Timer? _poll;
  StreamSubscription<WsEvent>? _wsSub;
  StreamSubscription<AppEvent>? _appSub;

  /// Bumped when a read starts and when held history is thrown away. A read
  /// that finishes under another number answers for a history that is gone,
  /// and changes nothing.
  int _fetchSequence = 0;
  bool _disposed = false;

  /// Someone is looking: false from when the last listener — the chat
  /// screen — goes until one comes back. A controller nobody has listened to
  /// yet (the empty screen's first send, a test reading it) counts as watched.
  bool _watched = true;

  /// Session reads started, and the newest of them applied. A read that lands
  /// after a newer one was applied is stale and changes nothing.
  int _sessionReads = 0;
  int _sessionApplied = 0;

  /// The one history read allowed in flight for this session. Unguarded
  /// full-history reads of a long chat used to start faster than they
  /// finished, and overlapped.
  Future<void>? _inFlight;

  /// A window refresh asked for while [_inFlight] ran: however many asked,
  /// one read runs after it, and every asker waits for that one.
  Completer<void>? _latestQueued;

  /// An older page asked for while [_inFlight] ran.
  bool _olderQueued = false;

  String get _sessionId => arg;

  @override
  ChatSessionState build(String sessionId) {
    _disposed = false;
    _watched = true;
    unawaited(_wsSub?.cancel());
    _wsSub = ref.read(wsClientProvider).events.listen(_onWsEvent);
    unawaited(_appSub?.cancel());
    _appSub = ref.read(appEventBusProvider).on('question.resolved').listen((
      event,
    ) {
      if (_watched && event.payload['sessionId'] == _sessionId) {
        unawaited(_refetch());
        unawaited(_seedPending());
      }
    });
    _startPolling();
    // The chat screen is the listener. Once it had gone, a busy conversation
    // was still polled every second, and re-read on every reconnect, for as
    // long as the app ran.
    ref.onCancel(() {
      _watched = false;
      _poll?.cancel();
    });
    // Back on screen: converge once, as the web re-reads the newest turns on
    // every mount.
    ref.onResume(() {
      _watched = true;
      _startPolling();
      unawaited(_refetch());
      unawaited(_seedPending());
    });
    ref.onDispose(() {
      _disposed = true;
      _poll?.cancel();
      unawaited(_wsSub?.cancel());
      unawaited(_appSub?.cancel());
    });
    unawaited(ref.read(wsClientProvider).connect());
    unawaited(_initialLoad());
    return const ChatSessionState();
  }

  void _startPolling() {
    _poll?.cancel();
    _poll = Timer.periodic(const Duration(seconds: 1), (_) {
      // Nobody reads a backgrounded app; the first tick after it resumes
      // catches up.
      if (_isLive && ref.read(appResumedProvider)) _catchUp();
    });
  }

  /// Web ChatRoute's `live`: the socket's status, else the session record's,
  /// is a busy one. A queued run has nothing to catch up on until the socket
  /// says it started.
  bool get _isLive => isBusyStatus(
    ref.read(chatStreamProvider).statusOf(_sessionId) ?? state.session?.status,
  );

  void _onWsEvent(WsEvent event) {
    // Off screen nothing is fetched; coming back converges once.
    if (!_watched) return;
    if (event.type == '__connected') {
      unawaited(_refetch());
      unawaited(_seedPending());
      return;
    }
    if (event.sessionId != _sessionId) return;
    switch (event.type) {
      // The pending store adds and removes question cards from these frames
      // itself; only an answer changes the transcript (web useChatEvents).
      case 'question.replied' || 'question.rejected' || 'question.cancelled':
        unawaited(_refetch());
      case 'session.status':
        // Decided from the frame, not the store, which may not have applied
        // it yet: the first chat opened after launch subscribes before the
        // store exists, and read the end of every run as still busy.
        final raw = asString(event.data['status']);
        final status = raw == null
            ? ref.read(chatStreamProvider).statusOf(_sessionId)
            : sessionStatusFrom(raw);
        // Terminal transition → one consistency-barrier refetch.
        if (status == SessionStatus.idle ||
            status == SessionStatus.error ||
            status == SessionStatus.waitingInput ||
            status == SessionStatus.queued) {
          unawaited(_refetch());
        }
      case 'session.updated':
        // Agent, mode or title. Polls no longer read the session while the
        // socket is up; this is what keeps the mode picker current.
        unawaited(_refreshSession());
    }
  }

  Future<void> _initialLoad() async {
    await _refetch();
    if (_disposed) return;
    state = state.copyWith(loading: false);
    unawaited(_seedPending());
  }

  Future<void> _seedPending() async {
    if (_disposed) return;
    // All controllers share one ordering guard: an older reconnect response
    // must not replace questions already fetched by a newer request.
    await ref.read(pendingProvider.notifier).refreshAll();
  }

  /// Re-read the newest window and merge it over what is held, so older
  /// pages already loaded stay. Waits behind a read already in flight.
  Future<void> _refetch() {
    if (_inFlight == null) return _start(_loadLatest);
    return (_latestQueued ??= Completer<void>()).future;
  }

  /// The busy poll: everything from the newest held message on. Skipped
  /// while another read is in flight or waiting — the next tick is a second
  /// away, and the waiting read brings the news anyway.
  void _catchUp() {
    if (_inFlight != null || _latestQueued != null || _olderQueued) return;
    unawaited(_start(_loadNewer));
  }

  /// Fetch the page before the oldest held message, if the server has one.
  Future<void> loadOlder() {
    if (_disposed || !state.hasMore || state.loadingOlder) {
      return Future.value();
    }
    state = state.copyWith(loadingOlder: true);
    if (_inFlight == null) return _start(_loadOlder);
    _olderQueued = true;
    return Future.value();
  }

  Future<void> _start(Future<void> Function(int sequence) read) {
    final future = read(++_fetchSequence);
    _inFlight = future;
    return future.whenComplete(() {
      _inFlight = null;
      _drain();
    });
  }

  /// Start whatever queued up behind the read that just finished.
  void _drain() {
    final latest = _latestQueued;
    _latestQueued = null;
    if (_disposed) {
      _olderQueued = false;
      latest?.complete();
      return;
    }
    if (latest != null) {
      unawaited(_start(_loadLatest).whenComplete(latest.complete));
      return;
    }
    if (_olderQueued) {
      _olderQueued = false;
      unawaited(_start(_loadOlder));
    }
  }

  /// Throw away the history held for this session and read the newest window
  /// again: regenerate and dismiss deleted messages, or a read's anchor
  /// vanished. A read still in flight answers for the old history and is
  /// ignored when it lands.
  Future<void> _resetHistory() {
    if (_disposed) return Future.value();
    _fetchSequence++;
    _olderQueued = false;
    ref.read(chatStreamProvider.notifier).resetHistory(_sessionId);
    state = state.copyWith(hasMore: false, loadingOlder: false);
    return _refetch();
  }

  Future<void> _loadLatest(int sequence) async {
    final api = ref.read(chatApiProvider);
    final sessionRead = ++_sessionReads;
    final statusAtStart = ref.read(chatStreamProvider).statusOf(_sessionId);
    try {
      final results = await Future.wait<Object>([
        api.history(_sessionId, turns: chatHistoryTurns),
        api.getSession(_sessionId),
      ]);
      if (_disposed || sequence != _fetchSequence) return;
      _applyWindow(results[0] as HistoryPage);
      _applySession(results[1] as Session, statusAtStart, sessionRead);
      _loaded();
    } catch (_) {
      if (!_disposed && sequence == _fetchSequence) {
        state = state.copyWith(loading: false, failed: true);
      }
    }
  }

  Future<void> _loadNewer(int sequence) async {
    final after = ref.read(chatStreamProvider).newestHistoryId(_sessionId);
    // Nothing confirmed to read on from: the newest window is the catch-up.
    if (after == null) return _loadLatest(sequence);
    final api = ref.read(chatApiProvider);
    // While the socket is up it brings status and session changes, and
    // reading the session on every tick as well doubled a live run's
    // requests. While it is down, this tick is the only news there is.
    final sessionRead = ref.read(wsClientProvider).connected
        ? null
        : ++_sessionReads;
    final statusAtStart = ref.read(chatStreamProvider).statusOf(_sessionId);
    try {
      final results = await Future.wait<Object>([
        api.history(_sessionId, after: after),
        if (sessionRead != null) api.getSession(_sessionId),
      ]);
      if (_disposed || sequence != _fetchSequence) return;
      ref
          .read(chatStreamProvider.notifier)
          .mergeHistory(_sessionId, (results[0] as HistoryPage).messages);
      if (sessionRead != null) {
        _applySession(results[1] as Session, statusAtStart, sessionRead);
      }
      _loaded();
    } catch (error) {
      if (_disposed || sequence != _fetchSequence) return;
      if (isHistoryCursorGone(error)) {
        // Not awaited: the reset's read queues behind this one.
        unawaited(_resetHistory());
        return;
      }
      state = state.copyWith(loading: false, failed: true);
    }
  }

  Future<void> _loadOlder(int sequence) async {
    final before = ref.read(chatStreamProvider).oldestHistoryId(_sessionId);
    if (before == null) {
      if (!_disposed) state = state.copyWith(loadingOlder: false);
      return;
    }
    try {
      final page = await ref
          .read(chatApiProvider)
          .history(_sessionId, before: before, turns: chatHistoryTurns);
      if (_disposed || sequence != _fetchSequence) return;
      ref
          .read(chatStreamProvider.notifier)
          .mergeHistory(_sessionId, page.messages);
      state = state.copyWith(hasMore: page.hasMore, loadingOlder: false);
    } catch (error) {
      if (_disposed || sequence != _fetchSequence) return;
      if (isHistoryCursorGone(error)) {
        unawaited(_resetHistory());
        return;
      }
      // hasMore stands, so leaving the top and coming back retries.
      state = state.copyWith(loadingOlder: false);
    }
  }

  /// Merge a newest window. Its `has_more` describes what precedes the
  /// window, so it only counts when nothing older than the window is held:
  /// older pages already loaded carry their own answer.
  void _applyWindow(HistoryPage page) {
    final store = ref.read(chatStreamProvider.notifier);
    final held = ref.read(chatStreamProvider);
    final first = page.messages.firstOrNull?.id;
    final newestHeld = held.newestHistoryId(_sessionId);
    final oldestHeld = held.oldestHistoryId(_sessionId);
    var takeHasMore =
        first == null || oldestHeld == null || oldestHeld.compareTo(first) >= 0;
    if (first != null &&
        newestHeld != null &&
        newestHeld.compareTo(first) < 0) {
      // Nothing held reaches the window: more turns went by than one window
      // holds since this view last caught up (a long disconnect, another
      // device). Merging would leave a silent hole between the two, so start
      // from the window; scrolling up loads the rest back.
      store.resetHistory(_sessionId);
      takeHasMore = true;
    }
    store.mergeHistory(_sessionId, page.messages);
    if (takeHasMore) state = state.copyWith(hasMore: page.hasMore);
  }

  /// A session read landed. One that started before a read already applied
  /// is stale: it would put back an agent or status the newer one replaced.
  /// The status moves only if the socket left it alone while the read was
  /// out (web `useSessionQuery`).
  void _applySession(Session session, SessionStatus? statusAtStart, int read) {
    if (read < _sessionApplied) return;
    _sessionApplied = read;
    if (ref.read(chatStreamProvider).statusOf(_sessionId) == statusAtStart) {
      ref
          .read(chatStreamProvider.notifier)
          .setStatus(_sessionId, session.status);
    }
    state = state.copyWith(session: session);
  }

  /// A history read landed. Publishes only a change: a poll that found
  /// nothing new must not rebuild the screen every second.
  void _loaded() {
    if (state.loading || state.failed) {
      state = state.copyWith(loading: false, failed: false);
    }
  }

  /// The socket said the session record changed: read it once. History, and
  /// the loading and error states that belong to it, stay as they are.
  Future<void> _refreshSession() async {
    final api = ref.read(chatApiProvider);
    final sessionRead = ++_sessionReads;
    final statusAtStart = ref.read(chatStreamProvider).statusOf(_sessionId);
    try {
      final session = await api.getSession(_sessionId);
      if (!_disposed) _applySession(session, statusAtStart, sessionRead);
    } catch (_) {
      // The next window read brings the record along.
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
    final video = ref.read(pickedVideoProvider(_sessionId));
    final cmid = makeClientId();
    final stream = ref.read(chatStreamProvider.notifier);
    final oldQuestions = ref
        .read(pendingProvider)
        .questionsOf(_sessionId)
        .map((q) => q.id)
        .toList();
    final previousStatus =
        ref.read(chatStreamProvider).statusOf(_sessionId) ??
        state.session?.status ??
        SessionStatus.idle;
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
      await ref
          .read(chatApiProvider)
          .promptAsync(
            _sessionId,
            text: text,
            clientMessageId: cmid,
            agent: agent,
            model: model,
            variant: variant,
            videoModel: video?.modelId,
            videoResolution: video?.resolution,
            attachments: attachments,
          );
      if (!_disposed) {
        final pending = ref.read(pendingProvider.notifier);
        for (final id in oldQuestions) {
          pending.removeQuestion(id);
        }
        unawaited(pending.refreshQuestions());
      }
    } catch (error) {
      stream.setStatus(_sessionId, previousStatus);
      // Take the optimistic echo back down. Leaving it there showed the
      // message sitting in the transcript as though it had been sent, which
      // is the opposite of what happened.
      stream.dropOptimistic(_sessionId, cmid);
      if (apiErrorOf(error)?.code == 'DESKTOP_NOT_READY') {
        ref.read(appEventBusProvider).emit('workbench.open', {
          'kind': 'desktop',
          'sessionId': _sessionId,
        });
      }
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

  /// Cancel running/queued/waiting work only after the server accepts it.
  Future<void> stop() async {
    final oldQuestions = ref
        .read(pendingProvider)
        .questionsOf(_sessionId)
        .map((question) => question.id)
        .toList();
    try {
      await ref.read(chatApiProvider).abort(_sessionId);
      if (_disposed) return;
      ref
          .read(chatStreamProvider.notifier)
          .setStatus(_sessionId, SessionStatus.idle);
      final pending = ref.read(pendingProvider.notifier);
      for (final id in oldQuestions) {
        pending.removeQuestion(id);
      }
      unawaited(pending.refreshQuestions());
      await _refetch();
    } catch (error) {
      if (!_disposed) {
        ref
            .read(toastProvider.notifier)
            .error(errorText(ref.read(i18nProvider), error));
      }
    }
  }

  Future<void> regenerate(String messageId, {String? model}) async {
    await ref
        .read(chatApiProvider)
        .regenerate(_sessionId, messageId, model: model);
    await _resetHistory();
  }

  Future<void> dismiss(String messageId) async {
    await ref.read(chatApiProvider).dismissMessage(_sessionId, messageId);
    await _resetHistory();
  }
}

final chatSessionProvider =
    NotifierProvider.family<ChatSessionController, ChatSessionState, String>(
      ChatSessionController.new,
    );

/// Unsent per-session model pick (web `stores/model-choice.ts`); null means
/// "keep the session's model".
final pickedModelProvider = StateProvider.family<String?, String>(
  (ref, sessionId) => null,
);

/// A video model and the resolution chosen with it (web
/// `stores/video-model-choice.ts`). The pair travels together because neither
/// means anything alone: a model's tiers are its own, and the same "720p"
/// costs and looks different per model.
class VideoPick {
  const VideoPick(this.modelId, this.resolution);

  final String modelId;
  final String resolution;
}

/// Unsent per-session video pick; null means "keep whatever the session
/// records". No default is substituted here — for video that pin costs money.
final pickedVideoProvider = StateProvider.family<VideoPick?, String>(
  (ref, sessionId) => null,
);

/// Unsent reasoning pick, keyed by [reasoningKey] — a conversation *and* a
/// model. A null state means nothing was picked for that pair, which is not
/// the same as picking "default" (see [Variant]).
final pickedVariantProvider = StateProvider.family<Variant?, String>(
  (ref, key) => null,
);
