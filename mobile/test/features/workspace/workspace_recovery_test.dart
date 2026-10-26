import 'dart:async';

import 'package:bossip_mobile/features/workspace/api/workspace_api.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/features/workspace/state/workspace_store.dart';
import 'package:bossip_mobile/shared/models/project.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _Active extends ActiveWorkspaceController {
  @override
  Future<ActiveWorkspaceState> build() async =>
      const ActiveWorkspaceState(currentId: 'workspace');
}

class _Ws extends AgentWsClient {
  _Ws() : super(Dio());
  final frames = StreamController<WsEvent>.broadcast(sync: true);
  Future<void> closeFrames() => frames.close();
  @override
  Stream<WsEvent> get events => frames.stream;
}

class _Api extends WorkspaceApi {
  _Api() : super(Dio());
  int reads = 0;
  @override
  Future<List<Project>> listProjects() async => [];
  @override
  Future<List<Session>> listSessions() async {
    reads++;
    return [
      Session.fromJson({'id': 'current', 'title': 'Current', 'status': 'idle'}),
    ];
  }
}

void main() {
  test(
    'reconnect snapshots do not refetch unknown historical chats; live statuses still apply',
    () async {
      final ws = _Ws();
      final api = _Api();
      final container = ProviderContainer(
        overrides: [
          activeWorkspaceProvider.overrideWith(_Active.new),
          workspaceApiProvider.overrideWithValue(api),
          wsClientProvider.overrideWithValue(ws),
        ],
      );
      addTearDown(container.dispose);
    addTearDown(ws.closeFrames);
      await container.read(activeWorkspaceProvider.future);
      await container.read(workspaceProvider.future);
      expect(api.reads, 1);
      ws.frames.add(const WsEvent('__connected', {}));
      await container.pump();
      expect(api.reads, 2);
      for (var i = 0; i < 465; i++) {
        ws.frames.add(
          WsEvent('session.status', {
            'sessionId': 'old-$i',
            'status': 'idle',
            'recovery': true,
          }),
        );
        await container.pump();
      }
      expect(api.reads, 2);
      ws.frames.add(
        const WsEvent('session.status', {
          'sessionId': 'current',
          'status': 'busy',
          'generation': 4,
        }),
      );
      expect(
        container.read(workspaceProvider).value!.sessionById('current')!.status,
        SessionStatus.busy,
      );
      expect(api.reads, 2);
      ws.frames.add(
        const WsEvent('session.status', {
          'sessionId': 'new-chat',
          'status': 'idle',
        }),
      );
      await container.pump();
      expect(api.reads, 3);
    },
  );
}
