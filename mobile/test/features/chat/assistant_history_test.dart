import 'dart:async';

import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/assistant_screen.dart';
import 'package:bossip_mobile/features/chat/state/assistant_controller.dart';
import 'package:bossip_mobile/features/chat/state/config_providers.dart';
import 'package:bossip_mobile/features/chat/widgets/chat_flow.dart';
import 'package:bossip_mobile/shared/api/containers_api.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'assistant_fixture.dart' show FixedI18n, TestWs, scope;

Map<String, dynamic> _message(String id, String role, String text) => {
  'id': id,
  'session_id': 'main',
  'role': role,
  'finish': role == 'assistant' ? 'stop' : null,
  'created_at': DateTime.now().toUtc().toIso8601String(),
  'parts': [
    {'id': '$id-text', 'type': 'text', 'text': text},
  ],
};

/// The V2 backend over HTTP: the real [AssistantApi] runs unmodified, and
/// every request it makes is recorded.
class _Backend {
  _Backend() {
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          requests.add(options);
          final data = _respond(options);
          if (data == null) {
            handler.reject(
              DioException(
                requestOptions: options,
                response: Response<dynamic>(
                  requestOptions: options,
                  statusCode: 404,
                ),
              ),
            );
            return;
          }
          handler.resolve(
            Response<dynamic>(
              requestOptions: options,
              statusCode: 200,
              data: data,
            ),
          );
        },
      ),
    );
  }

  final dio = Dio(BaseOptions(baseUrl: 'http://assistant.invalid'));
  final requests = <RequestOptions>[];

  List<String> get paths => [for (final r in requests) r.path];

  Map<String, dynamic>? _respond(RequestOptions options) =>
      switch (options.path) {
        '/api/assistant' => {
          'state': 'ready',
          'session': {
            'id': 'main',
            'user_id': scope.userId,
            'workspace_id': scope.workspaceId,
            'project_id': 'default',
            'kind': 'assistant',
            'agent': 'assistant',
            'status': 'idle',
            'model': 'test/model',
          },
          'event_cursor': 'cursor-1',
          'high_water_mark': 4,
          'last_seen_sequence': 0,
          'answers': [
            {
              'message_id': 'm04',
              'sequence': 4,
              'available': true,
              'display_token': 'display-m04',
            },
          ],
          'tasks': <Map<String, dynamic>>[],
          'next_task_cursor': null,
        },
        '/api/agent/session/main/history' =>
          options.queryParameters['before'] == null
              ? {
                  'messages': [
                    _message('m03', 'user', 'How is the page coming along?'),
                    _message('m04', 'assistant', 'The page is dark now.'),
                  ],
                  'has_more': true,
                }
              : {
                  'messages': [
                    _message('m01', 'user', 'Make the page dark'),
                    _message('m02', 'assistant', 'On it.'),
                  ],
                  'has_more': false,
                },
        '/api/assistant/events' => {'state': 'ready'},
        '/api/assistant/requests' => {
          'items': <Map<String, dynamic>>[],
          'receipts': <Map<String, dynamic>>[],
        },
        '/api/assistant/requests/waiting' => {
          'items': <Map<String, dynamic>>[],
        },
        '/api/assistant/watch' => {
          'items': <Map<String, dynamic>>[],
          'has_more': false,
        },
        '/api/assistant/read-cursor' => {'last_seen_sequence': 4},
        _ => null,
      };
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets(
    'the assistant reads its conversation from history, merges older pages, copies locally and marks what was seen',
    (tester) async {
      tester.view.physicalSize = const Size(390, 1400);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      final backend = _Backend();
      final ws = TestWs();
      final container = (await tester.runAsync(() async {
        SharedPreferences.setMockInitialValues({});
        final prefs = await SharedPreferences.getInstance();
        final bundle = await I18nBundle.load();
        return ProviderContainer(
          overrides: [
            prefsProvider.overrideWithValue(prefs),
            wsClientProvider.overrideWithValue(ws),
            assistantScopeProvider.overrideWithValue(scope),
            assistantApiProvider(
              scope,
            ).overrideWithValue(AssistantApi(backend.dio, scope)),
            appConfigProvider.overrideWith(
              (ref) async => AppConfig.fromJson({
                'models': <Map<String, dynamic>>[],
                'default_model': 'test/model',
              }),
            ),
            runningContainerProvider.overrideWith(
              (ref) => throw StateError('Assistant discovered a desktop'),
            ),
            i18nProvider.overrideWith(
              () => FixedI18n(
                I18nState(language: 'en-US', bundle: bundle),
                prefs,
              ),
            ),
          ],
        );
      }))!;
      container.read(authSessionProvider).userId = scope.userId;
      container.read(workspaceScopeProvider).currentId = scope.workspaceId;
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
      await tester.pumpWidget(
        UncontrolledProviderScope(
          container: container,
          child: MaterialApp(
            theme: ThemeData(
              extensions: [
                BossipTokens.resolve(
                  BossipThemeName.default_,
                  Brightness.light,
                ),
              ],
            ),
            home: const Scaffold(body: AssistantScreen(scope: scope)),
          ),
        ),
      );
      // The first read runs on the test clock, like every later one.
      Future<void> settle() async {
        for (var i = 0; i < 6; i++) {
          await tester.pump(const Duration(milliseconds: 20));
        }
      }

      await settle();
      expect(find.text('How is the page coming along?'), findsOneWidget);
      expect(find.text('The page is dark now.'), findsOneWidget);
      expect(backend.paths, contains('/api/agent/session/main/history'));
      expect(
        backend.paths.where((path) => path.contains('/api/assistant/messages')),
        isEmpty,
      );

      // The answer on screen is marked read with the snapshot's token alone.
      final receipts = backend.requests
          .where((r) => r.path == '/api/assistant/read-cursor')
          .toList();
      expect(receipts, hasLength(1));
      expect(receipts.single.data, {
        'last_seen_sequence': 4,
        'display_token': 'display-m04',
      });

      // An older page goes in front of what is held.
      unawaited(
        container.read(assistantControllerProvider(scope).notifier).loadOlder(),
      );
      await settle();
      expect(
        backend.requests
            .where((r) => r.path == '/api/agent/session/main/history')
            .map((r) => r.queryParameters['before']),
        containsAll(<String?>[null, 'm03']),
      );
      final state = container.read(assistantControllerProvider(scope));
      expect(state.messages.map((m) => m.id), ['m01', 'm02', 'm03', 'm04']);
      expect(state.hasMore, isFalse);
      // Laid out above what was on screen: scroll up to it.
      await tester.drag(find.byType(ChatFlow), const Offset(0, 800));
      await settle();
      expect(find.text('On it.'), findsOneWidget);
      await tester.drag(find.byType(ChatFlow), const Offset(0, -2000));
      await settle();

      // Copying just copies: no read of any kind.
      final before = backend.requests.length;
      await tester.tap(find.byTooltip('Copy').last);
      await tester.pump();
      await tester.pump();
      expect(writes, [
        {'text': 'The page is dark now.'},
      ]);
      expect(backend.requests.length, before);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      container.dispose();
      await ws.close();
      await tester.pump(const Duration(milliseconds: 1));
    },
  );
}
