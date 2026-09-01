import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/events/bus.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/project.dart';
import '../../../shared/models/session.dart';
import '../../../shared/ws/ws_client.dart';
import '../api/workspace_api.dart';

/// Sessions + projects for the sidebar/drawer, kept fresh via WS events
/// (web `useWorkspaceEvents` invalidates on `session.*` and `__connected`;
/// here title/status patch in place and structural events refetch).
class WorkspaceData {
  const WorkspaceData({this.projects = const [], this.sessions = const []});

  final List<Project> projects;
  final List<Session> sessions;

  Session? sessionById(String id) {
    for (final s in sessions) {
      if (s.id == id) return s;
    }
    return null;
  }

  Project? projectById(String? id) {
    if (id == null) return null;
    for (final p in projects) {
      if (p.id == id) return p;
    }
    return null;
  }
}

class WorkspaceController extends AsyncNotifier<WorkspaceData> {
  StreamSubscription<WsEvent>? _sub;
  StreamSubscription<AppEvent>? _busSub;
  final Map<String, int> _statusGeneration = {};
  final Map<String, int> _terminalStatusGeneration = {};

  @override
  Future<WorkspaceData> build() async {
    unawaited(_sub?.cancel());
    _sub = ref.watch(wsClientProvider).events.listen(_onWsEvent);
    unawaited(_busSub?.cancel());
    // Cross-feature signal: chat emits after first-send session creation.
    _busSub = ref
        .watch(appEventBusProvider)
        .on('workspace.refresh')
        .listen((_) => unawaited(refresh()));
    ref.onDispose(() {
      unawaited(_sub?.cancel());
      unawaited(_busSub?.cancel());
    });
    return _fetch();
  }

  Future<WorkspaceData> _fetch() async {
    final api = ref.read(workspaceApiProvider);
    final results = await Future.wait([api.listProjects(), api.listSessions()]);
    return WorkspaceData(
      projects: results[0] as List<Project>,
      sessions: (results[1] as List<Session>)
        ..sort((a, b) => b.updatedAt.compareTo(a.updatedAt)),
    );
  }

  Future<void> refresh() async {
    state = AsyncData(await _fetch());
  }

  void _onWsEvent(WsEvent event) {
    switch (event.type) {
      case '__connected':
        unawaited(refresh());
      case 'session.title':
        _patchSession(
          event.sessionId,
          (s) => s.copyWith(title: asString(event.data['title']) ?? s.title),
        );
      case 'session.status':
        final incoming = sessionStatusFrom(asString(event.data['status']));
        if (!_acceptStatusEvent(event, incoming)) return;
        _patchSession(event.sessionId, (s) => s.copyWith(status: incoming));
      case 'session.finalizing':
        if (!_acceptStatusEvent(event, SessionStatus.finalizing)) return;
        _patchSession(
          event.sessionId,
          (s) => s.copyWith(status: SessionStatus.finalizing),
        );
      case 'session.error':
        if (!_acceptStatusEvent(event, SessionStatus.error)) return;
        _patchSession(
          event.sessionId,
          (s) => s.copyWith(status: SessionStatus.error),
        );
    }
  }

  bool _acceptStatusEvent(WsEvent event, SessionStatus status) {
    final sessionId = event.sessionId;
    if (sessionId == null) return false;
    final generation = asInt(event.data['generation']);
    final current = _statusGeneration[sessionId];
    if (!acceptsEventGeneration(
      current,
      generation,
      rejectLegacyAfterSeen: true,
    )) {
      return false;
    }
    if (generation != null &&
        _terminalStatusGeneration[sessionId] == generation) {
      final currentStatus = state.valueOrNull?.sessionById(sessionId)?.status;
      if (currentStatus != status) {
        return false;
      }
    }
    if (generation != null) {
      _statusGeneration[sessionId] = generation;
      if (status == SessionStatus.idle || status == SessionStatus.error) {
        _terminalStatusGeneration[sessionId] = generation;
      }
    }
    return true;
  }

  void _patchSession(String? id, Session Function(Session) update) {
    final data = state.valueOrNull;
    if (data == null || id == null) return;
    if (!data.sessions.any((s) => s.id == id)) {
      // Unknown session (created elsewhere) → pull the fresh list.
      unawaited(refresh());
      return;
    }
    final sessions = [
      for (final s in data.sessions)
        if (s.id == id) update(s) else s,
    ];
    state = AsyncData(
      WorkspaceData(projects: data.projects, sessions: sessions),
    );
  }

  /// Create + return a session; the caller navigates to it.
  Future<Session> createSession({
    String? projectId,
    String model = '',
    String agent = 'build',
  }) async {
    final session = await ref
        .read(workspaceApiProvider)
        .createSession(projectId: projectId, model: model, agent: agent);
    await refresh();
    return session;
  }

  Future<void> deleteSession(String id) async {
    await ref.read(workspaceApiProvider).deleteSession(id);
    await refresh();
  }

  Future<void> renameSession(String id, String title) async {
    await ref.read(workspaceApiProvider).renameSession(id, title);
    await refresh();
  }

  Future<void> createProject(String name) async {
    await ref.read(workspaceApiProvider).createProject(name);
    await refresh();
  }

  Future<void> deleteProject(String id) async {
    await ref.read(workspaceApiProvider).deleteProject(id);
    await refresh();
  }
}

final workspaceProvider =
    AsyncNotifierProvider<WorkspaceController, WorkspaceData>(
      WorkspaceController.new,
    );

/// The project new chats land in (web `useWorkspaceUi.selectedProject`).
final selectedProjectProvider = StateProvider<String?>((ref) => null);
