import 'package:bossip_mobile/app/workspace_bootstrap.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

final _scope = StateProvider<({String userId, String workspaceId})?>(
  (ref) => null,
);

class _Ws extends AgentWsClient {
  _Ws() : super(Dio());
  final calls = <String>[];
  @override
  Future<void> connect() async => calls.add('connect');
  @override
  void disconnect() => calls.add('disconnect');
}

void main() {
  test(
    'all authenticated routes connect once per scope and disconnect on logout',
    () async {
      final ws = _Ws();
      final container = ProviderContainer(
        overrides: [
          workspaceEventScopeProvider.overrideWith((ref) => ref.watch(_scope)),
          wsClientProvider.overrideWithValue(ws),
        ],
      );
      addTearDown(container.dispose);
      container.listen(workspaceEventsProvider, (_, _) {});
      expect(ws.calls, isEmpty);
      container.read(_scope.notifier).state = (userId: 'a', workspaceId: 'one');
      await container.pump();
      expect(ws.calls, ['connect']);
      container.read(_scope.notifier).state = (userId: 'a', workspaceId: 'one');
      await container.pump();
      expect(ws.calls, ['connect']);
      container.read(_scope.notifier).state = (userId: 'a', workspaceId: 'two');
      await container.pump();
      container.read(_scope.notifier).state = (userId: 'b', workspaceId: 'two');
      await container.pump();
      container.read(_scope.notifier).state = null;
      await container.pump();
      expect(ws.calls, [
        'connect',
        'disconnect',
        'connect',
        'disconnect',
        'connect',
        'disconnect',
      ]);
    },
  );
}
