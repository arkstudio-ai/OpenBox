import 'dart:convert';

import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/assistant_screen.dart';
import 'package:bossip_mobile/features/chat/state/assistant_watch.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_requests.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_task_receipts.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_tasks.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_welcome.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/assistant_profile.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/widgets/toast.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'assistant_fixture.dart';
import 'assistant_redesign_parts.dart';

/// A server whose tasks and results are whatever a test needs.
class _Api extends TestApi {
  final tasks = <String, Map<String, dynamic>>{};
  final sessionItems = <Map<String, dynamic>>[];
  final gone = <String>{};
  int archiveConflicts = 0;
  int revisionAfterConflict = 5;

  @override
  Future<AssistantTask> task(String id) async {
    if (gone.contains(id)) {
      throw ApiError(
        status: 409,
        code: 'ASSISTANT_EXECUTION_UNAVAILABLE',
        message: 'The original execution Session is unavailable',
      );
    }
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
    user: AuthUser(id: 'owner', username: 'memoryqa_2026'),
    isLoading: false,
  );
}

/// The profile as stored, without a server.
class _Profile extends AssistantProfileNotifier {
  _Profile(this.profile);
  final AssistantProfile profile;
  @override
  Future<AssistantProfile> build() async => profile;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('the welcome greets as the user asked to be called, introduces '
      'the assistant by its name and offers six ideas', (tester) async {
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final bundle = (await tester.runAsync(I18nBundle.load))!;
    String? picked;
    Future<void> welcome(AssistantProfile profile) => tester.pumpWidget(
      ProviderScope(
        key: ValueKey(profile),
        overrides: [
          i18nProvider.overrideWith(
            () =>
                FixedI18n(I18nState(language: 'zh-CN', bundle: bundle), prefs),
          ),
          authProvider.overrideWith(_NamedUser.new),
          assistantProfileProvider.overrideWith(() => _Profile(profile)),
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
    // Never the sign-in name; the default introduction until it is named.
    await welcome(const AssistantProfile());
    await tester.pumpAndSettle();
    expect(find.text('晚上好'), findsOneWidget);
    expect(find.textContaining('memoryqa'), findsNothing);
    expect(find.textContaining('我是你的个人助理'), findsOneWidget);
    await welcome(const AssistantProfile(name: '小七', address: '老王'));
    await tester.pumpAndSettle();
    // Before dawn is still the evening.
    expect(find.text('晚上好，老王'), findsOneWidget);
    expect(find.textContaining('我是小七，你的个人助理'), findsOneWidget);
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
          'm01': userMessage('m01'),
          'm02': replyMessage(
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
          'm03': userMessage('m03', origin: 'task_result', synthetic: true),
          'm04': replyMessage(
            'm04',
            text: 'The page is dark now.',
            parent: 'm03',
          ),
        });
      api.newest = ['m01', 'm02', 'm03', 'm04'];
      api.recalls = {
        'm03': [
          const RecalledMemory(id: 'mem-1', summary: 'Works late on Fridays'),
        ],
      };
      final f = await _mount(
        tester,
        const AssistantScreen(scope: scope),
        server: api,
        size: const Size(390, 1600),
      );
      expect(find.text('Personal assistant'), findsNWidgets(2));
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
      // A thumbs-down asks why; the reason goes with it.
      expect(find.byKey(const ValueKey('reaction-reasons')), findsNothing);
      await tester.tap(find.byTooltip('Bad response').last);
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('reaction-reasons')), findsOneWidget);
      await tester.tap(find.text('Too long'));
      await tester.pumpAndSettle();
      expect(api.reactions.last, {
        'session': 'main',
        'message': 'm04',
        'reaction': 'down',
        'reason': 'too_long',
      });
      expect(_toasts(f), contains('Thanks, noted.'));
      // What the answer drew on, one tap away.
      expect(find.text('Works late on Fridays'), findsNothing);
      await tester.tap(find.text('Drew on 1 memory'));
      await tester.pumpAndSettle();
      expect(find.text('Works late on Fridays'), findsOneWidget);
      expect(find.text('Manage in Knowledge'), findsOneWidget);
      // Named in Settings, on another device or in chat: it speaks with that
      // name at once.
      f.ws.frames.add(
        const WsEvent('assistant.profile.updated', {
          'userId': 'owner',
          'profile': {'name': '小七'},
        }),
      );
      await tester.pumpAndSettle();
      expect(find.text('小七'), findsNWidgets(2));
      expect(find.text('Personal assistant'), findsNothing);
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
        'm01': userMessage('m01'),
        'm02': replyMessage(
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
    expect(find.text('Personal assistant'), findsOneWidget);
    expect(find.text('Checking progress…'), findsOneWidget);
    expect(find.textContaining('tasks.list'), findsNothing);
    expect(find.text("I'm on it — add anything you like…"), findsOneWidget);
    // No quick prompts while it is busy.
    expect(find.text('Any updates?'), findsNothing);
    await _unmount(tester, f);
  });

  testWidgets(
    'a task whose conversation was deleted reads plainly, not as an error',
    (tester) async {
      // Not in the main view's task list, so the card reads it by id.
      final api = _Api()..gone.add('deleted-task');
      final f = await _mount(
        tester,
        SingleChildScrollView(
          child: AssistantTaskReceipts(
            scope: scope,
            parts: [
              toolPart(
                'tasks.submit',
                output: jsonEncode({
                  'task_id': 'deleted-task',
                  'command_id': 'cmd-secret',
                  'state': 'accepted',
                }),
              ),
            ],
          ),
        ),
        server: api,
      );
      expect(
        find.text("This task's conversation has been deleted."),
        findsOneWidget,
      );
      expect(find.textContaining('ASSISTANT_'), findsNothing);
      expect(find.textContaining('cmd-secret'), findsNothing);
      await _unmount(tester, f);
    },
  );

  testWidgets(
    'a task card says status, project, time and the latest word; controls sit behind More',
    (tester) async {
      final api = _Api()
        ..watchItems = [
          watchItem(
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
              toolPart(
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
        watchItem(
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
          watchItem('finished', title: 'Finished work', outcome: 'succeeded'),
          watchItem('working', title: 'Working', sessionStatus: 'busy'),
          watchItem('asking', title: 'Asking', pending: 2),
          watchItem(
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
