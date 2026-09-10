import 'dart:async';

import 'package:bossip_mobile/features/skills/api/skills_api.dart';
import 'package:bossip_mobile/features/skills/widgets/archive_upload_queue.dart';
import 'package:bossip_mobile/features/skills/widgets/entry_row.dart';
import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/http_client.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:cookie_jar/cookie_jar.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

late I18nBundle _bundle;
ArchiveSelection _file(String name, {int size = 5}) => ArchiveSelection(
  name: name,
  size: size,
  openRead: () => Stream.value([1, 2, 3, 4, 5]),
);

Future<void> _mount(
  WidgetTester tester,
  Widget child, {
  Future<List<ArchiveSelection>> Function()? pick,
  double height = 844,
  double textScale = 1,
}) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'en-US'});
  final prefs = await SharedPreferences.getInstance();
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = Size(320, height);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        i18nProvider.overrideWith(() => I18nController(_bundle, prefs)),
        if (pick != null) archivePickerProvider.overrideWithValue(pick),
      ],
      child: MaterialApp(
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(
            context,
          ).copyWith(textScaler: TextScaler.linear(textScale)),
          child: child!,
        ),
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: Scaffold(body: SingleChildScrollView(child: child)),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _tap(WidgetTester tester, Finder finder) async {
  await tester.ensureVisible(finder);
  await tester.tap(finder);
  await tester.pumpAndSettle();
}

final _submit = find.byKey(const ValueKey('submit-archives'));

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async => _bundle = await I18nBundle.load());

  testWidgets('cancelling the picker restores controls without uploading', (
    tester,
  ) async {
    final picker = Completer<List<ArchiveSelection>>();
    final busy = <bool>[];
    var uploads = 0;
    await _mount(
      tester,
      ArchiveUploadQueue(
        onBusyChanged: busy.add,
        upload: (_, _, _) async => uploads++,
      ),
      pick: () => picker.future,
    );
    await _tap(tester, find.text('Choose multiple archives'));
    expect(busy, [true]);
    expect(tester.widget<FilledButton>(_submit).onPressed, isNull);
    expect(find.textContaining('Uploading'), findsNothing);
    picker.complete([]);
    await tester.pumpAndSettle();
    expect(busy, [true, false]);
    expect(
      tester.widget<OutlinedButton>(find.byType(OutlinedButton)).onPressed,
      isNotNull,
    );
    expect(uploads, 0);
  });

  testWidgets(
    'a full queue scrolls independently and keeps submit reachable on a small screen',
    (tester) async {
      var uploads = 0;
      await _mount(
        tester,
        ArchiveUploadQueue(
          allowCustomName: false,
          onBusyChanged: (_) {},
          upload: (_, _, _) async => uploads++,
        ),
        pick: () async =>
            List.generate(20, (index) => _file('team-archive-$index.zip')),
        height: 640,
        textScale: 1.2,
      );
      await _tap(tester, find.text('Choose multiple archives'));
      expect(find.text('20 / 20'), findsOneWidget);
      expect(_submit.hitTestable(), findsOneWidget);
      expect(tester.getRect(_submit).bottom, lessThanOrEqualTo(640));
      final queue = find.descendant(
        of: find.byType(Scrollbar),
        matching: find.byType(Scrollable),
      );
      await tester.scrollUntilVisible(
        find.text('team-archive-19.zip'),
        180,
        scrollable: queue,
      );
      expect(find.text('team-archive-19.zip').hitTestable(), findsOneWidget);
      expect(_submit.hitTestable(), findsOneWidget);
      expect(uploads, 0);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'a partial batch retries only failures and deduplicates repeated picks',
    (tester) async {
      final calls = <String>[];
      var fail = true;
      final files = [_file('one.zip'), _file('two.tar.gz')];
      await _mount(
        tester,
        ArchiveUploadQueue(
          onBusyChanged: (_) {},
          upload: (file, name, cancel) async {
            calls.add(file.name);
            if (file.name == 'two.tar.gz' && fail) throw StateError('failed');
          },
        ),
        pick: () async => files,
      );
      await _tap(tester, find.text('Choose multiple archives'));
      await _tap(tester, find.text('Choose multiple archives'));
      expect(find.text('one.zip'), findsOneWidget);
      expect(calls, isEmpty);
      await _tap(tester, _submit);
      expect(calls, ['one.zip', 'two.tar.gz']);
      expect(find.text('Uploaded'), findsOneWidget);
      expect(find.text('Failed'), findsOneWidget);
      fail = false;
      await _tap(tester, _submit);
      expect(calls, ['one.zip', 'two.tar.gz', 'two.tar.gz']);
      expect(find.text('Uploaded'), findsNWidgets(2));
      expect(tester.widget<FilledButton>(_submit).onPressed, isNull);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'custom names survive a failed attempt and new batches keep their filenames',
    (tester) async {
      var files = [_file('one.zip')];
      final names = <String?>[];
      await _mount(
        tester,
        ArchiveUploadQueue(
          onBusyChanged: (_) {},
          upload: (file, name, cancel) async {
            names.add(name);
            if (names.length == 1) throw StateError('failed');
          },
        ),
        pick: () async => files,
      );
      await _tap(tester, find.text('Choose multiple archives'));
      await tester.enterText(find.byType(TextField), 'my-skill');
      await _tap(tester, _submit);
      await _tap(tester, _submit);
      files = [_file('two.zip')];
      await _tap(tester, find.text('Choose multiple archives'));
      await _tap(tester, _submit);
      expect(names, ['my-skill', 'my-skill', null]);
    },
  );

  for (final entry in <String, List<ArchiveSelection>>{
    'file count': List.generate(21, (i) => _file('$i.zip')),
    'single-file size': [_file('large.zip', size: 32 * 1024 * 1024 + 1)],
    'total size': List.generate(
      5,
      (i) => _file('$i.zip', size: 32 * 1024 * 1024),
    ),
  }.entries) {
    testWidgets(
      '${entry.key} is rejected before reading or uploading content',
      (tester) async {
        var uploads = 0;
        await _mount(
          tester,
          ArchiveUploadQueue(
            onBusyChanged: (_) {},
            upload: (_, _, _) async {
              uploads++;
            },
          ),
          pick: () async => entry.value,
        );
        await _tap(tester, find.text('Choose multiple archives'));
        expect(
          find.textContaining('Up to 20 files per batch'),
          findsNWidgets(2),
        );
        expect(find.text('Queued'), findsNothing);
        expect(uploads, 0);
        expect(tester.widget<FilledButton>(_submit).onPressed, isNull);
      },
    );
  }

  testWidgets(
    'an uncertain request stays manual and disposal stops remaining uploads',
    (tester) async {
      final gate = Completer<void>();
      final calls = <String>[];
      CancelToken? token;
      await _mount(
        tester,
        ArchiveUploadQueue(
          onBusyChanged: (_) {},
          upload: (file, name, cancel) async {
            token = cancel;
            calls.add(file.name);
            await gate.future;
          },
        ),
        pick: () async => [_file('one.zip'), _file('two.zip')],
      );
      await _tap(tester, find.text('Choose multiple archives'));
      await _tap(tester, _submit);
      await _tap(tester, _submit);
      expect(calls, ['one.zip']);
      await tester.pumpWidget(const SizedBox.shrink());
      expect(token!.isCancelled, isTrue);
      gate.complete();
      await tester.pumpAndSettle();
      expect(calls, ['one.zip']);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'network timeouts show uncertainty and never retry automatically',
    (tester) async {
      var calls = 0;
      await _mount(
        tester,
        ArchiveUploadQueue(
          onBusyChanged: (_) {},
          upload: (_, _, _) async {
            calls++;
            throw DioException(
              requestOptions: RequestOptions(path: '/upload'),
              type: DioExceptionType.receiveTimeout,
            );
          },
        ),
        pick: () async => [_file('one.zip')],
      );
      await _tap(tester, find.text('Choose multiple archives'));
      await _tap(tester, _submit);
      expect(find.textContaining('Upload result is uncertain'), findsOneWidget);
      await tester.pump(const Duration(seconds: 30));
      expect(calls, 1);
    },
  );

  test(
    'multipart streams pin workspace and reject a queued upload after switching users',
    () async {
      final auth = AuthSession()
        ..userId = 'one'
        ..accessToken = 'token-one';
      final workspace = WorkspaceScope()..currentId = 'workspace';
      final dio = buildApiDio(
        auth: auth,
        cookieJar: CookieJar(),
        workspace: workspace,
      );
      var dispatched = 0;
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            dispatched++;
            handler.resolve(
              Response(requestOptions: options, data: <String, dynamic>{}),
            );
          },
        ),
      );
      final call = SkillsApi(dio).uploadArchiveStream(
        filename: 'one.zip',
        length: 5,
        openRead: () => Stream.value([1, 2, 3, 4, 5]),
        userId: 'one',
        workspaceId: 'workspace',
        cancel: CancelToken(),
      );
      auth.userId = 'two';
      await expectLater(
        call,
        throwsA(
          isA<DioException>().having(
            (e) => e.type,
            'type',
            DioExceptionType.cancel,
          ),
        ),
      );
      expect(dispatched, 0);
      dio.close();
    },
  );

  testWidgets(
    'HTTPS icons render as images and failed loads fall back without showing the URL',
    (tester) async {
      const url = 'https://example.test/skill.png';
      await _mount(tester, const EntryIcon(name: 'Skill', icon: url));
      expect(find.text(url), findsNothing);
      expect(find.byType(Image), findsOneWidget);
      await tester.runAsync(() async {});
      await tester.pumpAndSettle();
      expect(find.text('🧩'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('emoji and missing icons retain their existing appearance', (
    tester,
  ) async {
    await _mount(
      tester,
      const Row(
        children: [
          EntryIcon(name: 'Skill', icon: '🎬'),
          EntryIcon(name: 'Alpha'),
        ],
      ),
    );
    expect(find.text('🎬'), findsOneWidget);
    expect(find.text('A'), findsOneWidget);
    expect(find.byType(Image), findsNothing);
  });
}
