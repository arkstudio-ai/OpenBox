import 'dart:async';

import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_request_review.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'assistant_fixture.dart' show FixedI18n, scope;

final _scope = StateProvider<AssistantScope?>((_) => scope);
final _binding = {
  'assistant_session_id': 'main',
  'workspace_id': scope.workspaceId,
  'request_revision': List.filled(64, 'a').join(),
};
const _review = 'Review the full request to reply in chat';
const _reviewed =
    'The full request was displayed. You can reply in chat; '
    'general agreement grants this time only.';

class _Api extends AssistantApi {
  _Api() : super(Dio(), scope);
  final reviews = <({String kind, String id})>[];
  final displays = <String>[];
  Completer<Map<String, dynamic>>? holdReview, holdDisplay;
  Map<String, dynamic> response = {
    'segments': [for (var i = 0; i < 15; i++) 'Section $i: ${'target ' * 30}'],
    'display_token': 'signed-current-token',
    'request_revision': _binding['request_revision'],
  };
  Map<String, dynamic> receipt = {'state': 'displayed', 'display_id': 'event'};

  @override
  Future<Map<String, dynamic>> reviewRequest(String kind, String id) async {
    reviews.add((kind: kind, id: id));
    return holdReview == null ? response : holdReview!.future;
  }

  @override
  Future<Map<String, dynamic>> requestDisplayed(String token) async {
    displays.add(token);
    return holdDisplay == null ? receipt : holdDisplay!.future;
  }
}

Future<ProviderContainer> _mount(
  WidgetTester tester,
  _Api api, {
  String kind = 'permission',
  double textScale = 1,
}) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
  final container = (await tester.runAsync(() async {
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final bundle = await I18nBundle.load();
    return ProviderContainer(
      overrides: [
        assistantScopeProvider.overrideWith((ref) => ref.watch(_scope)),
        assistantApiProvider(scope).overrideWithValue(api),
        i18nProvider.overrideWith(
          () => FixedI18n(I18nState(language: 'en-US', bundle: bundle), prefs),
        ),
      ],
    );
  }))!;
  addTearDown(container.dispose);
  await tester.pumpWidget(
    UncontrolledProviderScope(
      container: container,
      child: MaterialApp(
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(
            context,
          ).copyWith(textScaler: TextScaler.linear(textScale)),
          child: child!,
        ),
        home: Scaffold(
          body: AssistantRequestReviewButton(
            scope: scope,
            kind: kind,
            requestId: 'request',
            binding: _binding,
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return container;
}

Future<void> _open(WidgetTester tester) async {
  await tester.tap(find.text(_review));
  await tester.pumpAndSettle();
}

ScrollController _scroll(WidgetTester tester) => tester
    .widget<SingleChildScrollView>(
      find.descendant(
        of: find.byType(AssistantRequestReview),
        matching: find.byType(SingleChildScrollView),
      ),
    )
    .controller!;

Future<void> _scan(WidgetTester tester) async {
  final scroll = _scroll(tester);
  final last = scroll.position.maxScrollExtent;
  for (var offset = 0.0; offset < last; offset += 150) {
    scroll.jumpTo(offset);
    await tester.pump(const Duration(milliseconds: 16));
  }
  scroll.jumpTo(last);
  await tester.pump(const Duration(milliseconds: 16));
  await tester.pump();
}

Future<void> _unmount(WidgetTester tester) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pump();
}

void main() {
  for (final kind in ['question', 'permission']) {
    testWidgets('$kind review requires every visible slice without approving', (
      tester,
    ) async {
      final api = _Api();
      await _mount(tester, api, kind: kind);
      expect(api.reviews, isEmpty);
      await _open(tester);
      expect(api.reviews.single, (kind: kind, id: 'request'));
      expect(api.displays, isEmpty);
      // Jumping directly to the end leaves unviewed middle paragraphs.
      final scroll = _scroll(tester);
      scroll.jumpTo(scroll.position.maxScrollExtent);
      await tester.pump(const Duration(milliseconds: 300));
      expect(api.displays, isEmpty);
      await _scan(tester);
      expect(api.displays, ['signed-current-token']);
      expect(find.text(_reviewed), findsOneWidget);
      await _scan(tester);
      expect(api.displays, hasLength(1));
      expect(tester.takeException(), isNull);
      await _unmount(tester);
    });
  }

  testWidgets(
    'a wrapped block taller than the viewport needs its entire middle too',
    (tester) async {
      final api = _Api();
      api.response['segments'] = ['Target boundary ${'operation ' * 22}'];
      await _mount(tester, api, textScale: 3);
      await _open(tester);
      final scroll = _scroll(tester);
      expect(scroll.position.maxScrollExtent, greaterThan(1200));
      scroll.jumpTo(scroll.position.maxScrollExtent);
      await tester.pump(const Duration(milliseconds: 300));
      expect(api.displays, isEmpty);
      await _scan(tester);
      expect(api.displays, ['signed-current-token']);
      expect(tester.takeException(), isNull);
      await _unmount(tester);
    },
  );

  testWidgets('background and covered-route rendering never count as display', (
    tester,
  ) async {
    final api = _Api();
    await _mount(tester, api);
    await _open(tester);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await _scan(tester);
    expect(api.displays, isEmpty);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pump(const Duration(milliseconds: 300));
    expect(api.displays, isEmpty);
    final navigator = Navigator.of(
      tester.element(find.byType(AssistantRequestReview)),
    );
    unawaited(
      showDialog<void>(
        context: tester.element(find.byType(AssistantRequestReview)),
        builder: (_) => const Dialog(child: Text('Covering dialog')),
      ),
    );
    await tester.pumpAndSettle();
    await _scan(tester);
    expect(api.displays, isEmpty);
    navigator.pop();
    await tester.pumpAndSettle();
    expect(api.displays, isEmpty);
    await _scan(tester);
    expect(api.displays, hasLength(1));
    await _unmount(tester);
  });

  testWidgets(
    'late review after scope switches away and back cannot restore text',
    (tester) async {
      final api = _Api()..holdReview = Completer<Map<String, dynamic>>();
      final container = await _mount(tester, api);
      await tester.tap(find.text(_review));
      await tester.pump(const Duration(milliseconds: 400));
      container.read(_scope.notifier).state = (
        userId: 'other',
        workspaceId: 'workspace',
      );
      await tester.pump();
      container.read(_scope.notifier).state = scope;
      api.holdReview!.complete(api.response);
      await tester.pumpAndSettle();
      expect(find.textContaining('Section 0:'), findsNothing);
      expect(api.displays, isEmpty);
      api.holdReview = null;
      await tester.tap(find.text('Retry'));
      await tester.pumpAndSettle();
      await _scan(tester);
      expect(api.displays, hasLength(1));
      await _unmount(tester);
    },
  );

  testWidgets('closing an in-flight review prevents late display evidence', (
    tester,
  ) async {
    final api = _Api()..holdReview = Completer<Map<String, dynamic>>();
    await _mount(tester, api);
    await tester.tap(find.text(_review));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.byType(CloseButton));
    await tester.pumpAndSettle();
    api.holdReview!.complete(api.response);
    await tester.pumpAndSettle();
    expect(api.displays, isEmpty);
    expect(find.byType(AssistantRequestReview), findsNothing);
    expect(tester.takeException(), isNull);
    await _unmount(tester);
  });

  testWidgets('layout changes require measuring the original text again', (
    tester,
  ) async {
    final api = _Api();
    await _mount(tester, api);
    await _open(tester);
    final scroll = _scroll(tester);
    tester.view.physicalSize = const Size(480, 844);
    scroll.jumpTo(650);
    await tester.pump();
    final end = scroll.position.maxScrollExtent;
    for (var offset = 650.0; offset < end; offset += 150) {
      scroll.jumpTo(offset);
      await tester.pump(const Duration(milliseconds: 16));
    }
    scroll.jumpTo(end);
    await tester.pump();
    expect(api.displays, isEmpty);
    await _scan(tester);
    expect(api.displays, hasLength(1));
    await _unmount(tester);
  });

  for (final (index, invalid) in [
    {'request_revision': 'obsolete'},
    {'segments': <String>[]},
    {
      'segments': ['first', 1],
    },
    {
      'segments': ['x' * 32001],
    },
    {'display_token': ''},
  ].indexed) {
    testWidgets(
      'invalid review $index ${invalid.keys.first} hides all unverified content',
      (tester) async {
        final api = _Api();
        api.response = {...api.response, ...invalid};
        await _mount(tester, api);
        await _open(tester);
        expect(find.text('Retry'), findsOneWidget);
        expect(find.textContaining('Section 0:'), findsNothing);
        expect(api.displays, isEmpty);
        expect(tester.takeException(), isNull);
        await _unmount(tester);
      },
    );
  }

  testWidgets(
    'unconfirmed display receipt clears content and requires a fresh review',
    (tester) async {
      final api = _Api()..receipt = {'state': 'applied', 'display_id': 'bad'};
      await _mount(tester, api);
      await _open(tester);
      await _scan(tester);
      expect(find.text(_reviewed), findsNothing);
      expect(find.text('Retry'), findsOneWidget);
      expect(find.textContaining('Section 0:'), findsNothing);
      api.receipt = {'state': 'displayed', 'display_id': 'confirmed'};
      api.response['display_token'] = 'fresh-token';
      await tester.tap(find.text('Retry'));
      await tester.pumpAndSettle();
      expect(api.displays, ['signed-current-token']);
      await _scan(tester);
      expect(api.displays, ['signed-current-token', 'fresh-token']);
      expect(find.text(_reviewed), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets('late display response cannot affect the next workspace', (
    tester,
  ) async {
    final api = _Api()..holdDisplay = Completer<Map<String, dynamic>>();
    final container = await _mount(tester, api);
    await _open(tester);
    await _scan(tester);
    expect(api.displays, hasLength(1));
    container.read(_scope.notifier).state = (
      userId: scope.userId,
      workspaceId: 'other',
    );
    await tester.pump();
    api.holdDisplay!.complete(api.receipt);
    await tester.pumpAndSettle();
    expect(find.text(_reviewed), findsNothing);
    expect(find.textContaining('Section 0:'), findsNothing);
    expect(tester.takeException(), isNull);
    await _unmount(tester);
  });

  testWidgets(
    'request withdrawn while recording cannot leave a reviewed receipt',
    (tester) async {
      final api = _Api()..holdDisplay = Completer<Map<String, dynamic>>();
      await _mount(tester, api);
      await _open(tester);
      await _scan(tester);
      api.holdDisplay!.completeError(
        StateError('Request is no longer pending'),
      );
      await tester.pumpAndSettle();
      expect(find.text(_reviewed), findsNothing);
      expect(find.textContaining('Section 0:'), findsNothing);
      expect(find.text('Retry'), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets('delayed display response never extends the review window', (
    tester,
  ) async {
    final api = _Api()..holdDisplay = Completer<Map<String, dynamic>>();
    await _mount(tester, api);
    await _open(tester);
    await _scan(tester);
    await tester.pump(const Duration(minutes: 4));
    api.holdDisplay!.complete(api.receipt);
    await tester.pumpAndSettle();
    expect(find.text(_reviewed), findsOneWidget);
    await tester.pump(const Duration(minutes: 1));
    expect(find.text(_reviewed), findsNothing);
    expect(find.text('Retry'), findsOneWidget);
    await _unmount(tester);
  });

  testWidgets('expired display does not keep a ready-to-reply hint', (
    tester,
  ) async {
    final api = _Api();
    await _mount(tester, api);
    await _open(tester);
    await _scan(tester);
    expect(find.text(_reviewed), findsOneWidget);
    await tester.pump(const Duration(minutes: 5));
    expect(find.text(_reviewed), findsNothing);
    expect(find.text('Retry'), findsOneWidget);
    await _unmount(tester);
  });
}
