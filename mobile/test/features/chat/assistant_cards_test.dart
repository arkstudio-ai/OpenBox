import 'dart:async';
import 'dart:convert';

import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/utils/task_receipt.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_report.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_requests.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_task_receipts.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'assistant_fixture.dart';

final binding = {
  'assistant_session_id': 'main',
  'workspace_id': scope.workspaceId,
  'request_revision': List.filled(64, 'a').join(),
  'options_hash': List.filled(64, 'b').join(),
};

class _RequestsApi extends TestApi {
  bool assetUnavailable = false;
  @override
  Future<AssistantSnapshot> snapshot({String? taskCursor}) async {
    final value = await super.snapshot(taskCursor: taskCursor);
    if (assetUnavailable) {
      value.tasks.first.data['latest_submission'] = {
        'state': 'canceled',
        'disposition': 'canceled',
        'delivery': 'steer',
        'error': {'code': 'ASSISTANT_ASSET_UNAVAILABLE'},
      };
    }
    return value;
  }

  bool revoked = false;
  Completer<void>? holdNextResult;
  final resultReads = <({int offset, String? version})>[];
  @override
  Future<Map<String, dynamic>> requests(String kind, {String? cursor}) async =>
      {
        'items': replies.any((r) => r['kind'] == kind)
            ? <Map<String, dynamic>>[]
            : [
                {
                  'id': kind,
                  'session_id': 'execution',
                  'assistant': binding,
                  'task_title': 'Campaign plan',
                  'project_name': 'Project Orchard',
                  if (kind == 'permission') ...{
                    'tool': 'shell',
                    'input': {'command': 'test-command'},
                    'always': ['test-command *'],
                  } else
                    'questions': [
                      {
                        'question': 'Which audience?',
                        'options': [
                          {'label': 'Existing customers'},
                        ],
                        'custom': false,
                      },
                    ],
                },
              ],
        'receipts': [
          for (final r in replies.where((r) => r['kind'] == kind))
            {
              'command_id': 'reply-${r['id']}',
              'state': 'accepted',
              'session_id': 'execution',
            },
        ],
      };
  @override
  Future<Map<String, dynamic>> result(
    String id, {
    int offset = 0,
    String? version,
  }) async {
    resultReads.add((offset: offset, version: version));
    final denied = revoked;
    final hold = holdNextResult;
    holdNextResult = null;
    await hold?.future;
    if (denied) throw StateError('Source permission revoked');
    return {
      'offset': offset,
      'next_offset': offset == 0 ? 8000 : null,
      'source_version': 'version-one',
      'sources': [
        {
          'text': offset == 0
              ? '```text\nsource body\n```'
              : 'Second verified page',
        },
      ],
    };
  }
}

class _LegacyApi extends ChatApi {
  _LegacyApi() : super(Dio());
  @override
  Future<List<QuestionRequest>> listQuestions() async => [];
  @override
  Future<List<PermissionRequest>> listPermissions() async => [];
  @override
  Future<void> replyQuestion(String id, List<List<String>> answers) async =>
      throw StateError('Assistant question used unversioned reply');
  @override
  Future<void> replyPermission(String id, String action) async =>
      throw StateError('Assistant permission used unversioned reply');
  @override
  Future<QuestionRequest> saveQuestionDraft(
    String id,
    List<QuestionDraftAnswer> draft,
    int revision,
  ) async => QuestionRequest(
    id: id,
    sessionId: 'execution',
    questions: const [QuestionItem(question: 'Which audience?')],
    draft: draft,
    draftRevision: revision + 1,
  );
}

ToolPart receiptPart({
  String tool = 'tasks.submit',
  String status = 'completed',
  String? output,
}) =>
    MessagePart.fromJson({
          'type': 'tool',
          'id': 'receipt-part',
          'tool': tool,
          'status': status,
          'output':
              output ??
              jsonEncode({
                'task_id': 'task',
                'command_id': 'original-command',
                'state': 'accepted',
              }),
        })
        as ToolPart;

Future<Fixture> _mount(
  WidgetTester tester,
  Widget child, {
  _RequestsApi? server,
}) async {
  final f = (await tester.runAsync(() async {
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final bundle = await I18nBundle.load();
    final fixture = Fixture(
      prefs,
      server: server,
      legacy: _LegacyApi(),
      i18n: I18nState(language: 'en-US', bundle: bundle),
    );
    await fixture.ready();
    return fixture;
  }))!;
  addTearDown(f.close);
  await tester.pumpWidget(
    UncontrolledProviderScope(
      container: f.container,
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: Scaffold(body: child),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return f;
}

Future<void> _unmount(WidgetTester tester, Fixture f) async {
  await tester.pumpWidget(const SizedBox.shrink());
  f.close();
  await tester.pump(const Duration(milliseconds: 1));
}

void main() {
  testWidgets('unavailable attachment is not displayed as accepted steering', (
    tester,
  ) async {
    final f = await _mount(
      tester,
      SingleChildScrollView(
        child: AssistantTaskReceipts(
          scope: scope,
          parts: [receiptPart(tool: 'assets.attach')],
          onAction: (action) => action(),
        ),
      ),
      server: _RequestsApi()..assetUnavailable = true,
    );
    expect(
      find.text(
        'An attachment is no longer available, so this input was not executed. Select the file again and send a new request.',
      ),
      findsOneWidget,
    );
    expect(
      find.text(
        'Modification accepted; waiting to be included in the specified run.',
      ),
      findsNothing,
    );
    await _unmount(tester, f);
  });
  test('only a completed canonical write receipt can expose task actions', () {
    expect(taskReceipt(receiptPart(tool: 'schedules.run')), (
      taskId: 'task',
      commandId: 'original-command',
    ));
    expect(taskReceipt(receiptPart(tool: 'assets.attach')), (
      taskId: 'task',
      commandId: 'original-command',
    ));
    expect(taskReceipt(receiptPart()), (
      taskId: 'task',
      commandId: 'original-command',
    ));
    for (final part in [
      receiptPart(tool: 'mcp.tasks.submit'),
      receiptPart(tool: 'assets.list'),
      receiptPart(tool: 'schedules.create'),
      receiptPart(status: 'running'),
      receiptPart(status: 'error'),
      receiptPart(output: 'prose task_id=task'),
      receiptPart(
        output: '{"task_id":"task","command_id":"cmd","state":"failed"}',
      ),
      const TextPart(
        id: 'text',
        text: '{"task_id":"task","command_id":"cmd","state":"accepted"}',
      ),
    ]) {
      expect(taskReceipt(part), isNull);
    }
  });

  testWidgets(
    'task receipt retains its identity while the live task advances; resume waits for pause',
    (tester) async {
      final f = await _mount(
        tester,
        SingleChildScrollView(
          child: AssistantTaskReceipts(
            scope: scope,
            parts: [receiptPart()],
            onAction: (action) => action(),
          ),
        ),
      );
      expect(find.text('Original task'), findsOneWidget);
      await tester.tap(find.text('Command receipt'));
      await tester.pumpAndSettle();
      expect(find.text('original-command'), findsOneWidget);
      f.api.taskDesired = 'paused';
      f.api.taskObserved = 'pausing';
      f.api.revision = 3;
      await tester.runAsync(f.controller.refresh);
      await tester.pumpAndSettle();
      final resume = find.widgetWithText(TextButton, 'Resume');
      expect(tester.widget<TextButton>(resume).onPressed, isNull);
      expect(find.text('original-command'), findsOneWidget);
      f.api.taskObserved = 'paused';
      await tester.runAsync(f.controller.refresh);
      await tester.pumpAndSettle();
      await tester.tap(resume);
      await tester.pumpAndSettle();
      expect(f.api.controls.single['expected_revision'], 3);
      expect(f.api.controls.single['action'], 'resume');
      expect(tester.takeException(), isNull);
      await _unmount(tester, f);
    },
  );

  for (final kind in ['question', 'permission']) {
    testWidgets(
      '$kind card shows its target and submits the versioned reply from the main entry',
      (tester) async {
        final api = _RequestsApi();
        final f = await _mount(
          tester,
          SingleChildScrollView(
            child: AssistantRequests(scope: scope, kind: kind),
          ),
          server: api,
        );
        expect(find.text('Campaign plan · Project Orchard'), findsOneWidget);
        if (kind == 'question') {
          await tester.tap(find.text('Existing customers'));
          await tester.pumpAndSettle();
          await tester.tap(find.widgetWithText(FilledButton, 'Confirm'));
        } else {
          expect(find.textContaining('test-command *'), findsOneWidget);
          await tester.tap(find.widgetWithText(FilledButton, 'Allow'));
        }
        await tester.pumpAndSettle();
        expect(
          api.replies.single['expected_request_revision'],
          binding['request_revision'],
        );
        expect(api.replies.single['source_ref'], {'kind': 'card'});
        if (kind == 'question') {
          expect(api.replies.single['answers'], [
            ['Existing customers'],
          ]);
        } else {
          expect(api.replies.single['action'], 'once');
        }
        expect(
          find.text(
            kind == 'question'
                ? 'Recent question replies'
                : 'Recent permission replies',
          ),
          findsOneWidget,
        );
        expect(find.text('Campaign plan · Project Orchard'), findsNothing);
        expect(tester.takeException(), isNull);
        await _unmount(tester, f);
      },
    );
  }

  testWidgets('a delayed copy proof cannot override a newer source denial', (
    tester,
  ) async {
    final api = _RequestsApi();
    final writes = <Object?>[];
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
      SystemChannels.platform,
      (call) async {
        if (call.method == 'Clipboard.setData') writes.add(call.arguments);
        return null;
      },
    );
    addTearDown(
      () => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        null,
      ),
    );
    final f = await _mount(
      tester,
      const AssistantReport(scope: scope, resultId: 'result'),
      server: api,
    );
    final hold = Completer<void>();
    api.holdNextResult = hold;
    await tester.tap(find.byIcon(Icons.copy_outlined));
    await tester.pump();
    api.revoked = true;
    await tester.tap(find.text('Read more'));
    await tester.pumpAndSettle();
    expect(find.byIcon(Icons.copy_outlined), findsNothing);
    hold.complete();
    await tester.pumpAndSettle();
    expect(writes, isEmpty);
    expect(api.resultReads, hasLength(3));
    expect(tester.takeException(), isNull);
    await _unmount(tester, f);
  });

  testWidgets(
    'report pagination rechecks loaded source pages and revoked content cannot be copied',
    (tester) async {
      final api = _RequestsApi();
      final writes = <Object?>[];
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        (call) async {
          if (call.method == 'Clipboard.setData') writes.add(call.arguments);
          return null;
        },
      );
      addTearDown(
        () => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
          SystemChannels.platform,
          null,
        ),
      );
      final f = await _mount(
        tester,
        const AssistantReport(scope: scope, resultId: 'result'),
        server: api,
      );
      await tester.tap(find.text('Read more'));
      await tester.pumpAndSettle();
      expect(api.resultReads, [
        (offset: 0, version: null),
        (offset: 0, version: 'version-one'),
        (offset: 8000, version: 'version-one'),
      ]);
      expect(find.text('Second verified page'), findsOneWidget);
      api.revoked = true;
      await tester.tap(find.byIcon(Icons.copy_outlined));
      await tester.pumpAndSettle();
      expect(writes, isEmpty);
      expect(find.text('Second verified page'), findsNothing);
      expect(tester.takeException(), isNull);
      await _unmount(tester, f);
    },
  );
}
