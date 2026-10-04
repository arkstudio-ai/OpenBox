import 'dart:async';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/api/assistant_reply.dart';
import 'package:bossip_mobile/features/chat/assistant_screen.dart';
import 'package:bossip_mobile/features/chat/state/assistant_overview.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'assistant_fixture.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SharedPreferences prefs;
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    prefs = await SharedPreferences.getInstance();
  });

  test(
    'drawer overview is read-only and never ensures a main session or fetches transcript bodies',
    () async {
      final api = TestApi()..created = false;
      final ws = TestWs();
      final container = ProviderContainer(
        overrides: [
          assistantApiProvider(scope).overrideWithValue(api),
          wsClientProvider.overrideWithValue(ws),
        ],
      );
      final snapshot = await container.read(
        assistantOverviewProvider(scope).future,
      );
      expect(snapshot.data['state'], 'not_created');
      expect(api.ensures, 0);
      expect(api.validated, isEmpty);
      expect(api.reads, isEmpty);
      container.dispose();
      await ws.close();
    },
  );

  test(
    'ensure is explicit and private bodies only enter through source validation',
    () async {
      final f = Fixture(prefs, server: TestApi()..created = false);
      addTearDown(f.close);
      await f.ready();
      expect(f.api.ensures, 1);
      expect(f.api.validated, contains(equals(['m02'])));
      f.ws.frames.add(
        const WsEvent('message.created', {
          'sessionId': 'main',
          'message': {
            'id': 'forged',
            'session_id': 'main',
            'role': 'assistant',
            'parts': [
              {'type': 'text', 'text': 'stale secret'},
            ],
          },
        }),
      );
      expect(f.state.messages.map((m) => m.id), ['m02']);
    },
  );

  test(
    'a late transcript copy check cannot restore bodies after a newer failed source check',
    () async {
      final f = Fixture(prefs);
      addTearDown(f.close);
      await f.ready();
      final gate = Completer<void>();
      f.api.onRead = (ids) async {
        final earlier = ids.map((id) => f.api.stored[id]!).toList();
        f.api.onRead = null;
        await gate.future;
        return earlier;
      };
      final copy = f.controller.canCopy(['m02'], 'verified answer');
      f.api.failValidation = true;
      await f.controller.refresh();
      expect(f.state.messages, isEmpty);
      gate.complete();
      expect(await copy, isFalse);
      expect(f.state.messages, isEmpty);
    },
  );

  test(
    'lost send response survives controller restart with the exact identity and choices',
    () async {
      final api = TestApi();
      var f = Fixture(prefs, server: api);
      await f.ready();
      f.container.read(pickedModelProvider('main').notifier).state =
          'chosen/model';
      api.onSend = (_) async =>
          throw ApiError(status: 503, code: 'NETWORK', message: 'Lost reply');
      await expectLater(
        f.controller.send('private test input', ['asset']),
        throwsA(isA<ApiError>()),
      );
      await f.ready();
      expect(f.state.sendUncertain, isTrue);
      expect(
        prefs.getKeys().map(prefs.get).join(),
        isNot(contains('private test input')),
      );
      f.close();
      f = Fixture(prefs, server: api);
      addTearDown(f.close);
      await f.ready();
      await expectLater(
        f.controller.send('different input', ['asset']),
        throwsA(isA<ApiError>()),
      );
      expect(api.sends.length, 1);
      api.onSend = null;
      await f.controller.send('private test input', ['asset']);
      expect(api.sends[1], api.sends[0]);
      expect(api.sends[1]['model'], 'chosen/model');
      expect(api.sends[1]['delivery'], 'followup');
    },
  );

  test('a durable human echo wins over a delayed transport failure', () async {
    final f = Fixture(prefs);
    addTearDown(f.close);
    await f.ready();
    final gate = Completer<Map<String, dynamic>>();
    f.api.onSend = (_) => gate.future;
    final send = f.controller.send('one turn', []);
    await Future<void>.delayed(Duration.zero);
    final id = f.api.sends.single['client_id'] as String;
    f.api.stored['m03'] = message(
      'm03',
      role: 'user',
      text: 'one turn',
      clientId: id,
    );
    f.api.newest.add('m03');
    await f.controller.refresh();
    gate.completeError(
      ApiError(status: 503, code: 'NETWORK', message: 'Late failure'),
    );
    await send;
    expect(f.state.sendAccepted, isTrue);
    expect(f.state.sendUncertain, isFalse);
    expect(f.state.messages.where((m) => m.id.startsWith('tmp-')), isEmpty);
  });

  test(
    'simultaneous taps and malformed receipts do not allocate new identities',
    () async {
      final f = Fixture(prefs);
      addTearDown(f.close);
      await f.ready();
      final gate = Completer<Map<String, dynamic>>();
      f.api.onSend = (_) => gate.future;
      final first = f.controller.send('one', []);
      await expectLater(f.controller.send('one', []), throwsStateError);
      await Future<void>.delayed(Duration.zero);
      final failed = expectLater(first, throwsFormatException);
      gate.complete({});
      await failed;
      f.api.onSend = null;
      await f.controller.send('one', []);
      expect(f.api.sends[0]['client_id'], f.api.sends[1]['client_id']);
    },
  );

  test(
    'old pages revalidate and an unsuccessful gap rebuild retains its cursor',
    () async {
      final api = TestApi()..older = ['m00'];
      api.stored['m00'] = message('m00', text: 'removed source');
      final f = Fixture(prefs, server: api);
      addTearDown(f.close);
      await f.ready();
      await f.controller.loadOlder();
      expect(f.state.messages.map((m) => m.id), ['m00', 'm02']);
      api.stored['m00'] = message('m00', status: 'unavailable', checked: 2);
      await f.controller.refresh();
      expect(f.state.messages.first.parts, isEmpty);
      api
        ..cursor = 2
        ..gap = true
        ..failValidation = true;
      await f.controller.refresh();
      expect(f.state.messages, isEmpty);
      api.failValidation = false;
      await f.controller.refresh();
      expect(api.eventCursors.takeLast(2), ['cursor-1', 'cursor-1']);
      expect(f.state.messages.map((m) => m.id), ['m02']);
      await f.controller.refresh();
      expect(api.eventCursors.last, 'cursor-2');
    },
  );

  test(
    'unknown control outcomes replay the original revision and run after state advances',
    () async {
      final f = Fixture(prefs);
      addTearDown(f.close);
      await f.ready();
      f.api.failCommand = true;
      await expectLater(
        f.controller.control(f.state.tasks.single, 'pause'),
        throwsA(isA<ApiError>()),
      );
      f.api.revision = 9;
      await f.ready();
      f.api.failCommand = false;
      await f.controller.control(f.state.tasks.single, 'pause');
      expect(f.api.controls[1], f.api.controls[0]);
      expect(f.api.controls[1]['expected_revision'], 1);
      expect(f.api.controls[1]['expected_run'], {
        'run_id': 'run-1',
        'generation': 1,
      });
    },
  );

  test(
    'scope changes discard in-flight reads and forbid copy of revoked evidence',
    () async {
      final f = Fixture(prefs);
      addTearDown(f.close);
      await f.ready();
      expect(await f.controller.canCopy(['m02'], 'verified answer'), isTrue);
      f.api.stored['m02'] = message('m02', status: 'unavailable', checked: 2);
      expect(await f.controller.canCopy(['m02'], 'verified answer'), isFalse);
      f.api.validationGate = Completer<void>();
      final work = f.controller.refresh();
      await Future<void>.delayed(Duration.zero);
      f.container.read(workspaceScopeProvider).currentId = 'another';
      f.api.validationGate!.complete();
      await work;
      expect(f.state.messages.single.parts, isEmpty);
      expect(await f.controller.canCopy(['m02'], 'verified answer'), isFalse);
    },
  );

  test(
    'HTTP adapter freezes actor/workspace and preserves repeated message parameters',
    () async {
      final dio = Dio();
      final calls = <RequestOptions>[];
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            calls.add(options);
            handler.resolve(
              Response(
                requestOptions: options,
                data: {'messages': <Map<String, dynamic>>[]},
              ),
            );
          },
        ),
      );
      final api = AssistantApi(dio, scope);
      await api.messages('main', ['one', 'two']);
      expect(calls.single.headers['X-Workspace-Id'], 'workspace');
      expect(calls.single.extra['bossip.expectedUser'], 'owner');
      expect(calls.single.extra['bossip.expectedWorkspace'], 'workspace');
      expect(calls.single.uri.queryParametersAll['message_ids'], [
        'one',
        'two',
      ]);
    },
  );

  for (final kind in ['question', 'permission']) {
    test(
      '$kind replies retain the reviewed revision and receipt identity across response loss',
      () async {
        final api = TestApi()..failReply = true;
        AssistantScope? active = scope;
        final binding = {
          'assistant_session_id': 'main',
          'workspace_id': scope.workspaceId,
          'request_revision': List.filled(64, 'a').join(),
          'options_hash': List.filled(64, 'b').join(),
        };
        final answer = kind == 'question'
            ? <String, dynamic>{
                'answers': [
                  ['sensitive reply'],
                ],
              }
            : <String, dynamic>{'action': 'once'};
        var service = AssistantReplyService(prefs, () => active, (_) => api);
        await expectLater(
          service.reply(
            kind: kind,
            id: 'request',
            binding: binding,
            answer: answer,
          ),
          throwsA(isA<ApiError>()),
        );
        expect(
          prefs.getKeys().map(prefs.get).join(),
          isNot(contains('sensitive reply')),
        );
        service = AssistantReplyService(prefs, () => active, (_) => api);
        api.failReply = false;
        await expectLater(
          service.reply(
            kind: kind,
            id: 'request',
            binding: binding,
            answer: {'action': 'different'},
          ),
          throwsA(isA<ApiError>()),
        );
        final advancedBinding = {
          ...binding,
          'request_revision': List.filled(64, 'c').join(),
        };
        await service.reply(
          kind: kind,
          id: 'request',
          binding: advancedBinding,
          answer: answer,
        );
        expect(api.replies[1], api.replies[0]);
        expect(
          api.replies[1]['expected_request_revision'],
          binding['request_revision'],
        );
        expect(api.replies[1]['source_ref'], {'kind': 'card'});
        active = (userId: 'another-owner', workspaceId: 'another-workspace');
        await expectLater(
          service.reply(
            kind: kind,
            id: 'request',
            binding: binding,
            answer: answer,
          ),
          throwsStateError,
        );
        expect(api.replies.length, 2);
        active = scope;
        api.replyGate = Completer<void>();
        final late = service.reply(
          kind: kind,
          id: 'late-request',
          binding: binding,
          answer: answer,
        );
        await Future<void>.delayed(Duration.zero);
        active = (userId: 'new-owner', workspaceId: scope.workspaceId);
        api.replyGate!.complete();
        await expectLater(late, throwsStateError);
        expect(
          QuestionRequest.fromJson({'assistant': binding}).assistant,
          binding,
        );
        expect(
          PermissionRequest.fromJson({'assistant': binding}).assistant,
          binding,
        );
      },
    );
  }

  testWidgets(
    'main entry reuses the composer without desktop discovery or history mutation actions',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      final bundle = await I18nBundle.load();
      const fontPath = String.fromEnvironment('ASSISTANT_TEST_FONT');
      const iconPath = String.fromEnvironment('ASSISTANT_TEST_ICONS');
      if (fontPath.isNotEmpty) {
        await tester.runAsync(() async {
          await ui.loadFontFromList(
            await File(fontPath).readAsBytes(),
            fontFamily: 'RegressionFont',
          );
          if (iconPath.isNotEmpty) {
            await ui.loadFontFromList(
              await File(iconPath).readAsBytes(),
              fontFamily: 'MaterialIcons',
            );
          }
        });
      }
      final f = Fixture(
        prefs,
        i18n: I18nState(language: 'en-US', bundle: bundle),
      );
      addTearDown(f.close);
      await tester.runAsync(f.ready);
      final capture = GlobalKey();
      await tester.pumpWidget(
        UncontrolledProviderScope(
          container: f.container,
          child: MaterialApp(
            theme: ThemeData(
              fontFamily: fontPath.isEmpty ? null : 'RegressionFont',
              extensions: [
                BossipTokens.resolve(
                  BossipThemeName.default_,
                  Brightness.light,
                ),
              ],
            ),
            home: RepaintBoundary(
              key: capture,
              child: const Scaffold(body: AssistantScreen(scope: scope)),
            ),
          ),
        ),
      );
      await tester.pump();
      expect(find.text('verified answer'), findsOneWidget);
      expect(find.text('Regenerate'), findsNothing);
      expect(find.text('Dismiss'), findsNothing);
      expect(f.api.reads, isEmpty);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pump(const Duration(milliseconds: 250));
      await tester.pump();
      expect(f.api.reads.single['display_token'], 'signed-display');
      await tester.tap(find.text('Tasks'));
      await tester.pumpAndSettle();
      expect(find.text('Original task'), findsOneWidget);
      const screenshot = String.fromEnvironment('ASSISTANT_SCREENSHOT_PATH');
      if (screenshot.isNotEmpty) {
        await tester.runAsync(() async {
          final boundary =
              capture.currentContext!.findRenderObject()!
                  as RenderRepaintBoundary;
          final image = await boundary.toImage(pixelRatio: 2);
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          await File(screenshot).writeAsBytes(bytes!.buffer.asUint8List());
          image.dispose();
        });
      }
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      f.close();
      await tester.pump(const Duration(milliseconds: 1));
    },
  );
}

extension<T> on List<T> {
  List<T> takeLast(int count) => sublist(length - count);
}
