import 'dart:async';

import 'package:bossip_mobile/features/chat/widgets/assistant_link_existing.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
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
    'picker reads on open and explains unverified history without sending',
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
          child: const MaterialApp(
            home: Scaffold(body: AssistantLinkExisting(scope: scope)),
          ),
        ),
      );
      expect(api.lists, 0);
      await tester.tap(find.text('Link an existing conversation'));
      await tester.pumpAndSettle();
      expect(api.lists, 1);
      expect(
        find.textContaining('The history’s memory isolation cannot be verified.'),
        findsOneWidget,
      );
      final button = tester.widget<TextButton>(
        find.widgetWithText(TextButton, 'Link'),
      );
      expect(button.onPressed, isNull);
      expect(api.links, isEmpty);
      await tester.pumpWidget(const SizedBox.shrink());
      f.close();
      await tester.pump();
    },
  );
}
