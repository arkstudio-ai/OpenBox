import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:crypto/crypto.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/events/app_lifecycle.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/ws/ws_client.dart';
import '../api/assistant_api.dart';
import '../utils/reasoning.dart';
import 'assistant_overview.dart';
import 'assistant_watch.dart';
import 'chat_session_controller.dart';
import 'config_providers.dart';

class AssistantState {
  const AssistantState({
    this.snapshot,
    this.messages = const [],
    this.loading = true,
    this.error,
    this.hasMore = false,
    this.loadingOlder = false,
    this.sending = false,
    this.sendUncertain = false,
    this.sendAccepted = false,
    this.tasks = const [],
    this.taskCursor,
    this.actionPending = const {},
  });
  final AssistantSnapshot? snapshot;
  final List<ChatMessage> messages;
  final bool loading,
      hasMore,
      loadingOlder,
      sending,
      sendUncertain,
      sendAccepted;
  final Object? error;
  final List<AssistantTask> tasks;
  final String? taskCursor;
  final Map<String, String> actionPending;

  AssistantState copyWith({
    AssistantSnapshot? snapshot,
    List<ChatMessage>? messages,
    bool? loading,
    bool? hasMore,
    bool? loadingOlder,
    bool? sending,
    bool? sendUncertain,
    bool? sendAccepted,
    List<AssistantTask>? tasks,
    Map<String, String>? actionPending,
  }) => AssistantState(
    snapshot: snapshot ?? this.snapshot,
    messages: messages ?? this.messages,
    loading: loading ?? this.loading,
    hasMore: hasMore ?? this.hasMore,
    loadingOlder: loadingOlder ?? this.loadingOlder,
    sending: sending ?? this.sending,
    sendUncertain: sendUncertain ?? this.sendUncertain,
    sendAccepted: sendAccepted ?? this.sendAccepted,
    tasks: tasks ?? this.tasks,
    taskCursor: taskCursor,
    actionPending: actionPending ?? this.actionPending,
  );
}

String _digest(Object value) =>
    sha256.convert(utf8.encode(jsonEncode(value))).toString();
String _identity() =>
    'mobile-${List.generate(24, (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0')).join()}';
bool _definitive(Object error) {
  final status = apiErrorOf(error)?.status ?? 0;
  return status >= 400 && status < 500 && status != 408 && status != 429;
}

int _byId(ChatMessage a, ChatMessage b) => a.id.compareTo(b.id);

/// One owner/workspace-bound view. The transcript pages like an ordinary
/// chat (V2): the newest turns from the session history, older pages as the
/// reader scrolls up. Socket frames are hints to read again, never bodies.
class AssistantController
    extends AutoDisposeFamilyNotifier<AssistantState, AssistantScope> {
  bool _disposed = false;
  Future<void>? _refreshing;
  bool _refreshAgain = false;
  bool _paging = false;
  String? _cursor;
  String? _mainId;
  bool _historyHasMore = false;
  int _taskPageCount = 1;
  final _held = <String, ChatMessage>{};
  final _read = <String>{};
  final _busyActions = <String>{};
  Timer? _timer;
  StreamSubscription<WsEvent>? _events;

  AssistantApi get _api => ref.read(assistantApiProvider(arg));
  String get _prefix =>
      'assistant-v1:${_digest([arg.userId, arg.workspaceId])}';
  bool get _current =>
      !_disposed &&
      ref.read(authSessionProvider).userId == arg.userId &&
      ref.read(workspaceScopeProvider).currentId == arg.workspaceId;

  @override
  AssistantState build(AssistantScope scope) {
    _disposed = false;
    _timer = Timer.periodic(const Duration(seconds: 5), (_) {
      if (_current && ref.read(appVisibleProvider)) unawaited(refresh());
    });
    _events = ref.read(wsClientProvider).events.listen((event) {
      if (_current &&
          ref.read(appVisibleProvider) &&
          (event.type == '__connected' ||
              event.type.startsWith('assistant.') ||
              (event.sessionId == _mainId && event.type == 'session.status'))) {
        unawaited(refresh());
      }
    });
    ref.listen(appVisibleProvider, (previous, visible) {
      if (visible && previous == false) unawaited(refresh());
    });
    ref.onDispose(() {
      _disposed = true;
      _timer?.cancel();
      unawaited(_events?.cancel());
    });
    Future.microtask(refresh);
    return const AssistantState();
  }

  Map<String, dynamic>? _stored(String suffix) {
    final raw = ref.read(prefsProvider).getString('$_prefix:$suffix');
    if (raw == null) return null;
    // A corrupt pending identity must never silently authorize a new send.
    return jsonDecode(raw) as Map<String, dynamic>;
  }

  Future<void> _save(String suffix, Map<String, dynamic> value) async {
    if (!_current) throw StateError('Assistant scope changed');
    if (!await ref
        .read(prefsProvider)
        .setString('$_prefix:$suffix', jsonEncode(value))) {
      throw StateError('Could not persist the submission identity');
    }
    if (!_current) throw StateError('Assistant scope changed');
  }

  Future<void> _clear(String suffix) async {
    if (!_current) return;
    await ref.read(prefsProvider).remove('$_prefix:$suffix');
  }

  Future<void> refresh() {
    if (!_current) return Future.value();
    if (_paging) {
      _refreshAgain = true;
      return Future.value();
    }
    if (_refreshing != null) {
      _refreshAgain = true;
      return _refreshing!;
    }
    return _refreshing = _refresh().whenComplete(() {
      _refreshing = null;
      if (_refreshAgain && _current) {
        _refreshAgain = false;
        unawaited(refresh());
      }
    });
  }

  Future<void> _refresh() async {
    try {
      var gap = false;
      if (_cursor != null) {
        final page = await _api.events(_cursor!);
        gap = page['state'] == 'snapshot_required';
      }
      var snapshot = await _api.snapshot();
      if (!_current) return;
      if (snapshot.data['state'] == 'not_created') {
        await _api.ensure();
        if (!_current) return;
        snapshot = await _api.snapshot();
      }
      final main = snapshot.session;
      if (main == null || main.kind != 'assistant') {
        throw StateError('Assistant unavailable');
      }
      final latest = await _api.history(main.id);
      if (!_current) return;
      final held = _held.values.toList()..sort(_byId);
      final newest = latest.messages;
      // A gap in the event stream, a different main conversation, or a
      // newest page that no longer touches what is held starts over from the
      // newest turns. Otherwise the older pages already read stay in front,
      // and within the newest page the server's copy is the current one.
      final disjoint =
          held.isNotEmpty &&
          newest.isNotEmpty &&
          held.last.id.compareTo(newest.first.id) < 0;
      final reset = gap || disjoint || (_mainId != null && _mainId != main.id);
      final older = reset || newest.isEmpty
          ? const <ChatMessage>[]
          : held.where((m) => m.id.compareTo(newest.first.id) < 0).toList();
      final transcript = [...older, ...newest]..sort(_byId);
      final taskMap = {for (final task in snapshot.tasks) task.id: task};
      var taskCursor = snapshot.taskCursor;
      for (
        var index = 1;
        index < _taskPageCount && taskCursor != null;
        index++
      ) {
        final next = await _api.snapshot(taskCursor: taskCursor);
        if (!_current || next.session?.id != main.id) return;
        taskMap.addEntries(next.tasks.map((t) => MapEntry(t.id, t)));
        taskCursor = next.taskCursor;
      }
      final pending = _stored('send');
      if (pending != null &&
          transcript.any(
            (m) => m.isUser && m.clientMessageId == pending['client_id'],
          )) {
        await _clear('send');
      }
      if (!_current) return;
      _mainId = main.id;
      _held
        ..clear()
        ..addEntries(transcript.map((m) => MapEntry(m.id, m)));
      final echoes = state.messages.where(
        (m) =>
            m.id.startsWith('tmp-') &&
            !transcript.any((v) => v.clientMessageId == m.clientMessageId),
      );
      final pendingActions = <String, String>{};
      for (final task in taskMap.values) {
        final saved = _stored('control:${task.id}');
        if (saved != null) pendingActions[task.id] = saved['action'] as String;
      }
      // Only the newest page can say whether older turns exist, unless older
      // pages read before are still held.
      if (older.isEmpty) _historyHasMore = latest.hasMore;
      state = AssistantState(
        snapshot: snapshot,
        messages: [...transcript, ...echoes],
        loading: false,
        hasMore: _historyHasMore,
        sending: state.sending,
        sendUncertain: _stored('send') != null && !state.sending,
        sendAccepted:
            state.sendAccepted || (pending != null && _stored('send') == null),
        tasks: taskMap.values.toList(),
        taskCursor: taskCursor,
        actionPending: pendingActions,
      );
      // Advance only after the snapshot and the transcript were rebuilt. A
      // failed read keeps the old cursor, including after a gap.
      _cursor = snapshot.cursor;
    } catch (error) {
      if (_current) {
        // What was shown stays (V2: revocation is not retroactive); only a
        // first load that failed has nothing to show but the error.
        state = AssistantState(
          snapshot: state.snapshot,
          messages: state.messages,
          loading: false,
          error: error,
          hasMore: state.hasMore,
          sending: state.sending,
          sendUncertain: state.sendUncertain,
          sendAccepted: state.sendAccepted,
          tasks: state.tasks,
          taskCursor: state.taskCursor,
          actionPending: state.actionPending,
        );
      }
    }
  }

  /// The turns before the oldest one held, put in front of it.
  Future<void> loadOlder() async {
    if (_paging || !state.hasMore || _mainId == null) return;
    // A refresh can start another as it ends; page only once none runs.
    while (_refreshing != null) {
      await _refreshing;
    }
    if (!_current || _held.isEmpty || _paging) return;
    _paging = true;
    state = state.copyWith(loadingOlder: true);
    try {
      final first = (_held.values.toList()..sort(_byId)).first.id;
      final page = await _api.history(_mainId!, before: first);
      if (!_current) return;
      _held.addEntries(
        page.messages
            .where((m) => m.id.compareTo(first) < 0)
            .map((m) => MapEntry(m.id, m)),
      );
      _historyHasMore = page.hasMore;
      state = state.copyWith(
        messages: [
          ...(_held.values.toList()..sort(_byId)),
          ...state.messages.where((m) => m.id.startsWith('tmp-')),
        ],
        hasMore: page.hasMore,
        loadingOlder: false,
      );
    } catch (_) {
      // Asked for again when the reader comes back to the top.
      if (_current) state = state.copyWith(loadingOlder: false);
    } finally {
      _paging = false;
      if (_current && _refreshAgain) {
        _refreshAgain = false;
        unawaited(refresh());
      }
    }
  }

  Future<void> moreTasks() async {
    final cursor = state.taskCursor;
    if (cursor == null || _paging) return;
    await _refreshing;
    if (!_current) return;
    _paging = true;
    try {
      final page = await _api.snapshot(taskCursor: cursor);
      if (!_current || page.session?.id != _mainId) return;
      final tasks = {
        for (final t in state.tasks) t.id: t,
        for (final t in page.tasks) t.id: t,
      };
      _taskPageCount++;
      state = AssistantState(
        snapshot: state.snapshot,
        messages: state.messages,
        loading: false,
        hasMore: state.hasMore,
        sending: state.sending,
        sendUncertain: state.sendUncertain,
        sendAccepted: state.sendAccepted,
        tasks: tasks.values.toList(),
        taskCursor: page.taskCursor,
        actionPending: state.actionPending,
      );
    } finally {
      _paging = false;
      if (_refreshAgain) {
        _refreshAgain = false;
        unawaited(refresh());
      }
    }
  }

  Future<void> send(String text, List<String> attachments) async {
    if (!_current || state.sending || state.snapshot?.session == null) {
      throw StateError('Assistant unavailable');
    }
    final main = state.snapshot!.session!;
    final fingerprint = _digest([main.id, text, attachments]);
    var saved = _stored('send');
    if (saved != null && saved['fingerprint'] != fingerprint) {
      throw ApiError(
        status: 409,
        code: 'ASSISTANT_SEND_UNCERTAIN',
        message: 'Retry the original pending input',
      );
    }
    final model = ref.read(pickedModelProvider(main.id));
    final config = ref.read(appConfigProvider).valueOrNull;
    final modelId = activeModelId(
      picked: model,
      sessionModel: main.model,
      defaultModel: config?.defaultModel,
    );
    final reasoning = resolveReasoning(
      model: config?.byId(modelId),
      sessionModel: main.model,
      sessionVariant: main.variant,
      pick: ref.read(pickedVariantProvider(reasoningKey(main.id, modelId))),
    ).value;
    final video = ref.read(pickedVideoProvider(main.id));
    saved ??= {
      'client_id': _identity(),
      'fingerprint': fingerprint,
      'choices': {
        if (model != null && model.isNotEmpty) 'model': model,
        if (reasoning != null) 'variant': reasoning.level,
        if (video != null) 'video_model': video.modelId,
        if (video != null) 'video_resolution': video.resolution,
      },
    };
    // Set before the first await; a second tap cannot invent another identity.
    state = state.copyWith(sending: true, sendAccepted: false);
    final id = saved['client_id'] as String;
    try {
      await _save('send', saved);
      if (!_current) return;
      final echo = ChatMessage(
        id: 'tmp-$id',
        sessionId: main.id,
        role: 'user',
        clientMessageId: id,
        createdAt: DateTime.now(),
        parts: [TextPart(id: 'tmp-part-$id', text: text)],
      );
      state = state.copyWith(
        messages: [...state.messages.where((m) => m.id != echo.id), echo],
      );
      final receipt = await _api.send({
        'assistant_session_id': main.id,
        'client_id': id,
        'delivery': 'followup',
        'text': text,
        'attachment_ids': attachments,
        ...asMap(saved['choices']),
      });
      if (receipt['assistant_session_id'] != main.id ||
          receipt['client_id'] != id ||
          receipt['inbox_id'] is! String ||
          receipt['delivery'] != 'followup') {
        throw const FormatException('Unconfirmed assistant receipt');
      }
      if (!_current) return;
      await _clear('send');
      if (!_current) return;
      state = state.copyWith(
        sending: false,
        sendUncertain: false,
        sendAccepted: true,
      );
      unawaited(refresh());
    } catch (error) {
      if (_current) {
        if (_held.values.any((m) => m.isUser && m.clientMessageId == id)) {
          await _clear('send');
          if (_current) {
            state = state.copyWith(
              sending: false,
              sendUncertain: false,
              sendAccepted: true,
            );
          }
          return;
        }
        if (_definitive(error)) {
          await _clear('send');
          if (!_current) rethrow;
          state = state.copyWith(
            messages: state.messages.where((m) => m.id != 'tmp-$id').toList(),
          );
        }
        state = state.copyWith(
          sending: false,
          sendUncertain: !_definitive(error),
        );
        unawaited(refresh());
      }
      rethrow;
    }
  }

  Future<void> control(AssistantTask task, String action) =>
      _command('control:${task.id}', {
        'action': action,
        'expected_revision': task.revision,
        'expected_run': task.run,
      }, (body) => _api.control(task.id, body));

  Future<void> linkExisting(String sessionId, String version) => _command(
    'link:$sessionId',
    {'session_id': sessionId, 'expected_version': version},
    (body) async {
      final receipt = await _api.linkExisting(body);
      if (receipt['execution_session_id'] != sessionId ||
          receipt['state'] != 'linked' ||
          receipt['task_id'] is! String) {
        throw const FormatException('Unconfirmed link receipt');
      }
      return receipt;
    },
  );

  /// Stop following a conversation (web `useAssistantArchive`): no more
  /// results or reports; the conversation itself is unchanged. One key per
  /// (task, inspected revision), so a lost response is retried with the same
  /// key; a task that changed after its card was drawn is read once more.
  Future<void> archive(AssistantTask task) async {
    try {
      await _archive(task.id, task.revision);
    } catch (error) {
      if (!_current || apiErrorOf(error)?.code != 'ASSISTANT_TASK_REVISION') {
        rethrow;
      }
      final fresh = await _api.task(task.id);
      if (!_current) return;
      if (fresh.id != task.id) {
        throw const FormatException('Unexpected task projection');
      }
      await _archive(task.id, fresh.revision);
    }
  }

  Future<void> _archive(String taskId, int revision) => _command(
    'archive:$taskId',
    {'expected_revision': revision},
    (body) async {
      final receipt = await _api.archive(taskId, body);
      if (receipt['task_id'] != taskId || receipt['state'] != 'archived') {
        throw const FormatException('Unconfirmed archive receipt');
      }
      return receipt;
    },
  );

  Future<void> retryReport(AssistantTask task) => _command(
    'report:${task.result['result_id']}',
    {'expected_report_attempt': task.result['report_attempt']},
    (body) => _api.retryReport(task.result['result_id'] as String, body),
  );

  Future<void> _command(
    String suffix,
    Map<String, dynamic> proposed,
    Future<Map<String, dynamic>> Function(Map<String, dynamic>) submit,
  ) async {
    if (!_current || !_busyActions.add(suffix)) return;
    try {
      final old = _stored(suffix);
      if (old != null && old['action'] != proposed['action']) {
        throw ApiError(
          status: 409,
          code: 'ASSISTANT_SEND_UNCERTAIN',
          message: 'Resolve the pending command first',
        );
      }
      final body = old ?? {...proposed, 'idempotency_key': _identity()};
      await _save(suffix, body);
      final receipt = await submit(body);
      if (receipt['command_id'] is! String) {
        throw const FormatException('Unconfirmed command receipt');
      }
      if (_current) await _clear(suffix);
    } catch (error) {
      if (_current &&
          _definitive(error) &&
          apiErrorOf(error)?.code != 'ASSISTANT_SEND_UNCERTAIN') {
        await _clear(suffix);
      }
      rethrow;
    } finally {
      _busyActions.remove(suffix);
      if (_current) {
        // The task list ("我的任务") reads the same committed state.
        ref.invalidate(assistantWatchProvider(arg));
        unawaited(refresh());
      }
    }
  }

  /// Read receipts depend only on the answer actually being seen: an
  /// available answer with a display token, newer than what was last read
  /// (web `AssistantReadBoundary`). A failed receipt waits for a fresh token
  /// from the next snapshot rather than retrying in a loop.
  Future<void> markRead(String messageId) async {
    final snapshot = state.snapshot;
    if (!_current ||
        snapshot == null ||
        !ref.read(appVisibleProvider) ||
        !_held.containsKey(messageId)) {
      return;
    }
    final answer = snapshot.answers
        .where(
          (a) =>
              a['message_id'] == messageId &&
              a['available'] == true &&
              a['display_token'] is String,
        )
        .firstOrNull;
    if (answer == null ||
        (asInt(answer['sequence']) ?? 0) <= snapshot.lastSeen) {
      return;
    }
    final token = answer['display_token'] as String;
    if (!_read.add(token)) return;
    if (_read.length > 100) {
      _read
        ..clear()
        ..add(token);
    }
    try {
      await _api.markRead(answer);
      if (_current) ref.invalidate(assistantOverviewProvider(arg));
    } catch (_) {
      // The next snapshot brings a fresh display token.
    }
  }

  /// Copying just copies: there must be text, from messages that are shown.
  Future<bool> canCopy(List<String> ids, String text) async =>
      _current &&
      text.isNotEmpty &&
      ids.isNotEmpty &&
      ids.every(_held.containsKey);
}

final assistantControllerProvider = NotifierProvider.autoDispose
    .family<AssistantController, AssistantState, AssistantScope>(
      AssistantController.new,
    );
