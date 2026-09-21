import 'dart:async';

import 'package:bossip_mobile/features/teams/api/teams_api.dart';
import 'package:bossip_mobile/features/teams/team_run_screen.dart';
import 'package:bossip_mobile/features/teams/widgets/team_collection.dart';
import 'package:bossip_mobile/features/teams/widgets/team_progress_card.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:bossip_mobile/shared/models/team.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import '../chat/suggestion_fixtures.dart';

const scope = (userId: 'owner', workspaceId: 'workspace');

class _Api extends TeamsApi {
  _Api() : super(Dio(), AuthSession(), WorkspaceScope());
  int seq = 1, controls = 0;
  String state = 'running';
  bool failCollection = false;
  bool details = false;
  Completer<void>? controlGate;
  final offsets = <int>[];

  @override
  Future<TeamSnapshot> snapshot(
    TeamScope scope,
    String runId, {
    CancelToken? cancel,
  }) async => TeamSnapshot.fromJson({
    'id': runId,
    'seq': seq,
    'task_count': 2,
    'completed_task_count': 1,
    'run': {'id': runId, 'title': '验算团队', 'state': state, 'revision': seq},
  });

  @override
  Future<void> control(
    TeamScope scope,
    TeamRun run,
    String action,
    String key,
  ) async {
    controls++;
    expect(action, 'pause');
    expect(run.revision, 1);
    await controlGate?.future;
    state = 'paused';
    seq++;
    throw ApiError(status: 0, code: 'NETWORK', message: 'Response lost');
  }

  @override
  Future<Map<String, dynamic>> read(
    TeamScope scope,
    String path, {
    Map<String, dynamic>? query,
    CancelToken? cancel,
  }) async {
    if (path.endsWith('/events')) return {'last_seq': seq};
    if (details) {
      return {
        'items': [
          if (path.endsWith('/tasks'))
            {
              'id': 'task',
              'title': '独立验算',
              'state': 'succeeded',
              'description': '计算 17+29',
            }
          else if (path.endsWith('/attempts'))
            {
              'id': 'attempt',
              'number': 1,
              'state': 'succeeded',
              'summary': '验算成功',
              'output': {'result': 46},
            },
        ],
        'next_offset': null,
      };
    }
    offsets.add(query?['offset'] as int? ?? 0);
    if (failCollection) throw StateError('Disconnected');
    return {
      'items': [
        {'title': '唯一任务'},
      ],
      'next_offset': null,
    };
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late _Api api;
  late SuggestionFixture fixture;
  setUp(() async {
    api = _Api();
    fixture = await SuggestionFixture.create(
      overrides: [teamsApiProvider.overrideWithValue(api)],
    );
  });
  tearDown(() => fixture.dispose());
  testWidgets(
    'task details can reopen selectable descriptions and structured outputs',
    (tester) async {
      api.details = true;
      await tester.pumpWidget(
        fixture.app(
          TeamRunScreen(
            scope: scope,
            runId: 'run',
            onOpenChat: (_) {},
            renderText: SelectableText.new,
            renderArtifact: (_) => const SizedBox.shrink(),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('任务'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('独立验算'));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(find.text('计算 17+29'), findsOneWidget);
      expect(find.text('验算成功'), findsOneWidget);
      expect(find.textContaining('"result": 46'), findsOneWidget);
      await tester.tap(find.text('独立验算'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('独立验算'));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(find.text('验算成功'), findsOneWidget);
    },
  );
  testWidgets(
    'a lost pause response reconciles committed state and blocks duplicate taps',
    (tester) async {
      api.controlGate = Completer<void>();
      await tester.pumpWidget(
        fixture.app(
          Scaffold(
            body: TeamProgressCard(
              scope: scope,
              runId: 'run',
              onDetails: () {},
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('暂停'));
      await tester.pump();
      await tester.tap(find.text('暂停'), warnIfMissed: false);
      expect(api.controls, 1);
      api.controlGate!.complete();
      await tester.pumpAndSettle();
      expect(find.text('继续'), findsOneWidget);
      expect(find.text('已暂停'), findsOneWidget);
      expect(api.controls, 1);
    },
  );

  testWidgets(
    'collection refresh failures retain the prior page and retry without duplicates',
    (tester) async {
      Widget page(int seq) => fixture.app(
        Scaffold(
          body: TeamCollection(
            scope: scope,
            runId: 'run',
            collection: 'tasks',
            seq: seq,
            itemBuilder: (row) => Text(row['title'] as String),
          ),
        ),
      );
      await tester.pumpWidget(page(1));
      await tester.pumpAndSettle();
      expect(find.text('唯一任务'), findsOneWidget);
      api.failCollection = true;
      await tester.pumpWidget(page(2));
      await tester.pumpAndSettle();
      expect(find.text('唯一任务'), findsOneWidget);
      api.failCollection = false;
      await tester.tap(find.byType(TextButton));
      await tester.pumpAndSettle();
      expect(find.text('唯一任务'), findsOneWidget);
      expect(api.offsets, [0, 0, 0]);
    },
  );
}
