import 'dart:convert';

import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/assistant_screen.dart';
import 'package:bossip_mobile/features/chat/state/assistant_watch.dart';
import 'package:bossip_mobile/features/chat/utils/assistant_activity.dart';
import 'package:bossip_mobile/features/chat/utils/memory_receipt.dart';
import 'package:bossip_mobile/features/chat/utils/task_status.dart';
import 'package:bossip_mobile/features/chat/utils/turn_view.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_requests.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_task_receipts.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_tasks.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_welcome.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/widgets/toast.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'assistant_fixture.dart';

ToolPart _tool(String tool, {String status = 'completed', Object? output}) =>
    MessagePart.fromJson({
          'type': 'tool',
          'id': 'part-$tool-$status',
          'tool': tool,
          'status': status,
          'output': ?output,
        })
        as ToolPart;

ChatMessage _user(
  String id, {
  String text = 'Please make the home page dark',
  String? origin,
  Map<String, dynamic>? ref,
  bool synthetic = false,
}) => ChatMessage.fromJson({
  'id': id,
  'session_id': 'main',
  'role': 'user',
  'created_at': DateTime.now().toUtc().toIso8601String(),
  'parts': [
    {
      'id': '$id-text',
      'type': 'text',
      'text': text,
      'synthetic': synthetic,
      'origin': ?origin,
      'origin_ref': ?ref,
    },
  ],
});

ChatMessage _reply(
  String id, {
  String? text,
  List<Map<String, dynamic>> parts = const [],
  String? finish = 'stop',
}) => ChatMessage.fromJson({
  'id': id,
  'session_id': 'main',
  'role': 'assistant',
  'finish': finish,
  'model': 'test/model',
  'tokens': {'input': 12345, 'output': 678},
  'created_at': DateTime.now().toUtc().toIso8601String(),
  'parts': [
    ...parts,
    if (text != null) {'id': '$id-text', 'type': 'text', 'text': text},
  ],
});

Map<String, dynamic> _watchItem(
  String id, {
  String title = 'Task',
  String sessionStatus = 'idle',
  String desired = 'running',
  String observed = 'idle',
  int pending = 0,
  String? outcome,
  String summary = '',
}) => {
  'task_id': id,
  'title': title,
  'project': {'id': 'project', 'name': 'Snake game'},
  'session_id': 'session-$id',
  'session_status': sessionStatus,
  'desired_state': desired,
  'observed_state': observed,
  'revision': 1,
  'updated_at': DateTime.now().toUtc().toIso8601String(),
  'pending_questions': pending,
  if (outcome != null)
    'latest_result': {
      'result_id': 'result-$id',
      'outcome': outcome,
      'delivery_state': 'processed',
      'created_at': DateTime.now().toUtc().toIso8601String(),
      'summary': summary,
    },
};

/// A server whose tasks and results are whatever a test needs.
class _Api extends TestApi {
  final tasks = <String, Map<String, dynamic>>{};
  final sessionItems = <Map<String, dynamic>>[];
  int archiveConflicts = 0;
  int revisionAfterConflict = 5;

  @override
  Future<AssistantTask> task(String id) async {
    final custom = tasks[id];
    if (custom == null) return super.task(id);
    return AssistantTask(custom);
  }

  @override
  Future<Map<String, dynamic>> archive(
    String taskId,
    Map<String, dynamic> body,
  ) async {
    if (archiveConflicts > 0) {
      archiveConflicts--;
      archives.add({'task_id': taskId, ...body});
      revision = revisionAfterConflict;
      throw ApiError(
        status: 409,
        code: 'ASSISTANT_TASK_REVISION',
        message: 'Task changed',
      );
    }
    return super.archive(taskId, body);
  }

  @override
  Future<Map<String, dynamic>> sessions({String? cursor}) async => {
    'items': sessionItems,
    'next_cursor': null,
  };

  @override
  Future<Map<String, dynamic>> result(
    String id, {
    int offset = 0,
    String? version,
  }) async => {
    'offset': offset,
    'next_offset': null,
    'source_version': 'v1',
    'sources': [
      {'text': 'The full written result.'},
    ],
  };
}

Map<String, dynamic> _taskView(
  String id, {
  String title = 'Task',
  String desired = 'running',
  String observed = 'idle',
}) => {
  'task': {
    'id': id,
    'title': title,
    'execution_session_id': 'session-$id',
    'desired_state': desired,
    'observed_state': observed,
    'control_revision': 1,
    'intent_revision': 1,
    'updated_at': DateTime.now().toUtc().toIso8601String(),
  },
  'execution_session': {'id': 'session-$id', 'status': 'idle'},
  'latest_result': null,
};

Future<Fixture> _mount(
  WidgetTester tester,
  Widget child, {
  TestApi? server,
  Size size = const Size(390, 844),
  bool settle = true,
  bool scaffold = true,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final f = (await tester.runAsync(() async {
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final bundle = await I18nBundle.load();
    final fixture = Fixture(
      prefs,
      server: server,
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
        home: scaffold ? Scaffold(body: child) : child,
      ),
    ),
  );
  if (settle) {
    await tester.pumpAndSettle();
  } else {
    await tester.pump();
    await tester.pump();
  }
  return f;
}

Future<void> _unmount(WidgetTester tester, Fixture f) async {
  await tester.pumpWidget(const SizedBox.shrink());
  f.close();
  await tester.pump(const Duration(milliseconds: 1));
}

List<String> _toasts(Fixture f) =>
    f.container.read(toastProvider).map((toast) => toast.text).toList();

class _NamedUser extends AuthController {
  @override
  AuthState build() => const AuthState(
    user: AuthUser(id: 'owner', username: '小王'),
    isLoading: false,
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('task status in plain words', () {
    final cases = <(Map<String, Object?>, TaskStatus)>[
      ({'pending': 1, 'session': 'busy'}, TaskStatus.waiting),
      ({'session': 'waiting_input'}, TaskStatus.waiting),
      ({'session': 'busy', 'observed': 'idle'}, TaskStatus.running),
      ({'observed': 'running'}, TaskStatus.running),
      ({'session': 'queued'}, TaskStatus.queued),
      (
        {'desired': 'paused', 'observed': 'running', 'session': 'idle'},
        TaskStatus.paused,
      ),
      ({'desired': 'canceled', 'observed': 'running'}, TaskStatus.stopped),
      ({'observed': 'effect_unknown'}, TaskStatus.failed),
      ({'session': 'error'}, TaskStatus.failed),
      ({'observed': 'completed'}, TaskStatus.done),
      ({'observed': 'completed', 'outcome': 'error'}, TaskStatus.failed),
      ({'observed': 'idle', 'outcome': 'succeeded'}, TaskStatus.done),
      ({'observed': 'idle', 'outcome': 'aborted'}, TaskStatus.stopped),
      ({'observed': 'idle'}, TaskStatus.idle),
    ];
    for (final (input, expected) in cases) {
      test('$input reads as ${expected.name}', () {
        expect(
          taskStatus(
            sessionStatus: input['session'] as String?,
            observedState: input['observed'] as String?,
            desiredState: input['desired'] as String?,
            pendingQuestions: (input['pending'] as int?) ?? 0,
            outcome: input['outcome'] as String?,
          ),
          expected,
        );
      });
    }

    test('only waiting, running, queued and paused work is unfinished', () {
      expect(
        [
          TaskStatus.waiting,
          TaskStatus.running,
          TaskStatus.queued,
          TaskStatus.paused,
        ].every(isActiveTask),
        isTrue,
      );
      expect(
        [
          TaskStatus.done,
          TaskStatus.failed,
          TaskStatus.stopped,
          TaskStatus.idle,
        ].any(isActiveTask),
        isFalse,
      );
    });

    test('says how long ago within a week, then a short date', () {
      final now = DateTime(2026, 10, 7, 12);
      expect(
        sinceLabel(now.subtract(const Duration(hours: 2)), 'en-US', now: now),
        '2 hours ago',
      );
      expect(
        sinceLabel(now.subtract(const Duration(hours: 2)), 'zh-CN', now: now),
        '2小时前',
      );
      expect(sinceLabel(DateTime(2026, 3, 4), 'en-US', now: now), 'Mar 4');
      expect(sinceLabel(DateTime(2020, 3, 4), 'zh-CN', now: now), '2020年3月4日');
      expect(sinceLabel(null, 'en-US'), '');
    });
  });

  group('what the assistant is doing, in words', () {
    for (final (tool, expected) in const [
      ('tasks.submit', 'delegating'),
      ('tasks.pause', 'updatingTask'),
      ('results.read', 'checkingWork'),
      ('memory.remember', 'remembering'),
      ('memory.search', 'recalling'),
      ('knowledge.read', 'reading'),
      ('requests.list', 'checkingRequests'),
      ('briefing.configure', 'scheduling'),
      ('projects.brief.update', 'updatingBrief'),
      ('assets.attach', 'handlingFiles'),
      ('status.credits', 'checkingStatus'),
      ('something.new', 'working'),
    ]) {
      test('$tool reads as $expected', () {
        expect(assistantActivity([_tool(tool, status: 'running')]), expected);
      });
    }

    test('names the call in flight over the last one, and thinks first', () {
      expect(
        assistantActivity([
          _tool('tasks.list', status: 'running'),
          _tool('memory.search'),
        ]),
        'checkingWork',
      );
      expect(
        assistantActivity([_tool('tasks.list'), _tool('memory.search')]),
        'recalling',
      );
      expect(assistantActivity([]), 'thinking');
    });
  });

  test('a task result or the daily briefing starts an answer of its own', () {
    final rows = buildChatRows([
      _user('m01'),
      _reply('m02', text: 'On it.'),
      _user('m03', origin: 'task_result', synthetic: true),
      _reply('m04', text: 'The page is dark now.'),
      _user(
        'm05',
        origin: 'system_recovery',
        ref: {'entrypoint': 'daily_briefing'},
        synthetic: true,
      ),
      _reply('m06', text: 'Good morning, here is your day.'),
      // Other hidden inputs keep the answer in the same turn.
      _user('m07', origin: 'system_recovery', synthetic: true),
      _reply('m08', text: 'And one more thing.'),
      _user('m09'),
      _reply('m10', text: 'Sure.'),
    ]);
    expect(rows.map((row) => row.runtimeType.toString()), [
      'UserRowData',
      'AssistantTurnData',
      'AssistantTurnData',
      'AssistantTurnData',
      'UserRowData',
      'AssistantTurnData',
    ]);
    final turns = rows.whereType<AssistantTurnData>().toList();
    expect(turns.map((turn) => turn.origin), [
      null,
      TurnOrigin.report,
      TurnOrigin.briefing,
      null,
    ]);
    expect(turns[2].messages.map((m) => m.id), ['m06', 'm08']);
  });

  test('only a completed memory tool result becomes a chip', () {
    final remembered = memoryReceipt(
      _tool(
        'memory.remember',
        output: jsonEncode({
          'state': 'remembered',
          'memory_id': 'mem-1',
          'summary': 'Prefers dark pages',
          'revision': 2,
        }),
      ),
    );
    expect(remembered?.kind, MemoryReceiptKind.remembered);
    expect(remembered?.summary, 'Prefers dark pages');
    expect(remembered?.revision, 2);
    expect(
      memoryReceipt(
        _tool(
          'memory.remember',
          status: 'running',
          output: jsonEncode({'state': 'remembered'}),
        ),
      ),
      isNull,
    );
    expect(
      memoryReceipt(_tool('memory.remember', output: 'remembered')),
      isNull,
    );
    expect(
      memoryReceipt(
        _tool('memory.forget', output: jsonEncode({'state': 'forgotten'})),
      )?.kind,
      MemoryReceiptKind.forgotten,
    );
  });

  testWidgets('the welcome greets by name and time and offers six ideas', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final bundle = (await tester.runAsync(I18nBundle.load))!;
    String? picked;
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(
            () =>
                FixedI18n(I18nState(language: 'zh-CN', bundle: bundle), prefs),
          ),
          authProvider.overrideWith(_NamedUser.new),
        ],
        child: MaterialApp(
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          home: Scaffold(
            body: AssistantWelcome(
              onPick: (prompt) => picked = prompt,
              clock: () => DateTime(2026, 10, 7, 4),
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    // Before dawn is still the evening.
    expect(find.text('晚上好，小王'), findsOneWidget);
    expect(find.textContaining('我是你的个人助理'), findsOneWidget);
    expect(
      find.byWidgetPredicate(
        (widget) =>
            widget.key is ValueKey<String> &&
            (widget.key! as ValueKey<String>).value.startsWith(
              'assistant-idea-',
            ),
      ),
      findsNWidgets(6),
    );
    await tester.ensureVisible(find.text('每天一份简报'));
    await tester.tap(find.text('每天一份简报'));
    expect(picked, '每天早上 8:30 给我发一份简报。');
    expect(welcomeTimeOfDay(DateTime(2026, 1, 1, 5)), 'morning');
    expect(welcomeTimeOfDay(DateTime(2026, 1, 1, 12)), 'afternoon');
    expect(welcomeTimeOfDay(DateTime(2026, 1, 1, 18)), 'evening');
  });

  testWidgets(
    'an empty conversation shows the welcome; an idea fills the composer without sending',
    (tester) async {
      final api = _Api()
        ..newest = []
        ..stored.clear();
      final f = await _mount(
        tester,
        const AssistantScreen(scope: scope),
        server: api,
      );
      expect(
        find.byWidgetPredicate(
          (widget) =>
              widget is Text &&
              RegExp(
                r'^Good (morning|afternoon|evening)$',
              ).hasMatch(widget.data ?? ''),
        ),
        findsOneWidget,
      );
      // The composer stays plain: the assistant's own words, no model pill.
      expect(find.text('What can I help with? Just say it'), findsOneWidget);
      expect(find.text('test/model'), findsNothing);
      await tester.ensureVisible(find.text('A daily briefing'));
      await tester.tap(find.text('A daily briefing'));
      await tester.pumpAndSettle();
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'Send me a briefing every morning at 8:30.',
      );
      expect(api.sends, isEmpty);
      expect(tester.takeException(), isNull);
      await _unmount(tester, f);
    },
  );

  testWidgets(
    'the assistant answers as a persona with task cards and memory chips, never ids',
    (tester) async {
      final api = _Api();
      api.stored
        ..clear()
        ..addAll({
          'm01': _user('m01'),
          'm02': _reply(
            'm02',
            text: 'Done — I handed it over.',
            parts: [
              {
                'id': 'submit-part',
                'type': 'tool',
                'tool': 'tasks.submit',
                'status': 'completed',
                'output': jsonEncode({
                  'task_id': 'task',
                  'command_id': 'cmd-secret',
                  'state': 'accepted',
                }),
              },
              {
                'id': 'memory-part',
                'type': 'tool',
                'tool': 'memory.remember',
                'status': 'completed',
                'output': jsonEncode({
                  'state': 'remembered',
                  'memory_id': 'mem-secret',
                  'summary': 'Prefers dark pages',
                  'revision': 2,
                }),
              },
              {
                'id': 'step-part',
                'type': 'step-finish',
                'input_tokens': 12345,
                'output_tokens': 678,
              },
            ],
          ),
          'm03': _user('m03', origin: 'task_result', synthetic: true),
          'm04': _reply('m04', text: 'The page is dark now.'),
        });
      api.newest = ['m01', 'm02', 'm03', 'm04'];
      final f = await _mount(
        tester,
        const AssistantScreen(scope: scope),
        server: api,
        size: const Size(390, 1600),
      );
      expect(find.text('Assistant'), findsNWidgets(2));
      expect(find.text('Task update'), findsOneWidget);
      expect(find.text('Done — I handed it over.'), findsOneWidget);
      expect(find.text('The page is dark now.'), findsOneWidget);
      expect(find.text('Original task'), findsOneWidget);
      expect(find.text('In progress'), findsOneWidget);
      expect(find.text('Remembered: Prefers dark pages'), findsOneWidget);
      expect(find.byTooltip('Copy'), findsNWidgets(2));
      expect(find.textContaining('Today '), findsNWidgets(2));
      for (final hidden in [
        'cmd-secret',
        'mem-secret',
        'm02',
        'run-1',
        'report-inbox',
        'test/model',
        '12345',
        '12.3k',
        'Final answer',
      ]) {
        expect(find.textContaining(hidden), findsNothing, reason: hidden);
      }
      await tester.tap(find.text('Undo'));
      await tester.pumpAndSettle();
      expect(api.forgotten.single, {
        'memory': 'mem-secret',
        'revision': 2,
        'request': 'assistant-undo:memory-part',
      });
      expect(find.text('Undone'), findsOneWidget);
      await tester.tap(find.byTooltip('Good response').last);
      await tester.pumpAndSettle();
      expect(api.reactions.single, {
        'session': 'main',
        'message': 'm04',
        'reaction': 'up',
      });
      expect(tester.takeException(), isNull);
      await _unmount(tester, f);
    },
  );

  testWidgets('while it works the assistant says what it is doing', (
    tester,
  ) async {
    final api = _Api()..sessionStatus = 'busy';
    api.stored
      ..clear()
      ..addAll({
        'm01': _user('m01'),
        'm02': _reply(
          'm02',
          finish: null,
          parts: [
            {
              'id': 'list-part',
              'type': 'tool',
              'tool': 'tasks.list',
              'status': 'running',
            },
          ],
        ),
      });
    api.newest = ['m01', 'm02'];
    final f = await _mount(
      tester,
      const AssistantScreen(scope: scope),
      server: api,
      settle: false,
    );
    expect(find.text('Assistant'), findsOneWidget);
    expect(find.text('Checking progress…'), findsOneWidget);
    expect(find.textContaining('tasks.list'), findsNothing);
    expect(find.text("I'm on it — add anything you like…"), findsOneWidget);
    // No quick prompts while it is busy.
    expect(find.text('How are things going?'), findsNothing);
    await _unmount(tester, f);
  });

  testWidgets(
    'a task card says status, project, time and the latest word; controls sit behind More',
    (tester) async {
      final api = _Api()
        ..watchItems = [
          _watchItem(
            'task',
            title: 'Original task',
            observed: 'running',
            outcome: 'error',
            summary: 'The build failed on step 3.',
          ),
        ];
      final f = await _mount(
        tester,
        SingleChildScrollView(
          child: AssistantTaskReceipts(
            scope: scope,
            parts: [
              _tool(
                'tasks.submit',
                output: jsonEncode({
                  'task_id': 'task',
                  'command_id': 'cmd-secret',
                  'state': 'accepted',
                }),
              ),
            ],
          ),
        ),
        server: api,
      );
      expect(find.text('Original task'), findsOneWidget);
      expect(find.text('In progress'), findsOneWidget);
      expect(find.text('Snake game · 2 hours ago'), findsOneWidget);
      expect(find.text('The build failed on step 3.'), findsOneWidget);
      expect(find.text('Open conversation'), findsOneWidget);
      for (final id in ['cmd-secret', 'execution', 'result', 'run-1']) {
        expect(find.textContaining(id), findsNothing, reason: id);
      }
      await tester.tap(find.byTooltip('More'));
      await tester.pumpAndSettle();
      for (final item in ['Show full result', 'Pause', 'Cancel task']) {
        expect(find.text(item), findsOneWidget, reason: item);
      }
      expect(find.text('Resume'), findsNothing);
      expect(find.text('Report again'), findsNothing);
      await tester.tap(find.text('Show full result'));
      await tester.pumpAndSettle();
      expect(find.text('The full written result.'), findsOneWidget);
      await tester.tap(find.byTooltip('More'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Stop following'));
      await tester.pumpAndSettle();
      expect(api.archives.single['task_id'], 'task');
      expect(api.archives.single['expected_revision'], 1);
      expect(api.archives.single['idempotency_key'], isA<String>());
      expect(
        _toasts(f),
        contains(
          "Done — I'll stop following this. The conversation stays as it is.",
        ),
      );
      // Waiting on the user: ask for the reply first.
      api.watchItems = [
        _watchItem(
          'task',
          title: 'Original task',
          observed: 'running',
          pending: 1,
        ),
      ];
      f.container.invalidate(assistantWatchProvider(scope));
      await tester.pumpAndSettle();
      expect(find.text('Needs your reply'), findsOneWidget);
      expect(
        find.text(
          "It's waiting for your reply — open the conversation to carry on.",
        ),
        findsOneWidget,
      );
      expect(find.text('Reply'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await _unmount(tester, f);
    },
  );

  test(
    'stopping to follow a task that changed reads it once more and uses a new key',
    () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final api = _Api()..archiveConflicts = 1;
      final f = Fixture(prefs, server: api);
      addTearDown(f.close);
      await f.ready();
      await f.controller.archive(f.state.tasks.single);
      expect(api.archives, hasLength(2));
      expect(api.archives[0]['expected_revision'], 1);
      expect(api.archives[1]['expected_revision'], 5);
      expect(
        api.archives[0]['idempotency_key'],
        isNot(api.archives[1]['idempotency_key']),
      );
    },
  );

  testWidgets(
    'My tasks counts unfinished work, flags what waits, and groups the sheet',
    (tester) async {
      final api = _Api()
        ..watchHasMore = true
        ..watchItems = [
          _watchItem('finished', title: 'Finished work', outcome: 'succeeded'),
          _watchItem('working', title: 'Working', sessionStatus: 'busy'),
          _watchItem('asking', title: 'Asking', pending: 2),
          _watchItem(
            'paused',
            title: 'Paused work',
            desired: 'paused',
            observed: 'paused',
          ),
        ];
      for (final (id, title) in const [
        ('finished', 'Finished work'),
        ('working', 'Working'),
        ('asking', 'Asking'),
        ('paused', 'Paused work'),
      ]) {
        api.tasks[id] = _taskView(id, title: title);
      }
      api.sessionItems.add({
        'id': 'conversation-secret',
        'title': '',
        'project_name': 'Snake game',
        'link': {
          'available': true,
          'version': List.filled(64, 'a').join(),
          'reason_code': null,
          'task_id': null,
          'archived': false,
        },
      });
      final f = await _mount(
        tester,
        Scaffold(
          appBar: AppBar(actions: const [AssistantTasksButton(scope: scope)]),
          body: const SizedBox.expand(),
        ),
        server: api,
        size: const Size(390, 1800),
        scaffold: false,
      );
      expect(
        find.bySemanticsLabel('My tasks, 1 waiting for your reply'),
        findsOneWidget,
      );
      expect(find.text('3'), findsOneWidget);
      expect(
        find.byKey(const ValueKey('assistant-tasks-waiting-dot')),
        findsOneWidget,
      );
      await tester.tap(find.byKey(const ValueKey('assistant-tasks-button')));
      await tester.pumpAndSettle();
      final headings = [
        'Needs your reply · 1',
        'Not finished yet · 2',
        'Recently finished · 1',
      ];
      final heights = [
        for (final heading in headings)
          tester.getTopLeft(find.text(heading)).dy,
      ];
      expect(heights, orderedEquals([...heights]..sort()));
      final cards = [
        for (final title in const [
          'Asking',
          'Working',
          'Paused work',
          'Finished work',
        ])
          tester.getTopLeft(find.text(title)).dy,
      ];
      expect(cards, orderedEquals([...cards]..sort()));
      expect(
        find.text(
          'There are older tasks — ask me "list all my tasks" to see them.',
        ),
        findsOneWidget,
      );
      await tester.tap(
        find.text('Let the assistant follow an existing conversation'),
      );
      await tester.pumpAndSettle();
      expect(find.text('Follow an existing conversation'), findsOneWidget);
      expect(find.text('Untitled conversation'), findsOneWidget);
      expect(find.textContaining('conversation-secret'), findsNothing);
      expect(tester.takeException(), isNull);
      await _unmount(tester, f);
    },
  );

  testWidgets('My tasks says what to do when nothing was handed over', (
    tester,
  ) async {
    final f = await _mount(
      tester,
      Scaffold(
        appBar: AppBar(actions: const [AssistantTasksButton(scope: scope)]),
        body: const SizedBox.expand(),
      ),
      server: _Api(),
      scaffold: false,
    );
    expect(find.bySemanticsLabel('My tasks'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('assistant-tasks-button')));
    await tester.pumpAndSettle();
    expect(find.text('Nothing handed to me yet'), findsOneWidget);
    expect(
      find.text(
        'Tell me in the box below what to do, e.g. "make the home page dark in one of my projects".',
      ),
      findsOneWidget,
    );
    await _unmount(tester, f);
  });

  testWidgets(
    'the attention card lists other conversations\' questions and only failed replies',
    (tester) async {
      final api = _RequestsWaiting()
        ..waiting = [
          {
            'id': 'waiting-1',
            'session_id': 'palette',
            'session_title': '配色讨论',
            'project_name': '贪吃蛇',
            'questions': [
              {'header': '', 'question': '页面用哪种配色？'},
            ],
          },
        ];
      final f = await _mount(
        tester,
        const SingleChildScrollView(child: AssistantRequests(scope: scope)),
        server: api,
      );
      expect(find.text('1 things need you'), findsOneWidget);
      expect(
        find.text('Other conversations are waiting for an answer too:'),
        findsOneWidget,
      );
      expect(find.text('配色讨论 · 贪吃蛇'), findsOneWidget);
      expect(find.text('页面用哪种配色？'), findsOneWidget);
      expect(find.text('Answer'), findsOneWidget);
      expect(
        find.text("Or just tell me the answer and I'll reply for you."),
        findsOneWidget,
      );
      expect(
        find.text(
          "One of your replies didn't take effect — please open the conversation.",
        ),
        findsOneWidget,
      );
      for (final id in ['cmd-failed', 'cmd-applied', 'waiting-1', 'palette']) {
        expect(find.textContaining(id), findsNothing, reason: id);
      }
      expect(tester.takeException(), isNull);
      await _unmount(tester, f);
    },
  );

  testWidgets('a failed reply alone is not counted as zero things', (
    tester,
  ) async {
    final f = await _mount(
      tester,
      const SingleChildScrollView(child: AssistantRequests(scope: scope)),
      server: _RequestsWaiting(),
    );
    expect(find.text('Things that need you'), findsOneWidget);
    expect(find.textContaining('0 things'), findsNothing);
    expect(
      find.text(
        "One of your replies didn't take effect — please open the conversation.",
      ),
      findsOneWidget,
    );
    await _unmount(tester, f);
  });

  testWidgets('nothing waits, nothing shows', (tester) async {
    final f = await _mount(
      tester,
      const SingleChildScrollView(child: AssistantRequests(scope: scope)),
      server: _Api(),
    );
    expect(find.textContaining('need you'), findsNothing);
    await _unmount(tester, f);
  });
}

class _RequestsWaiting extends _Api {
  @override
  Future<Map<String, dynamic>> requests(String kind, {String? cursor}) async =>
      {
        'items': <Map<String, dynamic>>[],
        'receipts': [
          if (kind == 'question') ...[
            {
              'command_id': 'cmd-failed',
              'state': 'failed',
              'session_id': 'execution',
            },
            {'command_id': 'cmd-applied', 'state': 'applied'},
          ],
        ],
      };
}
