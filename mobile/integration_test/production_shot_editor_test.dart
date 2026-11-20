import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/state/auth_flow.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/features/light/production/production_page.dart';
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
// The platform picker is the boundary of the simulator test.
// ignore: depend_on_referenced_packages
import 'package:image_picker_platform_interface/image_picker_platform_interface.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';

class _FramePicker extends ImagePickerPlatform {
  @override
  Future<List<XFile>> getMultiImageWithOptions({
    MultiImagePickerOptions options = const MultiImagePickerOptions(),
  }) async => [XFile(const String.fromEnvironment('SHOTS_QA_FRAME'))];
}

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const session = String.fromEnvironment('SHOTS_QA_SESSION');
  const password = String.fromEnvironment('SHOTS_QA_PASSWORD');
  const captureOnly = bool.fromEnvironment('SHOTS_QA_CAPTURE_ONLY');
  const capturePath = String.fromEnvironment('SHOTS_QA_OUT');

  testWidgets(
    'Guided shot previews, retake and explicit composition',
    (tester) async {
      expect(Uri.parse(Env.apiBase).host, isIn(['127.0.0.1', 'localhost']));
      await app.main();
      await tester.pump();
      final container = ProviderScope.containerOf(
        tester.element(find.byType(BossipApp)),
        listen: false,
      );
      if (container.read(authProvider).user?.username != 'devtest') {
        await container.read(authFlowProvider).login('devtest', password);
      }
      await container.read(uiModeProvider.notifier).setMode(UiMode.light);
      container.read(i18nProvider.notifier).setLanguage('zh-CN');
      final router = container.read(routerProvider);
      final api = container.read(lightProductionApiProvider);
      final dio = container.read(apiDioProvider);
      final original = await api.get(session);
      expect(original.shotWorkspace?.shots.length, 9);
      final support = await getApplicationSupportDirectory();
      final evidence = await Directory(
        capturePath.isEmpty ? '${support.path}/shot-editor-qa' : capturePath,
      ).create(recursive: true);
      debugPrint('SHOT_QA_EVIDENCE ${evidence.path}');
      Finder key(String value) => find.byKey(Key(value));
      Future<void> until(
        bool Function() ready,
        String step, {
        int seconds = 40,
      }) async {
        final end = DateTime.now().add(Duration(seconds: seconds));
        while (!ready() && DateTime.now().isBefore(end)) {
          await tester.pump(const Duration(milliseconds: 170));
          await Future<void>.delayed(const Duration(milliseconds: 130));
        }
        expect(ready(), isTrue, reason: step);
        debugPrint('SHOT_QA $step');
      }

      Future<void> capture(String name) async {
        await tester.pump(const Duration(milliseconds: 450));
        final visibleImages = find.byType(Image).hitTestable().evaluate();
        await Future.wait([
          for (final element in visibleImages)
            if ((element.widget as Image).image is NetworkImage)
              precacheImage(
                (element.widget as Image).image,
                element,
                onError: (_, _) {},
              ).timeout(const Duration(seconds: 10), onTimeout: () {}),
        ]);
        await tester.pump(const Duration(milliseconds: 200));
        final bytes = await binding.takeScreenshot(name);
        await File('${evidence.path}/$name.png').writeAsBytes(bytes);
      }

      Future<void> tap(Finder target) async {
        await until(
          () => target.hitTestable().evaluate().isNotEmpty,
          'tap $target',
        );
        await tester.tap(target.hitTestable().first);
        await tester.pump(const Duration(milliseconds: 380));
      }

      Future<void> reveal(String value) async {
        await tester.scrollUntilVisible(
          key(value),
          470,
          maxScrolls: 14,
          scrollable: find
              .descendant(
                of: key('production-shot-list'),
                matching: find.byType(Scrollable),
              )
              .first,
        );
        await tester.pump(const Duration(milliseconds: 350));
      }

      Future<void> top() async {
        final scroll = find
            .descendant(
              of: key('production-shot-list'),
              matching: find.byType(Scrollable),
            )
            .first;
        tester.state<ScrollableState>(scroll).position.jumpTo(0);
        await tester.pump(const Duration(milliseconds: 300));
      }

      // Use the real film and its signed frames to render a reproducible view of a
      // partially complete initial run. Only this GET response is staged.
      final preview = InterceptorsWrapper(
        onResponse: (response, handler) {
          if (response.requestOptions.path.endsWith('/productions/$session')) {
            final data = response.data as Map<String, dynamic>;
            final workspace = Map<String, dynamic>.from(
              data['shot_editor'] as Map,
            );
            final source = workspace['shots'] as List;
            final dog = Map<String, dynamic>.from(source[4] as Map);
            final reveal = Map<String, dynamic>.from(source[5] as Map);
            final bus = Map<String, dynamic>.from(source[7] as Map);
            for (final card in [reveal, bus]) {
              final version = card['version'] as Map;
              card['first_frame'] = version['first_frame'];
              card['prompt'] = version['prompt'];
              card['version'] = null;
              card['status'] = 'generating';
              card['editable'] = false;
            }
            workspace['shots'] = [dog, reveal, bus];
            workspace['films'] = <Object>[];
            data['shot_editor'] = workspace;
            data['status'] = 'working';
            data['phase'] = 'wan_shots';
            data['shot_progress'] = {
              'total': 9,
              'filmed': 4,
              'phase': 'wan_shots',
            };
            data['output'] = null;
          }
          handler.next(response);
        },
      );
      dio.interceptors.add(preview);
      router.go(Paths.lightProduction(session));
      await until(
        () => key('production-progress').evaluate().isNotEmpty,
        'initial progress',
      );
      await Future<void>.delayed(const Duration(seconds: 2));
      await capture('01-generating');
      await tap(find.text('展开'));
      expect(find.text('收起'), findsOneWidget);
      dio.interceptors.remove(preview);
      await until(
        () => key('production-result-preview').evaluate().isNotEmpty,
        'previous film ready',
      );

      // Keep the provider test isolated in the copied session, using a short
      // departing-shuttle take and a vision frame from the same sequence.
      final element = tester.element(find.byType(ProductionPage));
      final scope = ProviderScope.containerOf(element, listen: false);
      expect(scope, same(container));
      ImagePickerPlatform.instance = _FramePicker();
      const editKey = captureOnly
          ? 'production-edit-shot-6'
          : 'production-edit-shot-8';
      await reveal(editKey);
      await tap(key(editKey));
      await tap(key('production-shot-prompt-toggle'));
      expect(key('production-shot-prompt-field'), findsNothing);
      await tap(key('production-shot-prompt-toggle'));
      expect(key('production-shot-prompt-field'), findsOneWidget);
      await tap(key('production-replace-tail'));
      await tap(find.text('手机相册'));
      await until(
        () => find.text('尾帧已更换，需重新生成此片段').evaluate().isNotEmpty,
        'tail replacement',
      );
      await capture('02-segment-edit');
      if (captureOnly) {
        Navigator.of(
          tester.element(key('production-shot-prompt-toggle')),
        ).pop();
        await tester.pump(const Duration(milliseconds: 380));
        await top();
        await tap(key('production-version-history'));
        await capture('04-history');
        expect(find.text('成片 v2'), findsOneWidget);
        expect(find.text('成片 v1'), findsOneWidget);
        final pendingPreview = InterceptorsWrapper(
          onResponse: (response, handler) {
            if (response.requestOptions.path.endsWith(
              '/productions/$session',
            )) {
              final data = response.data as Map<String, dynamic>;
              final work = Map<String, dynamic>.from(
                data['shot_editor'] as Map,
              );
              final films = (work['films'] as List)
                  .cast<Map<String, dynamic>>();
              final previous = films.last;
              work['films'] = [
                {...previous, 'current': true},
              ];
              work['can_compose'] = true;
              work['shots'] = [
                for (final shot
                    in (work['shots'] as List).cast<Map<String, dynamic>>())
                  {...shot, 'pending': shot['index'] == 8},
              ];
              data['output'] = previous['media'];
              data['shot_editor'] = work;
            }
            handler.next(response);
          },
        );
        dio.interceptors.add(pendingPreview);
        router.go(Paths.home);
        await tester.pump(const Duration(milliseconds: 450));
        router.go(Paths.lightProduction(session));
        await tap(key('production-review-compose'));
        await capture('03-recompose');
        await tap(key('production-defer-compose'));
        dio.interceptors.remove(pendingPreview);
        return;
      }
      await tap(key('production-retake-shot'));
      await until(
        () => key('production-confirm-compose').evaluate().isNotEmpty,
        'new clip ready; reassembly offered',
        seconds: 480,
      );
      expect((await api.get(session)).output?.id, original.output?.id);
      await capture('03-recompose');
      await tap(key('production-defer-compose'));
      await top();
      await tap(key('production-review-compose'));
      await tap(key('production-confirm-compose'));
      await until(
        () => find.text('当前成片 · v2').evaluate().isNotEmpty,
        'new film published',
        seconds: 180,
      );
      final completed = await api.get(session);
      expect(completed.output?.id, isNot(original.output?.id));
      expect(completed.shotWorkspace?.films.length, 2);
      expect(
        completed.shotWorkspace?.shots
            .singleWhere((shot) => shot.index == 8)
            .versions
            .length,
        2,
      );
      await tap(key('production-version-history'));
      await capture('04-history');
      expect(find.text('成片 v2'), findsOneWidget);
      expect(find.text('成片 v1'), findsOneWidget);
    },
    skip: session.isEmpty,
    timeout: const Timeout(Duration(minutes: 15)),
  );
}
