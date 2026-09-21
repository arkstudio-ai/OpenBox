import 'dart:async';

import 'package:bossip_mobile/features/teams/api/teams_api.dart';
import 'package:bossip_mobile/features/teams/state/team_providers.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:bossip_mobile/shared/events/app_lifecycle.dart';
import 'package:bossip_mobile/shared/models/team.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import '../chat/suggestion_fixtures.dart';

const scope = (userId: 'owner', workspaceId: 'workspace');
const key = (scope: scope, runId: 'run');

class _Api extends TeamsApi {
  _Api() : super(Dio(), AuthSession(), WorkspaceScope());
  int seq = 1, snapshots = 0, events = 0, activeReads = 0, maxActive = 0;
  Completer<void>? gate;
  Completer<void>? initialGate;
  bool fail = false;
  bool invalidWatermark = false;
  @override
  Future<TeamSnapshot> snapshot(
    TeamScope scope,
    String runId, {
    CancelToken? cancel,
  }) async {
    snapshots++;
    final capturedSeq = seq;
    if (snapshots == 1) await initialGate?.future;
    return TeamSnapshot.fromJson({
      'id': runId,
      'seq': capturedSeq,
      'run': {'id': runId, 'state': 'running', 'revision': capturedSeq},
    });
  }

  @override
  Future<Map<String, dynamic>> read(
    TeamScope scope,
    String path, {
    Map<String, dynamic>? query,
    CancelToken? cancel,
  }) async {
    events++;
    activeReads++;
    if (activeReads > maxActive) maxActive = activeReads;
    try {
      await gate?.future;
      if (invalidWatermark) {
        throw ApiError(
          status: 409,
          code: 'INVALID_EVENT_WATERMARK',
          message: 'Reload snapshot',
        );
      }
      if (fail) throw StateError('network disconnected');
      return {'last_seq': seq};
    } finally {
      activeReads--;
    }
  }
}

void main() {
  testWidgets(
    'an initial in-flight snapshot cannot lose a newer completion hint',
    (tester) async {
      final api = _Api()..initialGate = Completer<void>();
      final ws = SuggestionWs();
      final container = ProviderContainer(
        overrides: [
          teamsApiProvider.overrideWithValue(api),
          wsClientProvider.overrideWithValue(ws),
        ],
      );
      final sub = container.listen(teamRunProvider(key), (_, _) {});
      await tester.pump();
      api.seq = 3;
      ws.frames.add(
        const WsEvent('team.run.updated', {'teamRunId': 'run', 'seq': 3}),
      );
      await tester.pump();
      api.initialGate!.complete();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 1));
      expect(container.read(teamRunProvider(key)).valueOrNull?.seq, 3);
      expect(api.events, 1);
      sub.close();
      container.dispose();
      await ws.close();
      await tester.pump(const Duration(milliseconds: 1));
    },
  );
  testWidgets(
    'coalesces team hints, catches up after reconnect and retains data on errors',
    (tester) async {
      final api = _Api(), ws = SuggestionWs();
      final container = ProviderContainer(
        overrides: [
          teamsApiProvider.overrideWithValue(api),
          wsClientProvider.overrideWithValue(ws),
        ],
      );
      final sub = container.listen(teamRunProvider(key), (_, _) {});
      await tester.pump();
      expect(container.read(teamRunProvider(key)).valueOrNull?.seq, 1);
      api.gate = Completer<void>();
      api.seq = 8;
      for (var i = 0; i < 10; i++) {
        ws.frames.add(
          const WsEvent('team.run.updated', {'teamRunId': 'run', 'seq': 8}),
        );
      }
      await tester.pump();
      expect(api.events, 1);
      api.gate!.complete();
      await tester.pump();
      expect(api.events, 2);
      expect(api.maxActive, 1);
      expect(container.read(teamRunProvider(key)).valueOrNull?.seq, 8);
      api.fail = true;
      ws.frames.add(const WsEvent('__connected', {}));
      await tester.pump();
      expect(container.read(teamRunProvider(key)).hasError, isTrue);
      expect(container.read(teamRunProvider(key)).valueOrNull?.seq, 8);
      api.fail = false;
      api.seq = 9;
      ws.frames.add(const WsEvent('__connected', {}));
      await tester.pump();
      expect(container.read(teamRunProvider(key)).valueOrNull?.seq, 9);
      expect(container.read(teamRunProvider(key)).hasError, isFalse);
      api.invalidWatermark = true;
      api.seq = 2;
      ws.frames.add(const WsEvent('__connected', {}));
      await tester.pump();
      expect(container.read(teamRunProvider(key)).valueOrNull?.seq, 2);
      expect(container.read(teamRunProvider(key)).hasError, isFalse);
      sub.close();
      container.dispose();
      await ws.close();
      await tester.pump(const Duration(milliseconds: 1));
    },
  );
  testWidgets(
    'background pauses catch-up; foreground reconciles without model dispatch',
    (tester) async {
      final api = _Api(), ws = SuggestionWs();
      final container = ProviderContainer(
        overrides: [
          teamsApiProvider.overrideWithValue(api),
          wsClientProvider.overrideWithValue(ws),
        ],
      );
      final sub = container.listen(teamRunProvider(key), (_, _) {});
      await tester.pump();
      container.read(appVisibleProvider.notifier).state = false;
      api.seq = 5;
      await tester.pump(const Duration(seconds: 20));
      expect(api.events, 0);
      container.read(appVisibleProvider.notifier).state = true;
      await tester.pump();
      expect(container.read(teamRunProvider(key)).valueOrNull?.seq, 5);
      sub.close();
      container.dispose();
      await ws.close();
      await tester.pump(const Duration(milliseconds: 1));
    },
  );
}
