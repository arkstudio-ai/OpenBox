import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/state/auth_flow.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/features/light/production/production_media.dart';
import 'package:bossip_mobile/main.dart' as app;
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/api/ui_mode_store.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/ui_mode.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
// Supply a real, local product photo at the simulator's OS picker boundary.
// ignore: depend_on_referenced_packages
import 'package:image_picker_platform_interface/image_picker_platform_interface.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';
import 'package:video_player/video_player.dart';

class _ProductPicker extends ImagePickerPlatform {
  _ProductPicker(this.photo);

  final XFile photo;

  @override
  Future<List<XFile>> getMedia({required MediaOptions options}) async => [
    photo,
  ];

  @override
  Future<List<XFile>> getMultiImageWithOptions({
    MultiImagePickerOptions options = const MultiImagePickerOptions(),
  }) async => [photo];
}

void main() => registerCommercialReferenceTest(
  enabled: const bool.fromEnvironment('COMMERCIAL_QA'),
  submit: const bool.fromEnvironment('COMMERCIAL_QA_SUBMIT'),
  photo: const String.fromEnvironment('COMMERCIAL_QA_PHOTO'),
  password: const String.fromEnvironment('COMMERCIAL_QA_PASSWORD'),
  output: const String.fromEnvironment('COMMERCIAL_QA_OUT'),
);

void registerCommercialReferenceTest({
  required bool enabled,
  required bool submit,
  required String photo,
  required String password,
  required String output,
  String username = 'devtest',
}) {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const reviewSession = String.fromEnvironment('COMMERCIAL_QA_REVIEW');
  const retrySession = String.fromEnvironment('COMMERCIAL_QA_RETRY');
  const prepareOnly = bool.fromEnvironment('COMMERCIAL_QA_PREPARE');
  const freshLogin = bool.fromEnvironment('COMMERCIAL_QA_FRESH_LOGIN');
  const libraryVideo = String.fromEnvironment('COMMERCIAL_QA_LIBRARY_VIDEO');
  const libraryPhoto = String.fromEnvironment('COMMERCIAL_QA_LIBRARY_PHOTO');
  final libraryAsset = libraryVideo.isEmpty ? libraryPhoto : libraryVideo;
  const inspectLibrary = bool.fromEnvironment('COMMERCIAL_QA_INSPECT_LIBRARY');
  const name = String.fromEnvironment('COMMERCIAL_QA_PRODUCT');
  const brief = String.fromEnvironment(
    'COMMERCIAL_QA_BRIEF',
    defaultValue: 'Tôi muốn làm một quảng cáo tiếng Việt từ tư liệu này.',
  );

  testWidgets(
    'Vietnamese commercial reference in the running app',
    (tester) async {
      expect(Uri.parse(Env.apiBase).host, isIn(['localhost', '127.0.0.1']));
      final support = await getApplicationSupportDirectory();
      final evidence = Directory(
        output.isEmpty ? '${support.path}/commercial-reference-qa' : output,
      )..createSync(recursive: true);
      final requests = <String>[];
      final receipts = <String>[];
      final requestIds = <String>[];
      final libraryFirstItems = <String, String>{};
      String? id;
      var recordedProduct = name;
      var recordedBrief = brief;
      var recordedAsset = libraryAsset;

      Finder key(String name) => find.byKey(Key(name));
      Future<void> screenshot(String label) async {
        File(
          '${evidence.path}/$label.png',
        ).writeAsBytesSync(await binding.takeScreenshot(label));
      }

      Future<void> until(
        bool Function() ready,
        String label, {
        int seconds = 35,
      }) async {
        final end = DateTime.now().add(Duration(seconds: seconds));
        while (!ready() && DateTime.now().isBefore(end)) {
          await tester.pump(const Duration(milliseconds: 180));
          await Future<void>.delayed(const Duration(milliseconds: 120));
        }
        if (!ready()) await screenshot('failure-$label');
        expect(ready(), isTrue, reason: label);
      }

      Future<void> tap(Finder finder, String label) async {
        await until(() => finder.evaluate().isNotEmpty, label);
        await tester.ensureVisible(finder.first);
        await tester.pump(const Duration(milliseconds: 240));
        await until(
          () => finder.hitTestable().evaluate().isNotEmpty,
          'visible-$label',
        );
        await tester.tap(finder.hitTestable().first);
        await tester.pump(const Duration(milliseconds: 330));
      }

      Future<void> fill(String field, String value) async {
        await until(() => key(field).evaluate().isNotEmpty, field);
        await tester.ensureVisible(key(field));
        await tester.enterText(key(field), value);
        FocusManager.instance.primaryFocus?.unfocus();
        await tester.pump(const Duration(milliseconds: 500));
      }

      void record(String step) {
        File('${evidence.path}/simulator.json').writeAsStringSync(
          jsonEncode({
            'step': step,
            'session_id': id,
            'account': username,
            'locale': 'vi-VN',
            'product': recordedProduct,
            'brief': recordedBrief,
            if (recordedAsset.isNotEmpty) 'library_asset': recordedAsset,
            if (retrySession.isNotEmpty) 'ui_action': 'retry',
            'actions': requests,
            'accepted_actions': receipts,
            'request_ids': requestIds,
            'at': DateTime.now().toUtc().toIso8601String(),
          }),
        );
      }

      await app.main();
      await tester.pump();
      final container = ProviderScope.containerOf(
        tester.element(find.byType(BossipApp)),
        listen: false,
      );
      await until(() => !container.read(authProvider).isLoading, 'auth-state');
      if (freshLogin ||
          container.read(authProvider).user?.username != username) {
        expect(password, isNotEmpty);
        await container.read(authFlowProvider).login(username, password);
      }
      expect(container.read(authProvider).user?.username, username);
      await container.read(uiModeProvider.notifier).setMode(UiMode.light);
      container.read(i18nProvider.notifier).setLanguage('vi-VN');

      final dio = container.read(apiDioProvider);
      final trace = InterceptorsWrapper(
        onRequest: (options, handler) {
          if (options.method == 'POST' &&
              options.uri.path.endsWith('/actions')) {
            final body = options.data as Map<String, dynamic>;
            requests.add(body['action'] as String);
            requestIds.add(body['request_id'] as String);
            record('request');
          }
          handler.next(options);
        },
        onResponse: (response, handler) {
          final options = response.requestOptions;
          if (options.method == 'GET' &&
              options.uri.path == '/api/assets' &&
              options.queryParameters['offset'] == 0) {
            final page = response.data as Map<String, dynamic>;
            final resources = (page['items'] as List)
                .cast<Map<String, dynamic>>();
            final kind = options.queryParameters['kind'] as String?;
            if (kind != null) {
              libraryFirstItems[kind] = resources.isEmpty
                  ? ''
                  : resources.first['id'] as String;
            }
          }
          if (options.method == 'POST') {
            if (options.uri.path == '/api/light/productions') {
              id =
                  (response.data as Map<String, dynamic>)['session_id']
                      as String;
            } else if (options.uri.path.endsWith('/actions')) {
              final body = options.data as Map<String, dynamic>;
              receipts.add(body['action'] as String);
              record('accepted');
            }
          }
          handler.next(response);
        },
      );
      dio.interceptors.add(trace);
      addTearDown(() => dio.interceptors.remove(trace));

      container.read(routerProvider).go(Paths.home);
      Future<VideoPlayerController> playing(String label) async {
        final video = find.byType(VideoPlayer);
        await until(
          () =>
              video.evaluate().isNotEmpty &&
              tester.widget<VideoPlayer>(video.last).controller.value.position >
                  const Duration(milliseconds: 300),
          label,
          seconds: 60,
        );
        final player = tester.widget<VideoPlayer>(video.last).controller;
        expect(
          player.value.duration.inMilliseconds,
          inInclusiveRange(53150, 53300),
        );
        return player;
      }

      if (reviewSession.isNotEmpty || retrySession.isNotEmpty) {
        expect(reviewSession.isEmpty || retrySession.isEmpty, isTrue);
        final session = retrySession.isEmpty ? reviewSession : retrySession;
        await tap(key('light-production-history'), 'open-history');
        await tap(key('production-history-$session'), 'open-production');
      }

      if (retrySession.isNotEmpty) {
        expect(submit || prepareOnly, isFalse);
        final api = container.read(lightProductionApiProvider);
        final before = await api.get(retrySession);
        expect(before.status, 'failed');
        expect(before.template, productStory);
        expect(before.output, isNull);
        expect(before.assets, hasLength(1));
        id = before.sessionId;
        recordedProduct = before.productName;
        recordedBrief = before.referenceCopy;
        recordedAsset = before.assets.single;
        final retry = find.descendant(
          of: key('production-primary'),
          matching: find.text('Thử lại'),
        );
        await until(() => retry.evaluate().isNotEmpty, 'failed-retry-button');
        expect(requests, isEmpty);
        await screenshot('01-failed-production');
        record('failed_before_retry');
        await tap(retry, 'retry-failed-production');
        await until(() => receipts.isNotEmpty, 'retry-receipt', seconds: 90);
        // The failure page sends another render request under its own ID.
        expect(requests, ['render']);
        expect(receipts, ['render']);
        expect(requestIds.single, isNotEmpty);
        final after = await api.get(retrySession);
        expect(after.sessionId, before.sessionId);
        expect(after.template, before.template);
        expect(after.referenceCopy, before.referenceCopy);
        expect(after.assets, before.assets);
        await screenshot('02-retry-submitted');
        record('retried');
        debugPrint('COMMERCIAL_QA_RETRIED $retrySession');
        return;
      }

      if (reviewSession.isNotEmpty) {
        await until(
          () => key('production-result-preview').evaluate().isNotEmpty,
          'result-preview',
        );
        await screenshot('04-app-result');
        await tap(key('production-result-preview'), 'play-result');
        final player = await playing('generated-film-playing');
        await screenshot('05-app-playback');
        await player.seekTo(const Duration(milliseconds: 51000));
        await tester.pump(const Duration(milliseconds: 550));
        await screenshot('06-app-tail');
        final task = await container
            .read(lightProductionApiProvider)
            .get(reviewSession);
        expect(task.status, 'completed');
        expect(task.output?.video, isTrue);
        expect(requests, isEmpty);
        File('${evidence.path}/app-playback.json').writeAsStringSync(
          jsonEncode({
            'session_id': reviewSession,
            'asset_id': task.output!.id,
            'duration_ms': player.value.duration.inMilliseconds,
            'actions': requests,
          }),
        );
        return;
      }

      await tap(key('light-entry-video'), 'home-video');
      if (libraryAsset.isNotEmpty) {
        expect(libraryVideo.isEmpty || libraryPhoto.isEmpty, isTrue);
        await tap(key('production-source-library'), 'open-library');
        Future<void> waitForLibraryPage(String kind) => until(
          () {
            final first = libraryFirstItems[kind];
            return first != null &&
                (first.isEmpty ||
                    key('production-library-$first').evaluate().isNotEmpty);
          },
          'first-library-page-$kind',
          seconds: 80,
        );
        await waitForLibraryPage('image');
        if (libraryVideo.isNotEmpty) {
          await tap(find.text('Video'), 'filter-video');
          await waitForLibraryPage('video');
        }
        final grid = find.descendant(
          of: find.byType(ProductionLibrary),
          matching: find.byType(CustomScrollView),
        );
        await until(() => grid.evaluate().isNotEmpty, 'material-library-grid');
        final material = key('production-library-$libraryAsset');
        if (inspectLibrary) {
          expect(libraryVideo.isNotEmpty, isTrue);
          expect(prepareOnly || submit, isFalse);
          await until(
            () => material.hitTestable().evaluate().isNotEmpty,
            'uploaded-video-on-first-screen',
          );
          final production = container.read(lightProductionApiProvider);
          final resources = <String, String>{};
          var more = true;
          while (more) {
            final page = await production.library(
              kind: 'video',
              offset: resources.length,
            );
            final before = resources.length;
            for (final item in page.items) {
              resources[item.id] = item.name;
            }
            more = page.hasMore;
            if (more) expect(resources.length, greaterThan(before));
          }
          expect(resources.containsKey(libraryVideo), isTrue);
          expect(
            resources.values.where((name) => name.startsWith('segment-video_')),
            isEmpty,
          );
          expect(requests, isEmpty);
          await screenshot('00-library-after');
          File('${evidence.path}/library-after.json').writeAsStringSync(
            jsonEncode({
              'resources': resources,
              'uploaded_on_first_screen': true,
              'production_actions': requests,
            }),
          );
          return;
        }
        for (var page = 0; page < 60 && material.evaluate().isEmpty; page++) {
          final more = find.text('Tải thêm').hitTestable();
          if (more.evaluate().isNotEmpty) await tester.tap(more.first);
          await tester.drag(grid.first, const Offset(0, -450));
          await tester.pump(const Duration(milliseconds: 400));
          await Future<void>.delayed(const Duration(milliseconds: 140));
        }
        await tap(material, 'select-material');
        await screenshot(
          libraryVideo.isEmpty ? '00-photo-library' : '00-video-library',
        );
        await tap(find.text('Dùng 1 tư liệu'), 'use-material');
      } else {
        expect(File(photo).existsSync(), isTrue);
        final formerPicker = ImagePickerPlatform.instance;
        ImagePickerPlatform.instance = _ProductPicker(
          XFile(photo, mimeType: 'image/png'),
        );
        try {
          await tap(key('production-source-upload'), 'upload');
          await until(
            () => find.text('Đã chọn 1 tư liệu').evaluate().isNotEmpty,
            'uploaded-product',
            seconds: 100,
          );
        } finally {
          ImagePickerPlatform.instance = formerPicker;
        }
      }
      await until(
        () =>
            id != null && find.text('Đã chọn 1 tư liệu').evaluate().isNotEmpty,
        'one-selected-asset',
      );
      await tap(key('production-primary'), 'choose-style');
      await tap(find.text('Tái tạo video nổi bật'), 'reference-library');
      await tap(
        key('production-template-$productStory'),
        'select-commercial-reference',
      );
      await screenshot('01-reference');
      if (!submit) {
        await tap(key('production-headphone-preview'), 'reference-preview');
        await playing('reference-playing');
        await screenshot('01-reference-playing');
        await tap(key('media-viewer-close'), 'close-reference');
      }
      await tap(key('production-primary'), 'use-reference');
      await fill('production-product-name', name);
      await fill('production-script', brief);
      await until(
        () => key('production-primary').evaluate().isNotEmpty,
        'review',
      );
      await screenshot('02-review');
      expect(
        requests,
        isEmpty,
        reason: 'Choosing a reference only prepares the form.',
      );
      record('review');
      if (prepareOnly) {
        expect(submit, isFalse);
        await tap(key('production-optimize'), 'prepare-storyboard');
        await until(
          () => key('production-remake-script').evaluate().isNotEmpty,
          'prepared-storyboard',
          seconds: 300,
        );
        final task = await container.read(lightProductionApiProvider).get(id!);
        expect(task.remakePrepared, isTrue);
        expect(task.script, contains('53,21 giây · 28 cảnh'));
        expect(RegExp(r'[\u4e00-\u9fff]').hasMatch(task.script), isFalse);
        expect(task.referenceCopy, brief);
        expect(task.productName, name);
        expect(task.assets, hasLength(1));
        if (libraryAsset.isNotEmpty) expect(task.assets.single, libraryAsset);
        expect(requests, ['prepare']);
        expect(receipts, ['prepare']);
        await tap(key('production-remake-script'), 'read-storyboard');
        await screenshot('03-prepared');
        File(
          '${evidence.path}/storyboard-api.txt',
        ).writeAsStringSync(task.script);
        record('prepared');
        return;
      }
      if (!submit) return;

      final generate = find.descendant(
        of: key('production-primary'),
        matching: find.text('Tạo video'),
      );
      expect(
        generate,
        findsOneWidget,
        reason: 'The product form must offer one-step generation.',
      );
      await tap(key('production-primary'), 'generate');
      await until(() => receipts.isNotEmpty, 'render-receipt', seconds: 90);
      expect(requests, ['render']);
      expect(receipts, ['render']);
      final task = await container.read(lightProductionApiProvider).get(id!);
      expect(task.template, productStory);
      expect(task.productName, name);
      expect(task.referenceCopy, brief);
      expect(task.assets, hasLength(1));
      if (libraryAsset.isNotEmpty) expect(task.assets.single, libraryAsset);
      await screenshot('03-submitted');
      record('submitted');
      debugPrint('COMMERCIAL_QA_SUBMITTED $id');
    },
    skip: !enabled,
    timeout: const Timeout(Duration(minutes: 8)),
  );
}
