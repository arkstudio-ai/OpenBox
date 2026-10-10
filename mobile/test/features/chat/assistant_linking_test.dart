import 'dart:async';

import 'package:bossip_mobile/features/chat/widgets/assistant_link_existing.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'assistant_fixture.dart';

final version = List.filled(64, 'a').join();

class _LinkApi extends TestApi {
  int lists = 0;
  bool blocked = false, loseReply = false, badReceipt = false;
  Completer<void>? gate;
  final links = <Map<String, dynamic>>[];
  @override
  Future<Map<String, dynamic>> sessions({String? cursor}) async {
    lists++;
    return {
      'items': [
        {
          'id': 'original',
          'title': 'Existing work',
          'project_name': 'Project A',
          'link': {
            'available': !blocked,
            'version': version,
            'reason_code': blocked ? 'ASSISTANT_LINK_HISTORY_UNVERIFIED' : null,
            'task_id': null,
            'archived': false,
          },
        },
      ],
      'next_cursor': null,
    };
  }

  @override
  Future<Map<String, dynamic>> linkExisting(Map<String, dynamic> body) async {
    links.add(Map.of(body));
    await gate?.future;
    if (loseReply) {
      throw ApiError(status: 503, code: 'NETWORK', message: 'Lost response');
    }
    return {
      'command_id': 'linked-command',
      'task_id': 'linked-task',
      'state': 'linked',
      'execution_session_id': badReceipt ? 'replacement' : body['session_id'],
    };
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'lost link reply survives controller restart with the same observation and key',
    () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final api = _LinkApi()..loseReply = true;
      var f = Fixture(prefs, server: api);
      await f.ready();
      await expectLater(
        f.controller.linkExisting('original', version),
        throwsA(isA<ApiError>()),
      );
      final first = Map.of(api.links.single);
      expect(first.keys.toSet(), {
        'session_id',
        'expected_version',
        'idempotency_key',
      });
      f.close();
      api.loseReply = false;
      f = Fixture(prefs, server: api);
      addTearDown(f.close);
      await f.ready();
      await f.controller.linkExisting('original', List.filled(64, 'b').join());
      expect(api.links.last, first);
      expect(
        prefs.getKeys().where((key) => key.endsWith(':link:original')),
        isEmpty,
      );
      expect(api.sends, isEmpty);
    },
  );

  test('wrong session receipt retains the pinned link request', () async {
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final api = _LinkApi()..badReceipt = true;
    final f = Fixture(prefs, server: api);
    addTearDown(f.close);
    await f.ready();
    await expectLater(
      f.controller.linkExisting('original', version),
      throwsA(isA<FormatException>()),
    );
    api.badReceipt = false;
    await f.controller.linkExisting('original', List.filled(64, 'b').join());
    expect(api.links.first, api.links.last);
  });

  test(
    'switching workspace while a link is pending cannot clear its retry identity',
    () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final api = _LinkApi()..gate = Completer<void>();
      final f = Fixture(prefs, server: api);
      addTearDown(f.close);
      await f.ready();
      final pending = f.controller.linkExisting('original', version);
      while (api.links.isEmpty) {
        await Future<void>.delayed(Duration.zero);
      }
      f.container.read(workspaceScopeProvider).currentId = 'other-workspace';
      api.gate!.complete();
      await pending;
      expect(
        prefs.getKeys().where((key) => key.endsWith(':link:original')),
        hasLength(1),
      );
      await f.controller.linkExisting('different', version);
      expect(api.links, hasLength(1));
    },
  );

  testWidgets(
    'the list reads when shown and explains an unavailable conversation without sending',
    (tester) async {
      final api = _LinkApi()..blocked = true;
      final f = (await tester.runAsync(() async {
        SharedPreferences.setMockInitialValues({});
        final prefs = await SharedPreferences.getInstance();
        final bundle = await I18nBundle.load();
        final f = Fixture(
          prefs,
          server: api,
          i18n: I18nState(language: 'en-US', bundle: bundle),
        );
        await f.ready();
        return f;
      }))!;
      addTearDown(f.close);
      await tester.pumpWidget(
        UncontrolledProviderScope(
          container: f.container,
          child: MaterialApp(
            theme: ThemeData(
              extensions: [
                BossipTokens.resolve(
                  BossipThemeName.default_,
                  Brightness.light,
                ),
              ],
            ),
            home: const Scaffold(body: AssistantLinkExisting(scope: scope)),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(api.lists, 1);
      expect(find.text('Existing work'), findsOneWidget);
      expect(find.text('Project A'), findsOneWidget);
      // A reason this build has no words for still reads as plain language.
      expect(
        find.text("This conversation can't be handed over right now."),
        findsOneWidget,
      );
      expect(find.textContaining('ASSISTANT_'), findsNothing);
      final button = tester.widget<OutlinedButton>(
        find.widgetWithText(OutlinedButton, 'Hand to assistant'),
      );
      expect(button.onPressed, isNull);
      expect(api.links, isEmpty);
      await tester.pumpWidget(const SizedBox.shrink());
      f.close();
      await tester.pump();
    },
  );
}
